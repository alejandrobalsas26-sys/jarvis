"""core/prefix_cache.py — V69 M58.5: cache observation, compatibility & invalidation.

WHAT THIS DOES AND DOES NOT CLAIM
---------------------------------
Ollama does not expose its KV cache. This module therefore NEVER asserts a KV-cache
hit. It reasons only from OBSERVABLE evidence the native transport already returns
(``core.ollama_native.ChatChunk``): ``prompt_eval_count`` / ``prompt_eval_duration``
(how much prefill the server actually did), ``load_duration`` (did it load weights
this turn), and the time to first CONTENT token. From those it CLASSIFIES what most
likely happened — and, critically, it never treats "the model is loaded" as proof
that a prefix was reused (the exact fallacy M58 forbids).

A prefix has an IDENTITY (``core.prompt_manifest.PromptManifest.compatibility_identity``)
that excludes the contract delta and every turn-dynamic field. Two turns with the
same identity MAY share a warmed prefix; two with different identities never may.
When the identity changes, this module names the DETERMINISTIC reason (model / ctx /
language / policy / scope / schema / power changed) so a stale prewarm is marked
stale and an incompatible metric is never reused as proof of readiness.

Everything here is bounded and content-free: counters, milliseconds, fingerprints and
enum states. No user message, prompt text, answer or tool argument is ever stored.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum


class CacheState(str, Enum):
    """The classification of one observed turn's prefill, relative to the warmed
    prefix. Ordered from least to most evidence of reuse."""

    UNKNOWN = "UNKNOWN"                                  # never observed
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"      # metrics missing
    COLD_MODEL = "COLD_MODEL"                            # weights loaded this turn
    MODEL_WARM_PREFIX_UNKNOWN = "MODEL_WARM_PREFIX_UNKNOWN"  # warm, but no baseline
    CONFIG_MISMATCH = "CONFIG_MISMATCH"                  # identity != warmed identity
    PREFIX_INVALIDATED = "PREFIX_INVALIDATED"            # a change invalidated it
    PREFIX_REUSE_LIKELY = "PREFIX_REUSE_LIKELY"          # low prefill, weak evidence
    PREFIX_REUSE_OBSERVED = "PREFIX_REUSE_OBSERVED"      # prefill dropped vs baseline


class InvalidationReason(str, Enum):
    """WHY a warmed prefix is no longer usable. Deterministic and total."""

    MODEL_CHANGED = "MODEL_CHANGED"
    TRANSPORT_CHANGED = "TRANSPORT_CHANGED"
    NUM_CTX_CHANGED = "NUM_CTX_CHANGED"
    THINK_CHANGED = "THINK_CHANGED"
    CORE_PROMPT_CHANGED = "CORE_PROMPT_CHANGED"
    LANGUAGE_CHANGED = "LANGUAGE_CHANGED"
    AUTHORITY_CHANGED = "AUTHORITY_CHANGED"
    SCOPE_CHANGED = "SCOPE_CHANGED"
    SECURITY_POLICY_CHANGED = "SECURITY_POLICY_CHANGED"
    PERSONALITY_CHANGED = "PERSONALITY_CHANGED"
    TOOL_SCHEMA_CHANGED = "TOOL_SCHEMA_CHANGED"
    CONTRACT_SCHEMA_CHANGED = "CONTRACT_SCHEMA_CHANGED"
    POWER_PROFILE_CHANGED = "POWER_PROFILE_CHANGED"
    MANUAL_INVALIDATION = "MANUAL_INVALIDATION"


# Field-by-field precedence used to name the invalidation reason. The FIRST field
# that differs wins, so the reason is deterministic and total. ``power_profile`` is
# tracked here (not on the prompt manifest) because it affects prewarm scheduling,
# not the prompt bytes.
_INVALIDATION_ORDER: tuple[tuple[str, InvalidationReason], ...] = (
    ("model", InvalidationReason.MODEL_CHANGED),
    ("transport", InvalidationReason.TRANSPORT_CHANGED),
    ("num_ctx", InvalidationReason.NUM_CTX_CHANGED),
    ("think", InvalidationReason.THINK_CHANGED),
    ("core_fingerprint", InvalidationReason.CORE_PROMPT_CHANGED),
    ("language", InvalidationReason.LANGUAGE_CHANGED),
    ("authority_mode", InvalidationReason.AUTHORITY_CHANGED),
    ("scope_fingerprint", InvalidationReason.SCOPE_CHANGED),
    ("security_policy_version", InvalidationReason.SECURITY_POLICY_CHANGED),
    ("personality_fingerprint", InvalidationReason.PERSONALITY_CHANGED),
    ("tool_schema_fingerprint", InvalidationReason.TOOL_SCHEMA_CHANGED),
    ("contract_schema_version", InvalidationReason.CONTRACT_SCHEMA_CHANGED),
    ("power_profile", InvalidationReason.POWER_PROFILE_CHANGED),
)


def _descriptor(manifest, power_profile: str = "UNKNOWN") -> dict:
    """The comparable field view of a manifest + power profile (content-free)."""
    return {
        "model": getattr(manifest, "model", ""),
        "transport": getattr(manifest, "transport", ""),
        "num_ctx": int(getattr(manifest, "num_ctx", 0) or 0),
        "think": getattr(manifest, "think", None),
        "core_fingerprint": getattr(manifest, "core_fingerprint", ""),
        "language": getattr(manifest, "language", ""),
        "authority_mode": getattr(manifest, "authority_mode", ""),
        "scope_fingerprint": getattr(manifest, "scope_fingerprint", ""),
        "security_policy_version": getattr(manifest, "security_policy_version", ""),
        "personality_fingerprint": getattr(manifest, "personality_fingerprint", ""),
        "tool_schema_fingerprint": getattr(manifest, "tool_schema_fingerprint", ""),
        "contract_schema_version": getattr(manifest, "contract_schema_version", ""),
        "power_profile": str(power_profile or "UNKNOWN"),
    }


def diff_invalidation(old, new, *, old_power: str = "UNKNOWN",
                      new_power: str = "UNKNOWN") -> InvalidationReason | None:
    """Return the deterministic invalidation reason between two manifests, or None
    when they are compatible. The first differing field (in precedence order) wins."""
    a = _descriptor(old, old_power)
    b = _descriptor(new, new_power)
    for field_name, reason in _INVALIDATION_ORDER:
        if a.get(field_name) != b.get(field_name):
            return reason
    return None


@dataclass
class PrefixObservation:
    """One turn's observable prefill evidence. Content-free."""

    compatibility_identity: str
    prompt_eval_count: int | None = None
    prompt_eval_ms: float | None = None
    load_ms: float | None = None
    first_content_ms: float | None = None
    at: float = 0.0
    state: CacheState = CacheState.UNKNOWN

    def snapshot(self) -> dict:
        return {
            "compatibility_identity": self.compatibility_identity,
            "prompt_eval_count": self.prompt_eval_count,
            "prompt_eval_ms": self.prompt_eval_ms,
            "load_ms": self.load_ms,
            "first_content_ms": self.first_content_ms,
            "state": self.state.value,
        }


# Below this the model clearly did NOT load weights this turn (warm). Ollama reports
# a small nonzero load_duration even when resident, so the threshold is generous.
_WARM_LOAD_MS = 800.0
# A prompt_eval that dropped to this fraction of the identity's cold baseline is
# strong evidence the server skipped re-prefilling the shared prefix.
_REUSE_RATIO = 0.6
# Below this fraction, we call it OBSERVED; between it and _REUSE_RATIO, LIKELY.
_STRONG_REUSE_RATIO = 0.4


@dataclass
class PrefixCacheObserver:
    """Bounded, process-global observer of prefix-reuse evidence.

    Holds, per compatibility identity: the first (cold) prompt-eval baseline and a
    small ring of recent observations. Classifies each new observation WITHOUT ever
    concluding reuse from model residency alone.
    """

    maxlen: int = 40
    _observations: "deque[PrefixObservation]" = field(
        default_factory=lambda: deque(maxlen=40))
    _cold_baseline_ms: dict = field(default_factory=dict)   # identity -> prompt_eval_ms
    _cold_baseline_count: dict = field(default_factory=dict)  # identity -> tokens
    invalidations: int = 0
    last_invalidation_reason: str | None = None
    reuse_observed: int = 0
    reuse_likely: int = 0
    cold_models: int = 0

    def __post_init__(self) -> None:
        n = max(4, min(int(self.maxlen), 400))
        self._observations = deque(self._observations, maxlen=n)

    def classify(
        self,
        *,
        compatibility_identity: str,
        prompt_eval_count: int | None,
        prompt_eval_ms: float | None,
        load_ms: float | None,
        first_content_ms: float | None = None,
        warmed_identity: str | None = None,
    ) -> CacheState:
        """Classify one turn's prefill and fold it into the rolling evidence.

        ``warmed_identity`` (optional) is the identity the last prewarm warmed; when
        it differs from this turn's identity the state is CONFIG_MISMATCH — a warmed
        metric from a different configuration is never counted as reuse.
        """
        state = self._classify(
            identity=compatibility_identity, count=prompt_eval_count,
            eval_ms=prompt_eval_ms, load_ms=load_ms,
            warmed_identity=warmed_identity)
        obs = PrefixObservation(
            compatibility_identity=compatibility_identity,
            prompt_eval_count=prompt_eval_count, prompt_eval_ms=prompt_eval_ms,
            load_ms=load_ms, first_content_ms=first_content_ms,
            at=time.time(), state=state)
        self._observations.append(obs)
        if state is CacheState.PREFIX_REUSE_OBSERVED:
            self.reuse_observed += 1
        elif state is CacheState.PREFIX_REUSE_LIKELY:
            self.reuse_likely += 1
        elif state is CacheState.COLD_MODEL:
            self.cold_models += 1
        return state

    def _classify(self, *, identity, count, eval_ms, load_ms, warmed_identity):
        # No evidence at all → cannot claim anything.
        if eval_ms is None and load_ms is None and count is None:
            return CacheState.INSUFFICIENT_EVIDENCE
        if warmed_identity is not None and identity != warmed_identity:
            return CacheState.CONFIG_MISMATCH
        # Weights loaded this turn → cold, regardless of anything else.
        if load_ms is not None and load_ms >= _WARM_LOAD_MS:
            # Record this as the cold baseline for the identity (first real prefill).
            if eval_ms is not None:
                self._cold_baseline_ms.setdefault(identity, eval_ms)
                if count is not None:
                    self._cold_baseline_count.setdefault(identity, count)
            return CacheState.COLD_MODEL
        # Model is warm from here on. Reuse requires a per-identity baseline AND a
        # measured prompt_eval to compare — residency alone proves nothing.
        if eval_ms is None:
            return CacheState.MODEL_WARM_PREFIX_UNKNOWN
        baseline = self._cold_baseline_ms.get(identity)
        if baseline is None:
            # First warm observation for this identity — record it as the baseline
            # but do NOT claim reuse yet (no cold reference to compare against).
            self._cold_baseline_ms[identity] = eval_ms
            if count is not None:
                self._cold_baseline_count[identity] = count
            return CacheState.MODEL_WARM_PREFIX_UNKNOWN
        if baseline <= 0:
            return CacheState.MODEL_WARM_PREFIX_UNKNOWN
        ratio = eval_ms / baseline
        if ratio <= _STRONG_REUSE_RATIO:
            return CacheState.PREFIX_REUSE_OBSERVED
        if ratio <= _REUSE_RATIO:
            return CacheState.PREFIX_REUSE_LIKELY
        # Prefill did not drop → the prefix was re-computed (or the baseline was
        # itself warm). Not enough to claim reuse.
        return CacheState.MODEL_WARM_PREFIX_UNKNOWN

    def note_invalidation(self, reason: InvalidationReason | str) -> None:
        """Record a deterministic invalidation. Clears per-identity baselines so a
        stale metric can never be reused as proof of readiness."""
        self.invalidations += 1
        self.last_invalidation_reason = getattr(reason, "value", str(reason))
        # A model/ctx/policy change invalidates the whole warmed set — the safe,
        # simple choice is to drop every baseline and let the next turn re-measure.
        self._cold_baseline_ms.clear()
        self._cold_baseline_count.clear()

    @property
    def last_state(self) -> CacheState:
        return self._observations[-1].state if self._observations else CacheState.UNKNOWN

    def observed_reuse_ratio(self) -> float | None:
        """Fraction of measured turns classified as reuse (observed or likely)."""
        measured = [o for o in self._observations
                    if o.state not in (CacheState.UNKNOWN,
                                       CacheState.INSUFFICIENT_EVIDENCE)]
        if not measured:
            return None
        reused = sum(1 for o in measured if o.state in (
            CacheState.PREFIX_REUSE_OBSERVED, CacheState.PREFIX_REUSE_LIKELY))
        return round(reused / len(measured), 3)

    def _p50(self, states: tuple[CacheState, ...]) -> float | None:
        xs = sorted(o.prompt_eval_ms for o in self._observations
                    if o.state in states and o.prompt_eval_ms is not None)
        return round(xs[len(xs) // 2], 1) if xs else None

    def snapshot(self) -> dict:
        return {
            "cache_state": self.last_state.value,
            "observations": len(self._observations),
            "invalidations": self.invalidations,
            "last_invalidation_reason": self.last_invalidation_reason,
            "observed_reuse_ratio": self.observed_reuse_ratio(),
            "reuse_observed": self.reuse_observed,
            "reuse_likely": self.reuse_likely,
            "cold_models": self.cold_models,
            "warm_prompt_eval_ms": self._p50((CacheState.PREFIX_REUSE_OBSERVED,
                                              CacheState.PREFIX_REUSE_LIKELY)),
            "cold_prompt_eval_ms": self._p50((CacheState.COLD_MODEL,)),
            "recent_prompt_eval_ms": (self._observations[-1].prompt_eval_ms
                                      if self._observations else None),
        }

    def reset(self) -> None:
        self._observations.clear()
        self._cold_baseline_ms.clear()
        self._cold_baseline_count.clear()
        self.invalidations = 0
        self.last_invalidation_reason = None
        self.reuse_observed = self.reuse_likely = self.cold_models = 0


# ── Process-global singleton ─────────────────────────────────────────────────
_observer: PrefixCacheObserver | None = None


def get_prefix_cache_observer() -> PrefixCacheObserver:
    global _observer
    if _observer is None:
        _observer = PrefixCacheObserver()
    return _observer


def reset_prefix_cache_observer(instance: PrefixCacheObserver | None = None) -> None:
    """Tests / a fresh process."""
    global _observer
    _observer = instance
