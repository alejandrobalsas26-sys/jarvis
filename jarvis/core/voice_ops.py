"""core/voice_ops.py — V67 M33: typed operational voice control.

Extends the EXISTING voice stack (STT in core.audio, TTS in core.tts, barge-in in
core.voice_interrupt) — it does NOT build a second recogniser or synthesiser. It adds
a thin *typed operational intent* layer between recognised text and the response:

    recognised text → typed VoiceOpsIntent (fixed allowlist) → mode → grounded answer
                    → spoken via the EXISTING tts.speak_async

Security is the whole point. Voice text is NEVER turned into arbitrary shell or an
arbitrary tool call. It resolves ONLY to a fixed set of typed intents:

  * READ_ONLY   → answered directly from structured state (the M32 grounded query
                  engine) — no world effect;
  * DRY_RUN     → the recommended runbook is PLANNED via RunbookEngine.dry_run — a
                  plan only, nothing executed;
  * REQUIRES_APPROVAL → anything that would change the world (executing a runbook /
                  a tool) is refused here and routed to the existing HITL + authority
                  + scope + audit gate. Voice never auto-executes a world effect.

Everything spoken is short and ASCII (Windows TTS / cp1252-safe). Interruption,
barge-in, cancellation and TTS shutdown are unchanged — we only call the existing
tts.speak_async, so the existing interrupt machinery still governs playback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from loguru import logger

from core.ops_query import OperationalContext, answer_question, build_live_context
from core.ops_views import _redact


class VoiceOpsMode(str, Enum):
    READ_ONLY = "read_only"                # execute directly (no world effect)
    DRY_RUN = "dry_run"                    # plan only, nothing executed
    REQUIRES_APPROVAL = "requires_approval"  # HITL + authority + scope; never from voice


class VoiceOpsIntent(str, Enum):
    SYSTEM_STATUS = "system_status"
    WHAT_CHANGED = "what_changed"
    SUMMARIZE_INCIDENTS = "summarize_incidents"
    WHAT_UNCERTAIN = "what_uncertain"
    UNHEALTHY_ASSETS = "unhealthy_assets"
    EXPOSED_SERVICES = "exposed_services"
    RECOMMEND_RUNBOOK = "recommend_runbook"
    DRY_RUN_RUNBOOK = "dry_run_runbook"
    EXECUTE_RUNBOOK = "execute_runbook"     # high-impact → REQUIRES_APPROVAL, never run
    UNKNOWN = "unknown"


# Each read-only intent maps to a question the M32 engine already answers (grounded).
_READ_ONLY_QUESTION: dict[VoiceOpsIntent, str] = {
    VoiceOpsIntent.SYSTEM_STATUS: "What is happening right now?",
    VoiceOpsIntent.WHAT_CHANGED: "What changed?",
    VoiceOpsIntent.SUMMARIZE_INCIDENTS: "Why is this incident important?",
    VoiceOpsIntent.WHAT_UNCERTAIN: "What is uncertain?",
    VoiceOpsIntent.UNHEALTHY_ASSETS: "Which assets are unhealthy?",
    VoiceOpsIntent.EXPOSED_SERVICES: "Which services are exposed?",
    VoiceOpsIntent.RECOMMEND_RUNBOOK: "What runbook do you recommend?",
}

# Ordered (specific → general). Each rule: ALL of `all` present, or ANY of `any`.
# DRY_RUN is checked BEFORE EXECUTE: "dry-run the recommended runbook" must never be
# read as an execute request (its substring "run the recommended" would otherwise match).
_RULES: tuple[tuple[VoiceOpsIntent, VoiceOpsMode, dict], ...] = (
    (VoiceOpsIntent.DRY_RUN_RUNBOOK, VoiceOpsMode.DRY_RUN,
     {"any": ("dry run", "dry-run", "plan the runbook", "plan the recommended",
              "simulate the runbook")}),
    (VoiceOpsIntent.EXECUTE_RUNBOOK, VoiceOpsMode.REQUIRES_APPROVAL,
     {"any": ("execute the runbook", "run the runbook", "actually run",
              "execute runbook", "run the recommended", "perform the runbook")}),
    (VoiceOpsIntent.RECOMMEND_RUNBOOK, VoiceOpsMode.READ_ONLY,
     {"any": ("recommend a runbook", "recommend runbook", "what runbook",
              "which runbook", "recommended runbook")}),
    (VoiceOpsIntent.SUMMARIZE_INCIDENTS, VoiceOpsMode.READ_ONLY,
     {"any": ("summarize incident", "summarise incident", "incident summary",
              "incidents")}),
    (VoiceOpsIntent.WHAT_UNCERTAIN, VoiceOpsMode.READ_ONLY,
     {"any": ("uncertain", "unsure", "what don't you know", "what do you not know")}),
    (VoiceOpsIntent.UNHEALTHY_ASSETS, VoiceOpsMode.READ_ONLY,
     {"any": ("unhealthy", "degraded asset", "show unhealthy", "which assets",
              "asset health")}),
    (VoiceOpsIntent.EXPOSED_SERVICES, VoiceOpsMode.READ_ONLY,
     {"any": ("exposed", "exposure", "open ports", "which services")}),
    (VoiceOpsIntent.WHAT_CHANGED, VoiceOpsMode.READ_ONLY,
     {"any": ("what changed", "what's new", "whats new", "what has changed")}),
    (VoiceOpsIntent.SYSTEM_STATUS, VoiceOpsMode.READ_ONLY,
     {"any": ("system status", "sitrep", "what is happening", "what's happening",
              "situation report", "status report", "status")}),
)

_WAKE_PREFIXES = ("hey jarvis", "ok jarvis", "okay jarvis", "jarvis")


def _strip_wake(text: str) -> str:
    t = (text or "").strip().lower().lstrip(",. ")
    for w in _WAKE_PREFIXES:
        if t.startswith(w):
            t = t[len(w):].lstrip(",. ")
            break
    return t


def classify_voice_ops(text: str) -> tuple[VoiceOpsIntent, VoiceOpsMode]:
    """Deterministic classification into the FIXED operational-intent allowlist.
    Returns (UNKNOWN, READ_ONLY) when nothing matches (caller falls through)."""
    t = _strip_wake(text)
    if not t:
        return (VoiceOpsIntent.UNKNOWN, VoiceOpsMode.READ_ONLY)
    for intent, mode, rule in _RULES:
        if "all" in rule and not all(s in t for s in rule["all"]):
            continue
        if "any" in rule and not any(s in t for s in rule["any"]):
            continue
        return (intent, mode)
    return (VoiceOpsIntent.UNKNOWN, VoiceOpsMode.READ_ONLY)


@dataclass
class VoiceOpsResponse:
    intent: VoiceOpsIntent
    mode: VoiceOpsMode
    spoken: str                      # short, TTS-friendly, ASCII, redacted
    requires_approval: bool = False
    executed_world_effect: bool = False   # ALWAYS False — voice never effects the world
    runbook: str | None = None
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": "voice_ops", "intent": self.intent.value, "mode": self.mode.value,
                "spoken": self.spoken, "requires_approval": self.requires_approval,
                "executed_world_effect": self.executed_world_effect,
                "runbook": self.runbook, "data": self.data}


def _recommended_runbook(ctx: OperationalContext) -> tuple[str | None, str]:
    """The situation's recommended runbook + a clean target for a dry-run plan."""
    s = ctx.situation.to_dict() if hasattr(ctx.situation, "to_dict") else (ctx.situation or {})
    recs = s.get("recommendations", []) if isinstance(s, dict) else []
    if not recs:
        return (None, "localhost")
    runbook = recs[0].get("runbook")
    top = (s.get("summary", {}) or {}).get("top_priority") or {}
    asset = str(top.get("asset", "")) or ""
    target = asset.split(":")[-1] if asset else "localhost"   # strip type prefix
    return (runbook, target or "localhost")


def handle_voice_ops(text: str, *, context: OperationalContext | None = None,
                     runbook_engine=None) -> VoiceOpsResponse | None:
    """Resolve one recognised utterance into a typed operational response.

    Returns ``None`` when the utterance is not an operational command (the caller then
    falls through to macros / the LLM). NEVER executes a world effect: read-only intents
    answer from state, dry-run intents PLAN only, and an execute request is refused and
    marked for the out-of-band HITL gate."""
    intent, mode = classify_voice_ops(text)
    if intent is VoiceOpsIntent.UNKNOWN:
        return None

    ctx = context or build_live_context()

    # ── high-impact: refuse from voice; route to the existing HITL/authority gate ──
    if mode is VoiceOpsMode.REQUIRES_APPROVAL:
        runbook, _ = _recommended_runbook(ctx)
        spoken = ("Running a runbook changes the world. That needs operator approval, "
                  "authorized scope, and goes through the human-in-the-loop gate. "
                  "I will not run it from a voice command.")
        return VoiceOpsResponse(intent=intent, mode=mode, spoken=spoken,
                                requires_approval=True, runbook=runbook)

    # ── dry-run: plan only, nothing executed ──────────────────────────────────────
    if mode is VoiceOpsMode.DRY_RUN:
        runbook, target = _recommended_runbook(ctx)
        if not runbook:
            return VoiceOpsResponse(
                intent=intent, mode=mode,
                spoken="There is no recommended runbook to dry-run right now.")
        if runbook_engine is None:
            from core.runbook_engine import engine as runbook_engine
        result = runbook_engine.dry_run(runbook, {"host": target, "target": target})
        plan = result.plan
        steps = len(plan.steps) if plan else 0
        hitl = len(plan.requires_hitl_steps) if plan else 0
        spoken = (f"Dry-run of {runbook}: {steps} step{'s' if steps != 1 else ''}, "
                  f"{hitl} requiring approval. This is a plan only; nothing was executed.")
        return VoiceOpsResponse(intent=intent, mode=mode, spoken=_redact(spoken),
                                runbook=runbook,
                                data={"status": result.status, "hitl_steps": hitl})

    # ── read-only: grounded answer from structured state (M32) ────────────────────
    question = _READ_ONLY_QUESTION.get(intent, "What is happening right now?")
    bundle = answer_question(question, context=ctx)
    return VoiceOpsResponse(intent=intent, mode=mode, spoken=_redact(bundle.answer),
                            data={"grounded": bundle.grounded, "empty": bundle.empty,
                                  "query_intent": bundle.intent.value})


async def process_for_voice_ops(text: str, broadcast_fn=None, tts=None, *,
                                context: OperationalContext | None = None) -> bool:
    """Voice-pipeline entry point (mirrors ``voice_macros.process_for_macro``). Returns
    True if the utterance was an operational command (answer spoken via the EXISTING
    TTS), False otherwise so the caller falls through to macros / the LLM."""
    try:
        resp = handle_voice_ops(text, context=context)
    except Exception as e:  # noqa: BLE001 — a bad utterance never breaks the voice loop
        logger.debug(f"VOICE_OPS: handle error: {e}")
        return False
    if resp is None:
        return False
    logger.info(f"VOICE_OPS: {resp.intent.value} ({resp.mode.value})")
    if tts is not None:
        try:
            await tts.speak_async(resp.spoken)   # reuse existing TTS (barge-in intact)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"VOICE_OPS: tts error: {e}")
    if broadcast_fn is not None:
        try:
            await broadcast_fn(resp.to_dict())
        except Exception:  # noqa: BLE001
            pass
    return True
