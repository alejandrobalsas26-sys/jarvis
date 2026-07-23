"""core/contract_family.py — V69 M58.4: cache-safe contract-family prefix prewarm.

WHY NOT TEN PREWARMS
--------------------
M57 has ten response contracts. Warming ten independent boot-time generations on a
15 W CPU would be a self-inflicted boot storm. It is also unnecessary: M58.2 made the
STABLE prefix (identity + security + language) byte-identical across every FAST
contract, so warming it ONCE benefits them all. The only per-contract variation is a
tiny compact delta (M58.3), so a small number of FAMILY prewarms — each warming the
real stable prefix plus one family-representative delta — covers the field.

FAMILIES (proven, not assumed)
------------------------------
  CONCISE      INSTANT · BRIEF · ERROR_RECOVERY     native FAST, no tools  -> prewarm
  EXPLANATORY  STANDARD · TECHNICAL · STRUCTURED     native FAST, no tools  -> prewarm
  SPECIALIZED  CODE · DOCUMENT_GROUNDED · OPERATIONAL · DEEP                -> on demand

Only families served by the native FAST no-tool transport are prewarmed. CODE/DEEP
route to a DIFFERENT model (CODER/DEEP role); DOCUMENT_GROUNDED/OPERATIONAL are
evidence-bound and shaped per turn — all four stay on-demand.

WHAT A FAMILY PREWARM IS FORBIDDEN TO DO
----------------------------------------
Exactly like M56's single prewarm: no tools, no RAG, no history, no memory, no TTS,
no user-visible answer, no history mutation, tiny bounded output, and it uses the
EXACT production model / native transport / think=false / live num_ctx / real stable
prefix. It takes the residency governor's lowest priority, yields to the operator,
and never starts after STOPPING. It records its OWN counters, separate from live-turn
metrics. Every metric is content-free.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from core.response_contract import ResponseContract


class ContractFamily(str, Enum):
    CONCISE = "CONCISE"
    EXPLANATORY = "EXPLANATORY"
    SPECIALIZED = "SPECIALIZED"


FAMILY_MEMBERS: dict[ContractFamily, tuple[ResponseContract, ...]] = {
    ContractFamily.CONCISE: (
        ResponseContract.INSTANT, ResponseContract.BRIEF,
        ResponseContract.ERROR_RECOVERY,
    ),
    ContractFamily.EXPLANATORY: (
        ResponseContract.STANDARD, ResponseContract.TECHNICAL,
        ResponseContract.STRUCTURED,
    ),
    ContractFamily.SPECIALIZED: (
        ResponseContract.CODE, ResponseContract.DOCUMENT_GROUNDED,
        ResponseContract.OPERATIONAL, ResponseContract.DEEP,
    ),
}
# Only these families use the native FAST no-tool transport → only these are warmed.
PREWARMABLE_FAMILIES: tuple[ContractFamily, ...] = (
    ContractFamily.CONCISE, ContractFamily.EXPLANATORY,
)
# The representative contract whose compact delta a family prewarm warms.
_REPRESENTATIVE: dict[ContractFamily, ResponseContract] = {
    ContractFamily.CONCISE: ResponseContract.BRIEF,
    ContractFamily.EXPLANATORY: ResponseContract.STANDARD,
    ContractFamily.SPECIALIZED: ResponseContract.CODE,
}

_CONTRACT_TO_FAMILY: dict[ResponseContract, ContractFamily] = {
    c: fam for fam, members in FAMILY_MEMBERS.items() for c in members
}

# A minimal, deterministic, content-free user turn. It is NOT the meaningless "ok":
# the REAL stable prefix is the system message, which this exercises fully. The user
# line only has to trigger a first token; a period keeps generation trivially short.
_FAMILY_USER_PROMPT = "."
_FAMILY_NUM_PREDICT = 4


def family_of(contract) -> ContractFamily:
    """The family a contract belongs to. Total (SPECIALIZED is the catch-all)."""
    if isinstance(contract, str):
        try:
            contract = ResponseContract(contract)
        except ValueError:
            return ContractFamily.SPECIALIZED
    return _CONTRACT_TO_FAMILY.get(contract, ContractFamily.SPECIALIZED)


def representative_contract(family: ContractFamily) -> ResponseContract:
    return _REPRESENTATIVE.get(family, ResponseContract.BRIEF)


def _representative_shape(family: ContractFamily, language: str):
    """A minimal ResponseShape carrying the family's representative contract."""
    from core.response_contract import ContractReason, ResponseShape, _BASE_SHAPES
    contract = representative_contract(family)
    return ResponseShape(contract=contract, reason=ContractReason.GENERAL_EDUCATIONAL,
                         language=language, **_BASE_SHAPES[contract])


def family_prewarm_messages(family: ContractFamily, *, language_directive: str = "",
                            language: str = "es") -> list[dict]:
    """The message list a family prewarm sends: the REAL stable prefix + the family's
    compact delta as the system message, then a minimal user turn.

    Deliberately omits the host-clock/continuation tail — those are turn-dynamic and
    are exactly what must NOT be part of the warmed reusable prefix.
    """
    from core.prompt_manifest import stable_prefix, contract_delta
    shape = _representative_shape(family, language)
    system = stable_prefix(language_directive=language_directive) + "\n\n" + \
        contract_delta(shape, language=language).render()
    return [{"role": "system", "content": system},
            {"role": "user", "content": _FAMILY_USER_PROMPT}]


class FamilyPrewarmMode(str, Enum):
    OFF = "OFF"
    CONCISE_ONLY = "CONCISE_ONLY"
    BACKGROUND_FAMILIES = "BACKGROUND_FAMILIES"
    BEFORE_TEXT_READY_CONCISE = "BEFORE_TEXT_READY_CONCISE"


DEFAULT_FAMILY_MODE = FamilyPrewarmMode.BACKGROUND_FAMILIES


def parse_family_mode(value) -> FamilyPrewarmMode:
    if isinstance(value, FamilyPrewarmMode):
        return value
    raw = str(value or "").strip().upper().replace("-", "_")
    try:
        return FamilyPrewarmMode(raw)
    except ValueError:
        return DEFAULT_FAMILY_MODE


class FamilyState(str, Enum):
    NOT_REQUESTED = "NOT_REQUESTED"
    QUEUED = "QUEUED"
    WAITING_FOR_GOVERNOR = "WAITING_FOR_GOVERNOR"
    RUNNING = "RUNNING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INVALIDATED = "INVALIDATED"


@dataclass
class FamilyRecord:
    """Content-free metrics for ONE family prewarm attempt."""

    family: str
    model: str = ""
    prompt_fingerprint: str = ""
    compatibility_identity: str = ""
    num_ctx: int = 0
    state: FamilyState = FamilyState.NOT_REQUESTED
    queue_wait_ms: float | None = None
    load_duration_ms: float | None = None
    prompt_eval_ms: float | None = None
    first_token_ms: float | None = None
    total_ms: float | None = None
    success: bool = False
    invalidated_reason: str | None = None
    power_profile: str = "UNKNOWN"
    # M59.1 — content-free profile-parity provenance for this attempt.
    live_runner_identity: str = ""
    prewarm_runner_identity: str = ""
    runner_parity: bool | None = None

    def snapshot(self) -> dict:
        return {
            "family": self.family, "model": self.model, "state": self.state.value,
            "prompt_fingerprint": self.prompt_fingerprint,
            "compatibility_identity": self.compatibility_identity,
            "num_ctx": self.num_ctx, "queue_wait_ms": self.queue_wait_ms,
            "load_duration_ms": self.load_duration_ms,
            "prompt_eval_ms": self.prompt_eval_ms,
            "first_token_ms": self.first_token_ms, "total_ms": self.total_ms,
            "success": self.success, "invalidated_reason": self.invalidated_reason,
            "power_profile": self.power_profile,
            "live_runner_identity": self.live_runner_identity,
            "prewarm_runner_identity": self.prewarm_runner_identity,
            "runner_parity": self.runner_parity,
        }


async def run_family_prewarm(
    family: ContractFamily,
    *,
    model: str,
    num_ctx: int,
    keep_alive: str,
    timeout_s: float,
    language: str = "es",
    language_directive: str = "",
    compatibility_identity: str = "",
    prompt_fingerprint: str = "",
    power_profile: str = "UNKNOWN",
    cancellation=None,
    client=None,
    temperature: float = 0.0,
    num_predict: int | None = None,
    options_extra: dict | None = None,
) -> FamilyRecord:
    """Run ONE bounded native /api/chat prewarm for a family, over the real transport
    with the real stable prefix. Never raises except CancelledError.

    ``temperature`` / ``num_predict`` / ``options_extra`` (M59.1.1) let the caller pass
    the sampling posture DERIVED from the family's live generation budget, so the
    prewarm warms the same runner+prefix the live turn uses instead of a hand-written
    ``temperature=0.0`` set. When omitted, the historical minimal defaults apply."""
    from core.ollama_native import NativeTransportError, chat_stream
    from core.turn_budget import StageTimeouts, TurnBudget

    rec = FamilyRecord(family=family.value, model=model, num_ctx=int(num_ctx),
                       prompt_fingerprint=prompt_fingerprint,
                       compatibility_identity=compatibility_identity,
                       state=FamilyState.RUNNING, power_profile=power_profile)
    messages = family_prewarm_messages(family, language_directive=language_directive,
                                       language=language)
    t0 = time.monotonic()
    budget = TurnBudget(total_s=timeout_s)
    timeouts = StageTimeouts(connect_s=5.0, first_token_s=timeout_s, idle_s=10.0,
                             total_s=timeout_s)
    cap = int(num_predict) if num_predict is not None else _FAMILY_NUM_PREDICT
    try:
        async for chunk in chat_stream(
            model=model, messages=messages, think=False,
            max_tokens=cap, temperature=float(temperature), budget=budget,
            timeouts=timeouts, ctx=int(num_ctx), keep_alive=keep_alive,
            cancellation=cancellation, client=client,
            options_extra=options_extra,
        ):
            if chunk.content and rec.first_token_ms is None:
                rec.first_token_ms = round((time.monotonic() - t0) * 1000.0, 1)
                rec.success = True
            if chunk.done:
                if chunk.load_duration is not None:
                    rec.load_duration_ms = round(chunk.load_duration / 1e6, 1)
                if chunk.prompt_eval_duration is not None:
                    rec.prompt_eval_ms = round(chunk.prompt_eval_duration / 1e6, 1)
                break
    except NativeTransportError as exc:
        rec.state, rec.invalidated_reason = FamilyState.FAILED, exc.reason
    except asyncio.CancelledError:
        rec.state, rec.total_ms = FamilyState.CANCELLED, \
            round((time.monotonic() - t0) * 1000.0, 1)
        raise
    except Exception as exc:  # noqa: BLE001 — a prewarm never crashes boot
        rec.state, rec.invalidated_reason = FamilyState.FAILED, type(exc).__name__
    rec.total_ms = round((time.monotonic() - t0) * 1000.0, 1)
    if rec.state is FamilyState.RUNNING:
        rec.state = FamilyState.READY if rec.success else FamilyState.DEGRADED
    return rec


class FamilyPrewarm:
    """Owns the family-prewarm lifecycle: mode, per-family state, once-per-identity
    guard, governor priority, cancellation. Every collaborator is injectable."""

    def __init__(self, *, model: str, mode: FamilyPrewarmMode = DEFAULT_FAMILY_MODE,
                 num_ctx: int = 2048, keep_alive: str = "30m",
                 timeout_s: float = 45.0, language: str = "es",
                 language_directive: str = "",
                 runner: Callable | None = None,
                 governor=None,
                 is_stopping: Callable[[], bool] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.model = model
        self.mode = mode
        self.num_ctx = int(num_ctx)
        self.keep_alive = keep_alive
        self.timeout_s = max(1.0, float(timeout_s))
        self.language = language
        self.language_directive = language_directive
        self._runner = runner or run_family_prewarm
        self._governor = governor
        self._is_stopping = is_stopping or (lambda: False)
        self._clock = clock
        self.states: dict[ContractFamily, FamilyState] = {
            f: FamilyState.NOT_REQUESTED for f in PREWARMABLE_FAMILIES}
        self.records: dict[ContractFamily, FamilyRecord] = {}
        self._warmed_identity: str | None = None
        # Once-per-(family, identity): a genuine identity change (language/model/ctx)
        # re-arms a family; a restart loop on the same identity cannot re-warm.
        self._done: set[tuple[str, str]] = set()
        self.attempts = 0
        self.successes = 0
        self.cancellations = 0
        self.stale_fingerprints = 0
        self.last_family: str | None = None
        self._task: asyncio.Task | None = None

    # ── identity ─────────────────────────────────────────────────────────────
    def _manifest(self, shape=None):
        from core.prompt_manifest import build_manifest
        from core.tool_schema import EMPTY_TOOL_SCHEMA_FINGERPRINT
        return build_manifest(
            model=self.model, transport="native", think=False, num_ctx=self.num_ctx,
            language=self.language, language_directive=self.language_directive,
            authority_mode="STANDARD", scope_fingerprint="",
            tool_schema_fingerprint=EMPTY_TOOL_SCHEMA_FINGERPRINT, shape=shape)

    def warmed_identity(self) -> str | None:
        """The compatibility identity of the last successfully warmed prefix, or None.
        The observer uses this to detect a CONFIG_MISMATCH — a warmed metric from a
        different configuration is never counted as reuse."""
        return self._warmed_identity

    def planned_families(self) -> tuple[ContractFamily, ...]:
        """Which families this mode warms, in priority order. CONCISE always first
        (it warms the shared prefix), EXPLANATORY only in BACKGROUND_FAMILIES."""
        if self.mode is FamilyPrewarmMode.OFF:
            return ()
        if self.mode in (FamilyPrewarmMode.CONCISE_ONLY,
                         FamilyPrewarmMode.BEFORE_TEXT_READY_CONCISE):
            return (ContractFamily.CONCISE,)
        return (ContractFamily.CONCISE, ContractFamily.EXPLANATORY)

    # ── one family ───────────────────────────────────────────────────────────
    async def warm_family(self, family: ContractFamily, *, force: bool = False,
                          cancellation=None, power_profile: str = "UNKNOWN"
                          ) -> FamilyRecord:
        """Warm ONE family, bounded and guarded. Never raises except CancelledError."""
        rec = FamilyRecord(family=family.value, model=self.model,
                           num_ctx=self.num_ctx, power_profile=power_profile)
        if family not in PREWARMABLE_FAMILIES:
            rec.state = FamilyState.NOT_REQUESTED
            return rec
        if self.mode is FamilyPrewarmMode.OFF and not force:
            rec.state = FamilyState.NOT_REQUESTED
            return rec
        if self._is_stopping():
            rec.state = FamilyState.CANCELLED
            rec.invalidated_reason = "stopping"
            return rec
        if not self.model:
            rec.state = FamilyState.FAILED
            rec.invalidated_reason = "no_model"
            return rec
        shape = _representative_shape(family, self.language)
        manifest = self._manifest(shape)
        identity = manifest.compatibility_identity()
        key = (family.value, identity)
        rec.compatibility_identity = identity
        rec.prompt_fingerprint = manifest.stable_prefix_fingerprint
        if key in self._done and not force:
            rec.state = FamilyState.READY  # already warm for this identity
            rec.success = True
            return rec
        # ── governor slot (lowest priority) ──
        rec.state = FamilyState.WAITING_FOR_GOVERNOR
        self.states[family] = FamilyState.WAITING_FOR_GOVERNOR
        queue_t0 = self._clock()
        slot_cm = self._acquire_slot()
        try:
            async with slot_cm:
                rec.queue_wait_ms = round((self._clock() - queue_t0) * 1000.0, 1)
                if self._is_stopping():
                    rec.state = FamilyState.CANCELLED
                    self.states[family] = FamilyState.CANCELLED
                    return rec
                self.states[family] = FamilyState.RUNNING
                self.attempts += 1
                self.last_family = family.value
                sampling = self._derive_sampling(family, shape)
                run = await self._runner(
                    family, model=self.model, num_ctx=self.num_ctx,
                    keep_alive=self.keep_alive, timeout_s=self.timeout_s,
                    language=self.language,
                    language_directive=self.language_directive,
                    compatibility_identity=identity,
                    prompt_fingerprint=manifest.stable_prefix_fingerprint,
                    power_profile=power_profile, cancellation=cancellation,
                    temperature=sampling["temperature"],
                    num_predict=sampling["num_predict"],
                    options_extra=sampling["options_extra"])
                # preserve queue wait measured here
                run.queue_wait_ms = rec.queue_wait_ms
                run.compatibility_identity = identity
                run.prompt_fingerprint = manifest.stable_prefix_fingerprint
                run.live_runner_identity = sampling["live_runner_identity"]
                run.prewarm_runner_identity = sampling["prewarm_runner_identity"]
                run.runner_parity = sampling["runner_parity"]
                rec = run
        except asyncio.CancelledError:
            self.cancellations += 1
            self.states[family] = FamilyState.CANCELLED
            raise
        except Exception as exc:  # noqa: BLE001
            rec.state = FamilyState.FAILED
            rec.invalidated_reason = type(exc).__name__
        self.states[family] = rec.state
        self.records[family] = rec
        if rec.success and rec.state is FamilyState.READY:
            self.successes += 1
            self._done.add(key)
            self._warmed_identity = identity
        return rec

    def _derive_sampling(self, family: ContractFamily, shape) -> dict:
        """Derive the prewarm sampling posture from the family's LIVE generation
        budget (M59.1.1). Never raises — a derivation failure degrades to the historical
        minimal defaults rather than breaking a best-effort prewarm."""
        default = {"temperature": 0.0, "num_predict": None, "options_extra": None,
                   "live_runner_identity": "", "prewarm_runner_identity": "",
                   "runner_parity": None}
        try:
            from core.inference_profile import profile_compatibility, profiles_for_shape
            from core.tool_schema import EMPTY_TOOL_SCHEMA_FINGERPRINT
            live, prewarm = profiles_for_shape(
                shape, model=self.model, num_ctx=self.num_ctx, transport="native",
                think=False, language=self.language,
                language_directive=self.language_directive,
                tool_schema_fingerprint=EMPTY_TOOL_SCHEMA_FINGERPRINT)
            verdict = profile_compatibility(prewarm, live)
            return {
                "temperature": prewarm.generation.temperature,
                "num_predict": prewarm.generation.num_predict,
                "options_extra": prewarm.generation.transport_options_extra(),
                "live_runner_identity": live.runner.fingerprint(),
                "prewarm_runner_identity": prewarm.runner.fingerprint(),
                "runner_parity": verdict.runner_compatible,
            }
        except Exception:  # noqa: BLE001 — never break a best-effort prewarm
            return default

    def _acquire_slot(self):
        """The governor slot context manager (lowest priority), or a no-op when no
        governor is wired (tests / headless)."""
        if self._governor is None:
            return _NullSlot()
        try:
            from core.residency_governor import Priority
            return self._governor.slot(role="fast", priority=Priority.PREWARM,
                                       reason="family_prewarm")
        except Exception:  # noqa: BLE001
            return _NullSlot()

    # ── all planned families ─────────────────────────────────────────────────
    async def warm_planned(self, *, power_profile: str = "UNKNOWN",
                           cancellation=None) -> list[FamilyRecord]:
        """Warm every planned family in priority order. CONCISE first; a failed
        optional family never blocks the others and never makes chat unavailable."""
        out: list[FamilyRecord] = []
        for family in self.planned_families():
            if self._is_stopping():
                break
            out.append(await self.warm_family(
                family, cancellation=cancellation, power_profile=power_profile))
        return out

    def start_background(self, *, power_profile: str = "UNKNOWN") -> "asyncio.Task | None":
        """Fire-and-supervise the planned families. Returns the task (or None when the
        mode/state refuses). The prompt opens immediately; readiness stays truthful."""
        if self.mode is FamilyPrewarmMode.OFF or self._is_stopping():
            return None
        if self._task is not None and not self._task.done():
            return self._task

        async def _supervised() -> None:
            try:
                await self.warm_planned(power_profile=power_profile)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — never escape into the event loop
                pass

        self._task = asyncio.ensure_future(_supervised())
        return self._task

    async def cancel(self) -> None:
        """Cancel an in-flight background family prewarm and await teardown. Bounded."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(
                task, return_exceptions=True)), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    def note_invalidation(self, reason: str) -> None:
        """A model/ctx/language change invalidates every warmed family. Marks records
        INVALIDATED and re-arms so a bounded rewarm may run when policy allows."""
        self.stale_fingerprints += len(self._done)
        self._done.clear()
        self._warmed_identity = None
        for family in list(self.records):
            self.records[family].state = FamilyState.INVALIDATED
            self.records[family].invalidated_reason = reason
            self.states[family] = FamilyState.INVALIDATED

    def note_config(self, *, model: str | None = None, num_ctx: int | None = None,
                    language: str | None = None,
                    language_directive: str | None = None) -> None:
        """Update the warmed configuration; a genuine change invalidates warm state."""
        changed = False
        if model and model != self.model:
            self.model, changed = model, True
        if num_ctx and int(num_ctx) != self.num_ctx:
            self.num_ctx, changed = int(num_ctx), True
        if language and language != self.language:
            self.language, changed = language, True
        if language_directive is not None and language_directive != self.language_directive:
            self.language_directive, changed = language_directive, True
        if changed:
            self.note_invalidation("config_changed")

    def snapshot(self) -> dict:
        return {
            "mode": self.mode.value,
            "model": self.model,
            "num_ctx": self.num_ctx,
            "family_states": {f.value: s.value for f, s in self.states.items()},
            "attempts": self.attempts,
            "successes": self.successes,
            "cancellations": self.cancellations,
            "stale_fingerprints": self.stale_fingerprints,
            "last_family": self.last_family,
            "warmed_identity": self._warmed_identity,
            "runner_parity": (self.records.get(ContractFamily.CONCISE).runner_parity
                              if ContractFamily.CONCISE in self.records else None),
            "live_runner_identity": (
                self.records.get(ContractFamily.CONCISE).live_runner_identity
                if ContractFamily.CONCISE in self.records else ""),
            "prewarm_runner_identity": (
                self.records.get(ContractFamily.CONCISE).prewarm_runner_identity
                if ContractFamily.CONCISE in self.records else ""),
            "last_first_token_ms": (self.records.get(
                ContractFamily.CONCISE).first_token_ms
                if ContractFamily.CONCISE in self.records else None),
            "last_prompt_eval_ms": (self.records.get(
                ContractFamily.CONCISE).prompt_eval_ms
                if ContractFamily.CONCISE in self.records else None),
            "records": {f.value: r.snapshot() for f, r in self.records.items()},
        }


class _NullSlot:
    """A no-op async context manager used when no residency governor is wired."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


# ── Process-global singleton ─────────────────────────────────────────────────
_family_prewarm: FamilyPrewarm | None = None


def get_family_prewarm() -> FamilyPrewarm:
    """The process family-prewarm, built from operator config on first use."""
    global _family_prewarm
    if _family_prewarm is None:
        model, mode, num_ctx, keep_alive, timeout_s = "", DEFAULT_FAMILY_MODE, 2048, \
            "30m", 45.0
        try:
            from core.config import settings
            mode = parse_family_mode(getattr(settings, "family_prewarm_mode",
                                             DEFAULT_FAMILY_MODE.value))
            keep_alive = getattr(settings, "fast_keep_alive", "30m")
            timeout_s = float(getattr(settings, "fast_prewarm_timeout_s", 45.0))
            model = (getattr(settings, "fast_model", "") or "").strip()
        except Exception:  # noqa: BLE001
            pass
        if not model:
            try:
                from core.model_router import ModelRole, model_for_role
                model = model_for_role(ModelRole.FAST) or ""
            except Exception:  # noqa: BLE001
                model = ""
        try:
            from core.fast_prewarm import resolve_fast_context
            num_ctx = int(resolve_fast_context())
        except Exception:  # noqa: BLE001
            num_ctx = 2048
        stopping = None
        governor = None
        try:
            from core.lifecycle import get_lifecycle
            stopping = get_lifecycle().is_stopping
        except Exception:  # noqa: BLE001
            pass
        try:
            from core.residency_governor import get_governor
            governor = get_governor()
        except Exception:  # noqa: BLE001
            governor = None
        _family_prewarm = FamilyPrewarm(
            model=model, mode=mode, num_ctx=num_ctx, keep_alive=keep_alive,
            timeout_s=timeout_s, governor=governor, is_stopping=stopping)
    return _family_prewarm


def reset_family_prewarm(instance: FamilyPrewarm | None = None) -> None:
    """Tests / a fresh process."""
    global _family_prewarm
    _family_prewarm = instance
