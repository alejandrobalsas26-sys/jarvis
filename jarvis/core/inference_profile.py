"""core/inference_profile.py — V69 M59.1: canonical inference profile identity.

WHY THIS MODULE EXISTS
----------------------
M58 aligned the four fields that unquestionably decide whether a native Ollama
runner can be reused across two requests — model, transport, ``think`` and
``num_ctx`` — and made the stable prompt PREFIX byte-identical across compatible
FAST contracts. Yet the live benchmark still observed a residual ``load_duration``
after a family prewarm, and the prewarm hand-maintained its OWN sampling options
(``temperature=0.0``, no ``top_p``/``repeat_penalty``) that differed from the live
turn's contract options. Two questions were left unanswered:

  1. Do those sampling differences make the prewarm warm a DIFFERENT runner than the
     live turn (so the residual load is a self-inflicted reload)?
  2. Or are they generation-only knobs that never reload the runner, so the residual
     load has some OTHER honest cause (eviction between prewarm and turn)?

Ollama's runner cache is keyed on the fields that build the llama context — model,
``num_ctx`` and the small set of ``num_*`` context options — plus the transport and
whether reasoning is engaged. The per-request SAMPLING options (temperature, top_p,
top_k, repeat_penalty, seed, stop, num_predict) are applied to an already-built
context and never rebuild it. This module encodes exactly that split so the runtime
can reason about it deterministically instead of guessing:

    RUNNER_IDENTITY    fields that CAN force a runner/context reload
    PREFIX_IDENTITY    the reusable prompt-prefix identity (PromptManifest)
    GENERATION_ONLY    per-request sampling that never reloads the runner

A prewarm profile is DERIVED from the live generation profile: it copies the runner
identity and prefix identity verbatim and keeps the same sampling posture, changing
only the output cap (``num_predict``). So the prewarm can never accidentally warm a
runner the live turn does not use, and the residual load — when the profiles are
proven compatible — is classified honestly as an eviction reload, never blamed on a
sampling mismatch that does not exist.

WHAT THIS MODULE REFUSES TO DO
------------------------------
It never claims two requests are equivalent without a field-by-field decision, it
never invents undocumented Ollama semantics (an unrecognised option is UNKNOWN and
forces a conservative incompatibility), and it never lets a generation-only knob
invalidate a runner. Everything it exposes is content-free: fingerprints, field
names and enum states — never prompt text, answers or tool arguments.

Pure and deterministic apart from reading ``core.config`` defaults through the
existing budget/manifest seams. No model, no network, no I/O of its own.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

# ── Option classification (the whole point of the module) ─────────────────────
# Options that participate in building the llama context: a difference in any of
# these can force Ollama to rebuild the runner. ``num_ctx`` is carried as a
# first-class RunnerIdentity field, so it is intentionally NOT repeated here.
RUNNER_AFFECTING_OPTION_KEYS: frozenset[str] = frozenset({
    "num_batch", "num_gpu", "num_thread", "num_keep", "num_gqa",
    "main_gpu", "low_vram", "f16_kv", "use_mmap", "use_mlock", "numa",
    "rope_frequency_base", "rope_frequency_scale", "vocab_only", "logits_all",
})
# Per-request sampling / decoding knobs applied to an already-built context. A
# difference here changes the OUTPUT, never the runner — so it never invalidates a
# prewarm. ``num_predict`` lives here: a smaller output cap is exactly how a prewarm
# stays cheap while remaining runner-identical.
GENERATION_ONLY_OPTION_KEYS: frozenset[str] = frozenset({
    "num_predict", "temperature", "top_p", "top_k", "min_p", "typical_p",
    "repeat_penalty", "repeat_last_n", "presence_penalty", "frequency_penalty",
    "penalize_newline", "mirostat", "mirostat_tau", "mirostat_eta", "tfs_z",
    "seed", "stop",
})


def classify_option(key: str) -> str:
    """Classify ONE native option key. Total: an unrecognised key is UNKNOWN, which
    the compatibility check treats conservatively (never silently ignored)."""
    if key in RUNNER_AFFECTING_OPTION_KEYS:
        return "runner"
    if key in GENERATION_ONLY_OPTION_KEYS:
        return "generation"
    return "unknown"


def _fingerprint(*parts) -> str:
    """A content-free 16-hex fingerprint over a canonical serialization (SHA-256,
    NOT process-salted ``hash``). Only the digest prefix is exposed."""
    canon = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(canon.encode("utf-8", "replace")).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════════════════════
#  RUNNER IDENTITY — the fields that can force a runner reload
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class RunnerIdentity:
    """The identity of a native Ollama runner configuration.

    Two requests whose runner identities are EQUAL address the same resident runner
    and never force a rebuild for a runner reason; two whose identities differ may.
    ``grammar`` is the structured-output / format mode ("" when free-form); a change
    to it is treated as runner-affecting per M59.1 ("grammar change invalidates").
    """

    model: str
    transport: str
    think: bool | None
    num_ctx: int
    grammar: str = ""
    # Runner-affecting options beyond num_ctx, kept sorted & content-free.
    runner_options: tuple[tuple[str, str], ...] = ()

    def fields(self) -> dict:
        return {
            "model": self.model, "transport": self.transport,
            "think": self.think, "num_ctx": self.num_ctx,
            "grammar": self.grammar,
            "runner_options": dict(self.runner_options),
        }

    def fingerprint(self) -> str:
        return _fingerprint(
            "runner", self.model, self.transport, self.think, int(self.num_ctx),
            self.grammar, self.runner_options)


# ══════════════════════════════════════════════════════════════════════════════
#  GENERATION-ONLY OPTIONS — never invalidate a runner
# ══════════════════════════════════════════════════════════════════════════════
# Only these keys are forwarded through the native transport's own allowlist today
# (core.ollama_native._ALLOWED_EXTRA_OPTIONS). Mirrored here so the derived prewarm
# forwards the SAME sampling posture the live turn uses — no second hand-maintained
# option set (M59.1: "no duplicate manually maintained options").
_TRANSPORT_EXTRA_KEYS: tuple[str, ...] = ("top_p", "repeat_penalty")


@dataclass(frozen=True)
class GenerationOnlyOptions:
    """Per-request sampling for ONE inference. A difference here is an output
    difference, never a runner difference."""

    num_predict: int
    temperature: float
    top_p: float
    repeat_penalty: float
    top_k: int | None = None
    seed: int | None = None
    stop: tuple[str, ...] = ()
    # Any other recognised generation-only knobs, kept for parity accounting.
    extra: tuple[tuple[str, float], ...] = ()

    def transport_options_extra(self) -> dict:
        """The ``options_extra`` dict for :func:`core.ollama_native.chat_stream` —
        exactly the keys its allowlist forwards, no more."""
        return {"top_p": float(self.top_p), "repeat_penalty": float(self.repeat_penalty)}

    def fingerprint(self) -> str:
        return _fingerprint(
            "gen", int(self.num_predict), round(float(self.temperature), 4),
            round(float(self.top_p), 4), round(float(self.repeat_penalty), 4),
            self.top_k, self.seed, self.stop, self.extra)


# ══════════════════════════════════════════════════════════════════════════════
#  THE COMBINED PROFILE
# ══════════════════════════════════════════════════════════════════════════════
class ProfileKind(str, Enum):
    LIVE = "LIVE"
    PREWARM = "PREWARM"


@dataclass(frozen=True)
class InferenceProfile:
    """A complete, content-free description of ONE native inference request: its
    runner identity, its reusable prefix identity, and its generation-only options.

    The ``keep_alive`` string is carried for accounting but is deliberately NOT part
    of the runner identity: it governs the eviction TIMER, not the runner build, so a
    mismatch cannot force a reload (it can only change how long the model lingers).
    """

    kind: ProfileKind
    runner: RunnerIdentity
    prefix_identity: str
    generation: GenerationOnlyOptions
    keep_alive: str = "10m"

    def runner_fingerprint(self) -> str:
        return self.runner.fingerprint()

    def snapshot(self) -> dict:
        """Content-free diagnostics: fingerprints and enum states, never options that
        could reconstruct a prompt or an answer."""
        return {
            "kind": self.kind.value,
            "runner_fingerprint": self.runner.fingerprint(),
            "prefix_identity": self.prefix_identity,
            "generation_fingerprint": self.generation.fingerprint(),
            "num_ctx": self.runner.num_ctx,
            "keep_alive": self.keep_alive,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  COMPATIBILITY — the deterministic, field-by-field decision
# ══════════════════════════════════════════════════════════════════════════════
class ResidualLoadClass(str, Enum):
    """How to read a ``load_duration`` observed on the first live turn after a
    prewarm — honestly, never blaming a mismatch that does not exist."""

    NO_RELOAD = "NO_RELOAD"                    # warm: model was resident, no rebuild
    RELOAD_RUNNER_MISMATCH = "RELOAD_RUNNER_MISMATCH"   # profiles differ → expected
    RELOAD_DESPITE_COMPATIBLE = "RELOAD_DESPITE_COMPATIBLE"  # compatible yet reloaded
    UNKNOWN = "UNKNOWN"                          # no measurement


# A load below this clearly did not rebuild the runner (Ollama reports a small
# nonzero load even when resident). Matches core.prefix_cache._WARM_LOAD_MS.
_WARM_LOAD_MS = 800.0


@dataclass(frozen=True)
class ProfileCompatibility:
    """The verdict for a (prewarm, live) pair. Content-free."""

    runner_compatible: bool
    prefix_compatible: bool
    generation_only_differs: bool
    incompatible_fields: tuple[str, ...]
    unknown_fields: tuple[str, ...]
    prewarm_runner_fingerprint: str = ""
    live_runner_fingerprint: str = ""

    @property
    def compatible(self) -> bool:
        """A prewarm is USEFUL for a live turn iff the runner and the prefix match.
        A generation-only difference (e.g. the output cap) never breaks this."""
        return self.runner_compatible and self.prefix_compatible

    def classify_residual_load(self, load_ms: float | None) -> ResidualLoadClass:
        if load_ms is None:
            return ResidualLoadClass.UNKNOWN
        if load_ms < _WARM_LOAD_MS:
            return ResidualLoadClass.NO_RELOAD
        if not self.runner_compatible:
            return ResidualLoadClass.RELOAD_RUNNER_MISMATCH
        # Compatible runner yet a real load: the prewarm did not warm the wrong
        # runner — the model was most likely EVICTED between prewarm and this turn.
        return ResidualLoadClass.RELOAD_DESPITE_COMPATIBLE

    def snapshot(self) -> dict:
        return {
            "compatible": self.compatible,
            "runner_compatible": self.runner_compatible,
            "prefix_compatible": self.prefix_compatible,
            "generation_only_differs": self.generation_only_differs,
            "incompatible_fields": list(self.incompatible_fields),
            "unknown_fields": list(self.unknown_fields),
            "prewarm_runner_identity": self.prewarm_runner_fingerprint,
            "live_runner_identity": self.live_runner_fingerprint,
        }


def compare_runner(prewarm: RunnerIdentity, live: RunnerIdentity
                   ) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    """Field-by-field runner comparison → (compatible, incompatible_fields,
    unknown_fields). A runner-affecting option present in one profile but absent in
    the other, or any UNKNOWN option that is not byte-identical, is conservatively
    incompatible."""
    incompatible: list[str] = []
    unknown: list[str] = []
    for name in ("model", "transport", "think", "num_ctx", "grammar"):
        if getattr(prewarm, name) != getattr(live, name):
            incompatible.append(name)
    p_opts = dict(prewarm.runner_options)
    l_opts = dict(live.runner_options)
    for key in sorted(set(p_opts) | set(l_opts)):
        klass = classify_option(key)
        if klass == "unknown":
            # Unknown option: if it is not present-and-equal on both sides we cannot
            # prove it is harmless, so it is a conservative incompatibility.
            if p_opts.get(key) != l_opts.get(key):
                unknown.append(key)
                incompatible.append(f"option:{key}")
        elif p_opts.get(key) != l_opts.get(key):
            incompatible.append(f"option:{key}")
    return (not incompatible, tuple(incompatible), tuple(unknown))


def profile_compatibility(prewarm: InferenceProfile, live: InferenceProfile
                          ) -> ProfileCompatibility:
    """The full deterministic verdict for a (prewarm, live) profile pair."""
    runner_ok, incompatible, unknown = compare_runner(prewarm.runner, live.runner)
    prefix_ok = (prewarm.prefix_identity == live.prefix_identity)
    if not prefix_ok:
        incompatible = incompatible + ("prefix_identity",)
    gen_differs = prewarm.generation.fingerprint() != live.generation.fingerprint()
    return ProfileCompatibility(
        runner_compatible=runner_ok,
        prefix_compatible=prefix_ok,
        generation_only_differs=gen_differs,
        incompatible_fields=incompatible,
        unknown_fields=unknown,
        prewarm_runner_fingerprint=prewarm.runner.fingerprint(),
        live_runner_fingerprint=live.runner.fingerprint(),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  BUILDING PROFILES FROM THE LIVE GENERATION BUDGET
# ══════════════════════════════════════════════════════════════════════════════
# The tiny output cap a derived prewarm uses. It differs from the live num_predict —
# that difference is generation-only and explicitly allowed; everything else is copied
# from the live profile so the runner and prefix identities are byte-identical.
PREWARM_NUM_PREDICT = 4


def _prefix_identity(*, model, transport, think, num_ctx, language,
                     language_directive, authority_mode, scope_fingerprint,
                     tool_schema_fingerprint, shape) -> str:
    """The reusable prefix identity via the existing PromptManifest — the SAME
    ``compatibility_identity`` the live turn and the cache observer use."""
    from core.prompt_manifest import build_manifest
    manifest = build_manifest(
        model=model, transport=transport, think=think, num_ctx=num_ctx,
        language=language, language_directive=language_directive,
        authority_mode=authority_mode, scope_fingerprint=scope_fingerprint,
        tool_schema_fingerprint=tool_schema_fingerprint, shape=shape)
    return manifest.compatibility_identity()


def live_profile_from_budget(
    budget,
    *,
    shape=None,
    model: str,
    transport: str = "native",
    think: bool | None = False,
    language: str = "es",
    language_directive: str = "",
    authority_mode: str = "STANDARD",
    scope_fingerprint: str = "",
    tool_schema_fingerprint: str = "",
    grammar: str = "",
    runner_options: dict | None = None,
) -> InferenceProfile:
    """Build the canonical LIVE profile from a :class:`core.generation_budget.
    GenerationBudget` (the real per-turn options) plus the manifest inputs.

    This is the single source of truth for the live request's runner identity,
    prefix identity and generation options — nothing downstream re-derives them.
    """
    runner = RunnerIdentity(
        model=str(model or ""), transport=str(transport or "native"),
        think=think, num_ctx=int(getattr(budget, "num_ctx", 2048)),
        grammar=str(grammar or ""),
        runner_options=_norm_options(runner_options),
    )
    generation = GenerationOnlyOptions(
        num_predict=int(getattr(budget, "num_predict", 128)),
        temperature=float(getattr(budget, "temperature", 0.3)),
        top_p=float(getattr(budget, "top_p", 0.9)),
        repeat_penalty=float(getattr(budget, "repeat_penalty", 1.1)),
    )
    prefix_id = _prefix_identity(
        model=runner.model, transport=runner.transport, think=think,
        num_ctx=runner.num_ctx, language=language,
        language_directive=language_directive, authority_mode=authority_mode,
        scope_fingerprint=scope_fingerprint,
        tool_schema_fingerprint=tool_schema_fingerprint, shape=shape)
    return InferenceProfile(
        kind=ProfileKind.LIVE, runner=runner, prefix_identity=prefix_id,
        generation=generation,
        keep_alive=str(getattr(budget, "keep_alive", "10m")))


def derive_prewarm_profile(live: InferenceProfile, *,
                           num_predict: int = PREWARM_NUM_PREDICT
                           ) -> InferenceProfile:
    """Derive the PREWARM profile from a LIVE profile.

    Copies the runner identity and prefix identity verbatim, keeps the live sampling
    posture (so the warmed request looks like a real turn), and changes ONLY the
    output cap. The result is guaranteed runner- and prefix-compatible with the live
    profile; only ``generation_only_differs`` is true (the cap)."""
    capped = GenerationOnlyOptions(
        num_predict=int(num_predict),
        temperature=live.generation.temperature,
        top_p=live.generation.top_p,
        repeat_penalty=live.generation.repeat_penalty,
        top_k=live.generation.top_k,
        seed=live.generation.seed,
        stop=live.generation.stop,
        extra=live.generation.extra,
    )
    return InferenceProfile(
        kind=ProfileKind.PREWARM, runner=live.runner,
        prefix_identity=live.prefix_identity, generation=capped,
        keep_alive=live.keep_alive)


def profiles_for_shape(
    shape,
    *,
    model: str,
    num_ctx: int,
    transport: str = "native",
    think: bool | None = False,
    language: str = "es",
    language_directive: str = "",
    authority_mode: str = "STANDARD",
    scope_fingerprint: str = "",
    tool_schema_fingerprint: str = "",
    grammar: str = "",
    settings=None,
) -> tuple[InferenceProfile, InferenceProfile]:
    """Return the (live, prewarm) profile pair for a :class:`core.response_contract.
    ResponseShape`, both derived from the SAME live :class:`GenerationBudget`.

    The prewarm profile is guaranteed runner- and prefix-compatible with the live
    profile — this is the seam the family prewarm uses so it never hand-maintains a
    divergent option set (M59.1.1)."""
    from core.generation_budget import budget_for_shape
    budget = budget_for_shape(shape, settings=settings, num_ctx=int(num_ctx))
    live = live_profile_from_budget(
        budget, shape=shape, model=model, transport=transport, think=think,
        language=language, language_directive=language_directive,
        authority_mode=authority_mode, scope_fingerprint=scope_fingerprint,
        tool_schema_fingerprint=tool_schema_fingerprint, grammar=grammar)
    return live, derive_prewarm_profile(live)


def _norm_options(opts: dict | None) -> tuple[tuple[str, str], ...]:
    """Canonicalise a runner-options dict into a sorted, hashable, content-free
    tuple of (key, str(value)) pairs."""
    if not opts:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in opts.items()))


# ══════════════════════════════════════════════════════════════════════════════
#  Health block (content-free)
# ══════════════════════════════════════════════════════════════════════════════
def sampling_parity_health(prewarm: InferenceProfile | None,
                           live: InferenceProfile | None,
                           *, observed_load_ms: float | None = None) -> dict:
    """The M59 SAMPLING-PARITY health block. Never exposes options, only fingerprints,
    field names and enum states."""
    if prewarm is None or live is None:
        return {
            "prewarm_runner_identity": None, "live_runner_identity": None,
            "compatible": None, "incompatible_fields": [], "unknown_fields": [],
            "residual_load_ms": observed_load_ms,
            "residual_load_class": ResidualLoadClass.UNKNOWN.value,
        }
    verdict = profile_compatibility(prewarm, live)
    out = verdict.snapshot()
    out["residual_load_ms"] = observed_load_ms
    out["residual_load_class"] = verdict.classify_residual_load(observed_load_ms).value
    return out
