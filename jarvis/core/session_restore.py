"""core/session_restore.py — V69 M60.3: bounded, truthful conversation resumption.

WHAT MAY BE RESTORED
--------------------
Only committed, DISPLAYED conversation content, plus the small set of facts that are
still true after a restart: the session identity, the language, the operator's
response profile, the unresolved-question markers and the extractive digest.

WHAT MUST BE RECOMPUTED, NEVER RESTORED
---------------------------------------
The prompt manifest, the tool-schema fingerprint, the authority/scope posture, the
security-policy version, the language directive, model readiness and runtime health.
Restoring any of those would be asserting, on the strength of a file, something that
is only knowable by measuring the CURRENT process. M56 already proved this class of
bug the expensive way (a prewarm whose num_ctx no longer matched the live turn cost
8.7 s on an already-resident model); M60 does not repeat it with authorization.

INVALIDATION
------------
When a stamped fingerprint no longer matches the recomputed one, the CONTINUATION and
prefix-cache state derived from it is invalidated and the reason is reported. The
human-readable history is KEPT — it is still a true record of what was said; only the
machine state that depended on the old fingerprint is dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from core.session_continuity import SessionRecord, SessionState, TurnRecord
from core.turn_reconciliation import RecoveredTurnState, compare_fingerprints

# Bounded restoration defaults (M60.3.2).
DEFAULT_RESTORE_TURNS = 12
MAX_RESTORE_TURNS = 40
MAX_RESTORE_CHARS = 8000
MAX_DIGEST_CHARS = 1200

# Terminal states whose content was complete and may be replayed as history verbatim.
_COMPLETE_STATES = frozenset({"COMPLETED", "FINAL_RESPONSE_COMPLETE"})
# States whose content is real but PARTIAL — restorable, but labelled as truncated.
_PARTIAL_STATES = frozenset({
    RecoveredTurnState.PARTIAL_VISIBLE_RESPONSE.value,
    RecoveredTurnState.INTERRUPTED_BY_CRASH.value,
    RecoveredTurnState.INTERRUPTED_BY_POWER_LOSS.value,
    RecoveredTurnState.UNKNOWN_TERMINATION.value,
    "INTERRUPTED_BY_OPERATOR", "REPLACED_BY_NEW_TURN", "TIMED_OUT",
    "CANCELLED_ON_SHUTDOWN", "FINAL_RESPONSE_TRUNCATED",
})
# States that contribute NOTHING to conversation context.
_EMPTY_STATES = frozenset({
    RecoveredTurnState.FAILED_BEFORE_CONTENT.value, "FAILED", "ACTIVE",
})


class RestoreState(str, Enum):
    NO_SESSION = "NO_SESSION"
    RESTORED = "RESTORED"
    RESTORED_WITH_INVALIDATION = "RESTORED_WITH_INVALIDATION"
    METADATA_ONLY = "METADATA_ONLY"
    REFUSED = "REFUSED"


@dataclass
class RestoredSession:
    """A bounded, truthful conversational restoration. No effect is ever resumed."""

    state: RestoreState = RestoreState.NO_SESSION
    session_id: str = ""
    language: str = "es"
    response_profile: str = "AUTO"
    turns: list[dict] = field(default_factory=list)     # OpenAI-shaped history
    restored_turns: int = 0
    restored_chars: int = 0
    partial_turns: int = 0
    unresolved_questions: list[str] = field(default_factory=list)
    invalidated: list[str] = field(default_factory=list)
    digest: str = ""
    notes: list[str] = field(default_factory=list)
    continuation_valid: bool = True

    def note(self, message: str) -> None:
        if len(self.notes) < 12:
            self.notes.append(str(message)[:120])

    def snapshot(self) -> dict:
        """Content-free view for health/panels — counts, never conversation text."""
        return {
            "restore_state": self.state.value,
            "language": self.language,
            "response_profile": self.response_profile,
            "restored_turns": self.restored_turns,
            "restored_chars": self.restored_chars,
            "partial_turns": self.partial_turns,
            "unresolved_questions": len(self.unresolved_questions),
            "invalidated": list(self.invalidated),
            "continuation_valid": self.continuation_valid,
            "digest_chars": len(self.digest),
            "notes": list(self.notes),
        }


# ── Labels: an incomplete answer must SAY it is incomplete ───────────────────
_PARTIAL_LABEL = {
    "es": "[respuesta interrumpida — solo se muestra lo que llegaste a ver]",
    "en": "[interrupted response — only what you actually saw is shown]",
}
_UNRESOLVED_LABEL = {
    "es": "[pregunta sin responder tras la interrupcion]",
    "en": "[question left unanswered by the interruption]",
}


def _label(language: str, table: dict) -> str:
    return table["en"] if str(language or "es").lower().startswith("en") \
        else table["es"]


def build_restored_history(turns: list[TurnRecord], *, language: str = "es",
                           max_turns: int = DEFAULT_RESTORE_TURNS,
                           max_chars: int = MAX_RESTORE_CHARS
                           ) -> tuple[list[dict], dict]:
    """Turn journal records into a bounded OpenAI-shaped history.

    Newest-first selection, then chronological output — so a long session yields the
    most RELEVANT window rather than its oldest turns. A partial assistant answer is
    included but explicitly labelled; nothing is silently presented as complete.
    """
    stats = {"restored": 0, "chars": 0, "partial": 0, "skipped": 0,
             "unresolved": []}
    if not turns:
        return [], stats
    bounded = max(1, min(int(max_turns), MAX_RESTORE_TURNS))
    selected: list[TurnRecord] = []
    chars = 0
    for t in reversed(turns):
        state = str(t.terminal_state or "")
        if state in _EMPTY_STATES and state != "ACTIVE":
            stats["skipped"] += 1
            continue
        if state == "ACTIVE":
            # Never restore a turn that was never finalized: its state is unknown.
            stats["skipped"] += 1
            continue
        if state == RecoveredTurnState.UNRESOLVED_USER_MESSAGE.value:
            if t.content_persisted and t.content:
                stats["unresolved"].append(t.content[:200])
        if not t.content_persisted or not t.content:
            stats["skipped"] += 1
            continue
        if len(selected) >= bounded or chars + len(t.content) > max_chars:
            break
        selected.append(t)
        chars += len(t.content)
    selected.reverse()

    history: list[dict] = []
    for t in selected:
        content = t.content
        state = str(t.terminal_state or "")
        if state in _PARTIAL_STATES and t.role == "assistant":
            content = f"{content}\n{_label(language, _PARTIAL_LABEL)}"
            stats["partial"] += 1
        elif state == RecoveredTurnState.UNRESOLVED_USER_MESSAGE.value:
            content = f"{content}\n{_label(language, _UNRESOLVED_LABEL)}"
        elif state not in _COMPLETE_STATES and t.role == "assistant":
            content = f"{content}\n{_label(language, _PARTIAL_LABEL)}"
            stats["partial"] += 1
        history.append({"role": "user" if t.role == "user" else "assistant",
                        "content": content})
    stats["restored"] = len(history)
    stats["chars"] = sum(len(h["content"]) for h in history)
    return history, stats


def restore_session(journal, *, session_id: str | None = None,
                    current_fingerprints_map: dict | None = None,
                    max_turns: int = DEFAULT_RESTORE_TURNS,
                    digest: str = "") -> RestoredSession:
    """Restore ONE bounded session. Never resumes an effect, a job or an authorization.

    A missing/forgotten session, a disabled journal or a metadata-only mode all yield
    a truthful non-restoring result rather than a partially invented one.
    """
    out = RestoredSession()
    if not getattr(journal, "enabled", False):
        out.state = RestoreState.REFUSED
        out.note("persistence is OFF")
        return out

    sess: SessionRecord | None = None
    try:
        sess = (journal.get_session(session_id) if session_id
                else journal.last_session())
    except Exception as exc:  # noqa: BLE001
        out.state = RestoreState.REFUSED
        out.note(f"session lookup failed: {type(exc).__name__}")
        return out
    if sess is None:
        out.state = RestoreState.NO_SESSION
        return out
    if sess.state == SessionState.FORGOTTEN.value:
        out.state = RestoreState.REFUSED
        out.session_id = sess.session_id
        out.note("session content was explicitly forgotten")
        return out

    out.session_id = sess.session_id
    out.language = sess.language or "es"
    out.response_profile = sess.response_profile or "AUTO"

    # Fingerprint comparison FIRST: an invalidated continuation must not be handed
    # back as if it were still valid, even when the history itself is fine.
    if current_fingerprints_map:
        out.invalidated = compare_fingerprints(sess, current_fingerprints_map)
        if out.invalidated:
            out.continuation_valid = False
            for change in out.invalidated:
                out.note(f"invalidated by {change}")

    try:
        turns = journal.turns(sess.session_id)
    except Exception as exc:  # noqa: BLE001
        out.state = RestoreState.REFUSED
        out.note(f"turn read failed: {type(exc).__name__}")
        return out

    if not getattr(journal, "persists_content", False):
        out.state = RestoreState.METADATA_ONLY
        out.restored_turns = 0
        out.note(f"{len(turns)} turns recorded without content")
        return out

    history, stats = build_restored_history(turns, language=out.language,
                                            max_turns=max_turns)
    out.turns = history
    out.restored_turns = stats["restored"]
    out.restored_chars = stats["chars"]
    out.partial_turns = stats["partial"]
    out.unresolved_questions = list(stats["unresolved"])[:5]
    if digest:
        out.digest = str(digest)[:MAX_DIGEST_CHARS]
    out.state = (RestoreState.RESTORED_WITH_INVALIDATION if out.invalidated
                 else RestoreState.RESTORED)
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  What a restore must NEVER carry forward
# ══════════════════════════════════════════════════════════════════════════════
#  Named explicitly so the guarantee is testable rather than aspirational.
FORBIDDEN_RESTORE_KEYS: frozenset[str] = frozenset({
    "system_prompt", "tools", "tool_schema", "authority", "authority_mode",
    "scope", "scopes", "hitl", "otp", "authorization", "granted", "approved",
    "model_loaded", "warm", "warmth", "prewarmed", "ready", "readiness",
    "runtime_health", "pending_tts", "tts_queue", "locks", "queue", "tasks",
    "session_token", "credentials",
})


def assert_restore_is_clean(payload: dict) -> list[str]:
    """Return any forbidden key present in a restore payload. Empty list = clean."""
    return sorted(k for k in (payload or {}) if k.lower() in FORBIDDEN_RESTORE_KEYS)


def render_continuity_panel(journal, restored: "RestoredSession | None" = None,
                            recovery=None, *, language: str = "es") -> str:
    """The SESSION CONTINUITY panel. Counts and states only — never a line of text."""
    english = str(language or "es").lower().startswith("en")
    h = journal.health() if journal is not None else {}
    rows = [
        ("mode", h.get("persistence_mode")),
        ("active_session", h.get("active_session_id_hash")),
        ("journal_state", h.get("journal_state")),
        ("sessions_retained", h.get("sessions_retained")),
        ("turns_retained", h.get("turns_retained")),
        ("last_checkpoint_ms", h.get("last_checkpoint_ms")),
        ("checkpoint_failures", h.get("checkpoint_failures")),
    ]
    if restored is not None:
        rows += [
            ("restore_state", restored.state.value),
            ("restored_turns", restored.restored_turns),
            ("partial_turns", restored.partial_turns),
            ("unresolved_questions", len(restored.unresolved_questions)),
            ("continuation_valid", "yes" if restored.continuation_valid else "no"),
        ]
    if recovery is not None:
        rows += [
            ("recovery_state", getattr(recovery.state, "value", "")),
            ("interrupted_turns", recovery.interrupted_turns),
            ("tool_review_required", recovery.unresolved_tool_outcomes),
            ("actions_replayed", recovery.actions_replayed),
        ]
    title = "SESSION CONTINUITY" if english else "CONTINUIDAD DE SESION"
    lines = [title] + [f"  {k}={'n/a' if v is None else v}" for k, v in rows]
    note = ("readiness and model warmth are re-measured, never restored" if english
            else "la disponibilidad y el calentamiento se vuelven a medir, "
                 "nunca se restauran")
    lines.append(f"  ({note})")
    return "\n".join(lines)
