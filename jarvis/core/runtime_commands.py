"""core/runtime_commands.py — V69 M59.5: M59 runtime status & bounded actions.

M59 built four runtime facts the operator had no way to ask about: what the session
actually warmed and what evidence exists for it, what the prewarm did, what background
compaction accepted or rejected, and which barge-in backend is really running. This is
the operator surface for exactly those — and nothing else.

SECURITY POSTURE (identical to ``core.response_commands``, deliberately)
------------------------------------------------------------------------
  * EXACT-MATCH ALLOWLIST — a command is recognised only when the WHOLE line matches
    a known alias. ``/rewarm concise`` and ``/qualify quick`` are literal aliases, NOT
    a verb plus an argument: there is no argument parsing anywhere in this module, so
    no value, path, model name, scope or host setting can arrive from free text;
  * no shell, no subprocess, no filesystem write, no environment mutation, no Ollama
    configuration change, no Git operation, no semantic-collection write;
  * the five status commands are strictly READ-ONLY;
  * the three bounded actions each do ONE thing with an existing, already-bounded and
    already-governed subsystem: ask the rewarm policy, ask the compaction scheduler
    (which runs its quality gate), or run the deterministic qualification matrix;
  * everything is lifecycle-aware (nothing starts after STOPPING), governor-aware (the
    work goes through the residency governor at its normal priority) and cancellable
    (each action is a bounded await the caller may cancel);
  * every rendered line is CONTENT-FREE: states, counts, fingerprints, milliseconds.
    Never a prompt, an answer, a tool argument, a key value or a secret.

A command that is not on the list is not a command — it is an ordinary user turn.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class RuntimeCommand(str, Enum):
    """The complete, closed set of M59 runtime commands."""

    # read-only status
    WARMTH_STATUS = "WARMTH_STATUS"
    PREWARM_STATUS = "PREWARM_STATUS"
    COMPACTION_STATUS = "COMPACTION_STATUS"
    BARGE_STATUS = "BARGE_STATUS"
    RUNTIME_QUALIFICATION = "RUNTIME_QUALIFICATION"
    # bounded actions
    REWARM_CONCISE = "REWARM_CONCISE"
    COMPACT_NOW = "COMPACT_NOW"
    QUALIFY_QUICK = "QUALIFY_QUICK"


READ_ONLY: frozenset[RuntimeCommand] = frozenset({
    RuntimeCommand.WARMTH_STATUS, RuntimeCommand.PREWARM_STATUS,
    RuntimeCommand.COMPACTION_STATUS, RuntimeCommand.BARGE_STATUS,
    RuntimeCommand.RUNTIME_QUALIFICATION,
})
BOUNDED_ACTIONS: frozenset[RuntimeCommand] = frozenset({
    RuntimeCommand.REWARM_CONCISE, RuntimeCommand.COMPACT_NOW,
    RuntimeCommand.QUALIFY_QUICK,
})

# The exact aliases. Spanish forms are listed alongside the English ones because the
# runtime is Spanish-first; each is still a WHOLE-LINE exact match.
_ALIASES: dict[RuntimeCommand, tuple[str, ...]] = {
    RuntimeCommand.WARMTH_STATUS: ("/warmth-status", "/estado-calentamiento"),
    RuntimeCommand.PREWARM_STATUS: ("/prewarm-status", "/estado-precalentado"),
    RuntimeCommand.COMPACTION_STATUS: ("/compaction-status", "/estado-compactacion",
                                       "/estado-compactación"),
    RuntimeCommand.BARGE_STATUS: ("/barge-status", "/estado-interrupcion",
                                  "/estado-interrupción"),
    RuntimeCommand.RUNTIME_QUALIFICATION: ("/runtime-qualification",
                                           "/cualificacion-runtime",
                                           "/cualificación-runtime"),
    RuntimeCommand.REWARM_CONCISE: ("/rewarm concise", "/recalentar concise"),
    RuntimeCommand.COMPACT_NOW: ("/compact-now", "/compactar-ahora"),
    RuntimeCommand.QUALIFY_QUICK: ("/qualify quick", "/cualificar quick"),
}
_EXACT: dict[str, RuntimeCommand] = {
    alias: cmd for cmd, aliases in _ALIASES.items() for alias in aliases
}


@dataclass(frozen=True)
class ParsedRuntimeCommand:
    """One recognised command. Carries no operator text beyond the matched alias."""

    command: RuntimeCommand
    alias: str

    @property
    def read_only(self) -> bool:
        return self.command in READ_ONLY

    @property
    def bounded_action(self) -> bool:
        return self.command in BOUNDED_ACTIONS


def parse_runtime_command(text: str) -> ParsedRuntimeCommand | None:
    """Return the command when the WHOLE line is a known alias, else ``None``.

    Case-insensitive; interior whitespace is normalised so ``/rewarm  concise`` still
    matches its ONE alias. There is deliberately no prefix matching and no argument
    capture: ``/qualify quick --live`` is not a command, it is an ordinary turn.
    """
    raw = " ".join((text or "").strip().lower().split())
    if not raw or not raw.startswith("/"):
        return None
    cmd = _EXACT.get(raw)
    return ParsedRuntimeCommand(command=cmd, alias=raw) if cmd is not None else None


def known_runtime_aliases() -> tuple[str, ...]:
    """The complete allowlist, for help output and tests."""
    return tuple(sorted(_EXACT))


# ══════════════════════════════════════════════════════════════════════════════
#  Rendering — bounded, localized, content-free
# ══════════════════════════════════════════════════════════════════════════════
def _en(language: str) -> bool:
    return str(language or "es").lower().startswith("en")


def _fmt(value) -> str:
    """Render one metric value. ASCII-safe by construction: the live JARVIS console is
    cp1252 (M58 already had to strip a non-ASCII header), so a panel must never carry
    a character the terminal cannot encode."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _panel(title: str, rows: list[tuple[str, object]], *, note: str = "") -> str:
    """The house panel style (see ``core.response_status``): an uppercase ASCII title
    and ``  key=value`` rows. Metric KEYS stay in English — they are identifiers, not
    prose; only the title and the trailing note are localized."""
    lines = [str(title)]
    lines += [f"  {k}={_fmt(v)}" for k, v in rows]
    if note:
        lines.append(f"  ({note})")
    return "\n".join(lines)


def _warmth_snapshot() -> dict:
    try:
        from core.warmth_runtime import get_warmth_runtime
        return get_warmth_runtime().snapshot()
    except Exception:  # noqa: BLE001
        return {}


def render_warmth_status(language: str = "es", snapshot: dict | None = None) -> str:
    """Session warmth: what was warmed, and what LIVE evidence exists for it."""
    s = snapshot if snapshot is not None else _warmth_snapshot()
    english = _en(language)
    rows = [
        ("state", s.get("session_state")),
        ("reuse_state", s.get("reuse_state")),
        ("family", s.get("active_family")),
        ("observations", s.get("observation_count")),
        ("invalidations", s.get("invalidation_count")),
        ("last_invalidation", s.get("last_invalidation_reason")),
        ("rewarm_attempts", s.get("predictive_rewarm_attempts")),
        ("rewarm_successes", s.get("predictive_rewarm_successes")),
        ("rewarm_deferred", s.get("rewarm_deferred")),
        ("rewarm_preemptions", s.get("rewarm_preemptions")),
        ("cooldown_s", s.get("cooldown_remaining")),
        ("rewarm_pending", s.get("rewarm_pending")),
    ]
    note = ("a prewarm alone is PREWARMED, never observed reuse" if english
            else "un precalentado por si solo es PREWARMED, nunca reutilizacion "
                 "observada")
    return _panel("SESSION WARMTH" if english else "CALENTAMIENTO DE SESION",
                  rows, note=note)


def render_prewarm_status(language: str = "es", snapshot: dict | None = None) -> str:
    """Contract-family prewarm: mode, per-family state and profile parity."""
    if snapshot is None:
        try:
            from core.contract_family import get_family_prewarm
            snapshot = get_family_prewarm().snapshot()
        except Exception:  # noqa: BLE001
            snapshot = {}
    s = snapshot or {}
    english = _en(language)
    states = s.get("family_states") or {}
    rows = [
        ("mode", s.get("mode")),
        ("model", s.get("model")),
        ("num_ctx", s.get("num_ctx")),
        ("families", " ".join(f"{k}:{v}" for k, v in sorted(states.items())) or None),
        ("attempts", s.get("attempts")),
        ("successes", s.get("successes")),
        ("cancellations", s.get("cancellations")),
        ("stale_fingerprints", s.get("stale_fingerprints")),
        ("runner_parity", s.get("runner_parity")),
        ("warmed_identity", s.get("warmed_identity")),
        ("first_token_ms", s.get("last_first_token_ms")),
        ("prompt_eval_ms", s.get("last_prompt_eval_ms")),
    ]
    return _panel("FAMILY PREWARM" if english else "PRECALENTADO DE FAMILIAS", rows)


def render_compaction_status(language: str = "es", snapshot: dict | None = None) -> str:
    """Background compaction: governor wait, quality gate accept/reject accounting."""
    if snapshot is None:
        try:
            from core.compaction_scheduler import get_compaction_scheduler
            snapshot = get_compaction_scheduler().snapshot()
        except Exception:  # noqa: BLE001
            snapshot = {}
    s = snapshot or {}
    english = _en(language)
    reasons = s.get("rejection_reasons") or {}
    rows = [
        ("state", s.get("state")),
        ("quality", s.get("quality_state")),
        ("scheduled", s.get("scheduled")),
        ("completed", s.get("completed")),
        ("cancelled_for_user", s.get("cancelled_for_user")),
        ("preemptions", s.get("preemptions")),
        ("candidates", s.get("candidates")),
        ("accepted", s.get("accepted")),
        ("rejected", s.get("rejected")),
        ("reject_reasons",
         " ".join(f"{k}:{v}" for k, v in sorted(reasons.items())) or None),
        ("validation_failures", s.get("validation_failures")),
        ("governor_wait_ms", s.get("governor_wait_ms")),
        ("digest_version", s.get("digest_version")),
        ("tokens_saved", s.get("context_tokens_saved")),
    ]
    note = ("never writes semantic memory automatically" if english
            else "nunca escribe memoria semantica automaticamente")
    return _panel("BACKGROUND COMPACTION" if english
                  else "COMPACTACION EN SEGUNDO PLANO", rows, note=note)


def render_barge_status(language: str = "es", snapshot: dict | None = None) -> str:
    """Barge-in: which console-local backend runs, and why it is not a better one."""
    if snapshot is None:
        try:
            from core.barge_in import get_barge_in_controller
            snapshot = get_barge_in_controller().snapshot()
        except Exception:  # noqa: BLE001
            snapshot = {}
    s = snapshot or {}
    english = _en(language)
    rows = [
        ("selected_backend", s.get("selected_backend")),
        ("mode", s.get("mode")),
        ("portable_backend_available", s.get("portable_backend_available")),
        ("fallback_reason", s.get("fallback_reason")),
        ("supported", s.get("supported")),
        ("active_interruptions", s.get("active_interruptions")),
        ("command_interruptions", s.get("command_interruptions")),
        ("cancellation_latency_ms", s.get("cancellation_latency_ms")),
        ("terminal_restore_failures", s.get("terminal_restore_failures")),
        ("orphan_reader_count", s.get("orphan_reader_count")),
        ("arm_failures", s.get("arm_failures")),
    ]
    note = ("console-local only: no global hook, no key value is ever stored"
            if english else
            "solo consola local: sin hook global, nunca se guarda el valor de tecla")
    return _panel("ACTIVE-CONSOLE BARGE-IN" if english
                  else "INTERRUPCION DE CONSOLA", rows, note=note)


def render_runtime_qualification(language: str = "es",
                                 result: dict | None = None) -> str:
    """The deterministic qualification matrix verdict (no server, no host change)."""
    r = result if result is not None else run_quick_qualification()
    english = _en(language)
    rows = [
        ("verdict", r.get("verdict")),
        ("mode", r.get("mode")),
        ("cases", r.get("total")),
        ("passed", r.get("passed")),
        ("failed", r.get("failed")),
    ]
    failed = r.get("failed_cases") or []
    if failed:
        rows.append(("failed_cases", " ".join(failed[:6])))
    if r.get("error"):
        rows.append(("error", r.get("error")))
    note = ("deterministic only: no live server, no Git, no host mutation"
            if english else
            "solo determinista: sin servidor vivo, sin Git, sin cambios en el host")
    return _panel("RUNTIME QUALIFICATION" if english
                  else "CUALIFICACION DE RUNTIME", rows, note=note)


def run_quick_qualification() -> dict:
    """Run the bounded DETERMINISTIC qualification matrix and summarise it.

    This is the ``/qualify quick`` path: it evaluates the M59 invariants in-process.
    It starts no server, reads no model, writes no file, touches no Git state and
    mutates no host or semantic collection.
    """
    try:
        from core.qualification import (
            CaseVerdict, aggregate_verdict, run_deterministic_matrix,
        )
        cases = run_deterministic_matrix()
        return {
            "mode": "deterministic",
            "verdict": aggregate_verdict(cases, live_requested=False),
            "total": len(cases),
            "passed": sum(1 for c in cases if c.verdict is CaseVerdict.PASS),
            "failed": sum(1 for c in cases if c.verdict is CaseVerdict.FAIL),
            "failed_cases": [c.case_id for c in cases
                             if c.verdict is CaseVerdict.FAIL],
        }
    except Exception as exc:  # noqa: BLE001 — a status command never raises
        return {"mode": "deterministic", "verdict": "INSUFFICIENT_EVIDENCE",
                "total": 0, "passed": 0, "failed": 0, "failed_cases": [],
                "error": type(exc).__name__}


# ══════════════════════════════════════════════════════════════════════════════
#  Bounded actions
# ══════════════════════════════════════════════════════════════════════════════
_STOPPING = ("El runtime se está deteniendo; no se inicia trabajo nuevo.",
             "The runtime is stopping; no new work is started.")


def _is_stopping() -> bool:
    try:
        from core.lifecycle import get_lifecycle
        return bool(get_lifecycle().is_stopping())
    except Exception:  # noqa: BLE001
        return False


def request_rewarm(family: str = "CONCISE") -> dict:
    """The ``/rewarm concise`` decision. Bounded, governed and always refusable.

    An EXPLICIT operator request re-arms the family's attempt counter first — the same
    thing a genuine invalidation does — so a previously exhausted family can be asked
    again. It does NOT bypass any safety refusal: STOPPING, battery policy, an active
    FAST turn and a requested embedding all still outrank it, and the decision they
    produce is reported verbatim instead of being retried in a loop.
    """
    try:
        from core.session_warmth import RewarmTrigger
        from core.warmth_runtime import get_warmth_runtime
        wr = get_warmth_runtime()
        wr.policy.note_invalidation(family)
        decision = wr.evaluate(RewarmTrigger.PREVIOUS_PREWARM_CANCELLED, family=family)
        scheduled = False
        if decision.should_schedule:
            scheduled = wr.schedule_rewarm(
                RewarmTrigger.PREVIOUS_PREWARM_CANCELLED, family=family) is not None
        out = decision.snapshot()
        out["scheduled"] = scheduled
        return out
    except Exception as exc:  # noqa: BLE001
        return {"action": "SKIP", "reason": type(exc).__name__, "family": family,
                "scheduled": False, "cooldown_remaining_s": 0.0}


async def run_compact_now(llm=None) -> dict:
    """The ``/compact-now`` action: ONE bounded compaction pass, right now.

    It routes through the EXISTING scheduler, so it takes the governor slot at
    BACKGROUND_COMPACTION priority, runs the M59.4 quality gate on every proposed item
    and writes only the in-memory conversation digest. It NEVER writes semantic memory:
    the scheduler has no semantic-write path, and this command adds none.

    The idle-only gates (minimum turns, context pressure, cooldown) are waived —
    that is exactly what "now" means — but every SAFETY gate is honoured as measured:
    an active turn, HITL, an effectful tool, active answer speech, a high-priority
    embedding, a non-operational lifecycle or a battery power policy all refuse.
    """
    if _is_stopping():
        return {"state": "REFUSED", "reason": "stopping"}
    try:
        from core.compaction_scheduler import (
            build_conditions_from_runtime, get_compaction_scheduler,
        )
        from core.config import settings
    except Exception as exc:  # noqa: BLE001
        return {"state": "REFUSED", "reason": type(exc).__name__}
    sched = get_compaction_scheduler()
    history = list(getattr(llm, "history", []) or []) if llm is not None else []
    budget = int(getattr(settings, "fast_context", 2048) or 2048)
    conds = build_conditions_from_runtime(history, context_budget=budget)
    # Waive the three idle-TIMING gates (minimum turns, context pressure, cooldown) —
    # that is exactly what "now" means — and leave every SAFETY field as measured.
    forced = replace(conds,
                     completed_turns=max(conds.completed_turns, sched.min_turns),
                     context_pressure=max(conds.context_pressure,
                                          sched.pressure_threshold),
                     cooldown_expired=True)
    # With the timing gates satisfied, the first remaining block can only be a real
    # safety condition, and it is reported verbatim instead of being overridden.
    safety_blocked = forced.block_reason(min_turns=0, pressure_threshold=0.0)
    if safety_blocked:
        return {"state": "REFUSED", "reason": safety_blocked}
    state = await sched.maybe_run(history, forced)
    snap = sched.snapshot()
    return {"state": getattr(state, "value", str(state)),
            "candidates": snap.get("candidates"), "accepted": snap.get("accepted"),
            "rejected": snap.get("rejected"),
            "quality": snap.get("quality_state"),
            "duration_ms": snap.get("last_duration_ms"),
            "digest_version": snap.get("digest_version")}


# ── Localized confirmations (never echo operator text) ────────────────────────
_REWARM_SCHEDULED = ("Recalentado programado (familia {f}).",
                     "Rewarm scheduled (family {f}).")
_REWARM_REFUSED = ("Recalentado no programado: {r}.",
                   "Rewarm not scheduled: {r}.")
_COMPACT_REFUSED = ("Compactación no ejecutada: {r}.",
                    "Compaction not run: {r}.")
_COMPACT_DONE = ("Compactación {s}: {a} aceptados / {c} candidatos (calidad {q}).",
                 "Compaction {s}: {a} accepted / {c} candidates (quality {q}).")


def describe_rewarm(result: dict, *, language: str = "es") -> str:
    english = _en(language)
    if result.get("scheduled"):
        pair = _REWARM_SCHEDULED
        return (pair[1] if english else pair[0]).format(
            f=str(result.get("family") or "default"))
    pair = _REWARM_REFUSED
    return (pair[1] if english else pair[0]).format(
        r=str(result.get("reason") or "unknown"))


def describe_compaction(result: dict, *, language: str = "es") -> str:
    english = _en(language)
    if str(result.get("state")) == "REFUSED":
        pair = _COMPACT_REFUSED
        return (pair[1] if english else pair[0]).format(
            r=str(result.get("reason") or "unknown"))
    pair = _COMPACT_DONE
    return (pair[1] if english else pair[0]).format(
        s=str(result.get("state")), a=_fmt(result.get("accepted")),
        c=_fmt(result.get("candidates")), q=_fmt(result.get("quality")))


async def apply_runtime_command(parsed: ParsedRuntimeCommand, *, language: str = "es",
                                llm=None) -> str:
    """Apply ONE parsed runtime command and return the operator-facing text.

    Bounded and never raising. Read-only commands render a panel; the three bounded
    actions each perform exactly one already-governed operation. Nothing here touches
    the host, the model configuration, the Ollama posture, Git or the filesystem.
    """
    cmd = parsed.command
    try:
        if cmd is RuntimeCommand.WARMTH_STATUS:
            return render_warmth_status(language)
        if cmd is RuntimeCommand.PREWARM_STATUS:
            return render_prewarm_status(language)
        if cmd is RuntimeCommand.COMPACTION_STATUS:
            return render_compaction_status(language)
        if cmd is RuntimeCommand.BARGE_STATUS:
            return render_barge_status(language)
        if cmd is RuntimeCommand.RUNTIME_QUALIFICATION:
            return render_runtime_qualification(language)
        if cmd is RuntimeCommand.QUALIFY_QUICK:
            return render_runtime_qualification(language, run_quick_qualification())
        if _is_stopping():
            return _STOPPING[1] if _en(language) else _STOPPING[0]
        if cmd is RuntimeCommand.REWARM_CONCISE:
            return describe_rewarm(request_rewarm("CONCISE"), language=language)
        if cmd is RuntimeCommand.COMPACT_NOW:
            return describe_compaction(await run_compact_now(llm), language=language)
    except Exception as exc:  # noqa: BLE001 — an operator command never crashes a turn
        return f"{cmd.value}: {type(exc).__name__}"
    return cmd.value
