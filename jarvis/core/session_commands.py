"""core/session_commands.py — V69 M60.3.1: session-continuity operator commands.

SECURITY POSTURE (identical to ``core.runtime_commands``, deliberately)
-----------------------------------------------------------------------
  * EXACT-MATCH ALLOWLIST — a command is recognised only when the WHOLE line matches
    a known alias. There is NO argument parsing anywhere in this module, so no path,
    session id, file name, size or destination can ever arrive from free text. The
    export destination is a MANAGED directory with an application-generated name;
    ``/session-export-redacted ../../etc/passwd`` is not a command, it is an ordinary
    conversational turn.
  * no shell, no subprocess, no environment mutation, no host change, no Ollama
    change, no Git operation, no semantic-collection write, no model inference;
  * six commands are READ-ONLY or bounded-local; ``/session-forget-current`` is
    DESTRUCTIVE and therefore requires a deterministic two-step confirmation whose
    token is minted here and never derived from model output;
  * ``/session-resume-last`` restores conversation TEXT only. It cannot resume a tool,
    a job, an authorization or an effect — the restore path has no execution seam.

A command that is not on the list is not a command.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from core.managed_paths import exports_dir, managed_path
from core.redaction_policy import scan_structure
from core.session_continuity import SessionState, id_hash
from core.session_restore import render_continuity_panel, restore_session

MAX_EXPORT_BYTES = 512 * 1024
MAX_LISTED_SESSIONS = 10


class SessionCommand(str, Enum):
    """The complete, closed set of M60 session commands."""

    SESSION_STATUS = "SESSION_STATUS"
    SESSIONS = "SESSIONS"
    SESSION_NEW = "SESSION_NEW"
    SESSION_RESUME_LAST = "SESSION_RESUME_LAST"
    SESSION_CHECKPOINT = "SESSION_CHECKPOINT"
    SESSION_FORGET_CURRENT = "SESSION_FORGET_CURRENT"
    SESSION_FORGET_CONFIRM = "SESSION_FORGET_CONFIRM"
    SESSION_EXPORT_REDACTED = "SESSION_EXPORT_REDACTED"
    RECOVERY_STATUS = "RECOVERY_STATUS"


READ_ONLY: frozenset[SessionCommand] = frozenset({
    SessionCommand.SESSION_STATUS, SessionCommand.SESSIONS,
    SessionCommand.RECOVERY_STATUS,
})
DESTRUCTIVE: frozenset[SessionCommand] = frozenset({
    SessionCommand.SESSION_FORGET_CURRENT, SessionCommand.SESSION_FORGET_CONFIRM,
})

_ALIASES: dict[SessionCommand, tuple[str, ...]] = {
    SessionCommand.SESSION_STATUS: ("/session-status", "/estado-sesion",
                                    "/estado-sesión"),
    SessionCommand.SESSIONS: ("/sessions", "/sesiones"),
    SessionCommand.SESSION_NEW: ("/session-new", "/sesion-nueva", "/sesión-nueva"),
    SessionCommand.SESSION_RESUME_LAST: ("/session-resume-last",
                                         "/reanudar-sesion", "/reanudar-sesión"),
    SessionCommand.SESSION_CHECKPOINT: ("/session-checkpoint",
                                        "/punto-de-control"),
    SessionCommand.SESSION_FORGET_CURRENT: ("/session-forget-current",
                                            "/olvidar-sesion", "/olvidar-sesión"),
    SessionCommand.SESSION_FORGET_CONFIRM: ("/session-forget-confirm",
                                            "/olvidar-confirmar"),
    SessionCommand.SESSION_EXPORT_REDACTED: ("/session-export-redacted",
                                             "/exportar-sesion",
                                             "/exportar-sesión"),
    SessionCommand.RECOVERY_STATUS: ("/recovery-status", "/estado-recuperacion",
                                     "/estado-recuperación"),
}
_EXACT: dict[str, SessionCommand] = {
    alias: cmd for cmd, aliases in _ALIASES.items() for alias in aliases
}


@dataclass(frozen=True)
class ParsedSessionCommand:
    command: SessionCommand
    alias: str

    @property
    def read_only(self) -> bool:
        return self.command in READ_ONLY

    @property
    def destructive(self) -> bool:
        return self.command in DESTRUCTIVE


def parse_session_command(text: str) -> ParsedSessionCommand | None:
    """Return the command when the WHOLE line is a known alias, else ``None``.

    Case-insensitive; interior whitespace normalised. No prefix matching and no
    argument capture — ``/session-export-redacted C:\\secrets`` is an ordinary turn.
    """
    raw = " ".join((text or "").strip().lower().split())
    if not raw or not raw.startswith("/"):
        return None
    cmd = _EXACT.get(raw)
    return ParsedSessionCommand(command=cmd, alias=raw) if cmd is not None else None


def known_session_aliases() -> tuple[str, ...]:
    return tuple(sorted(_EXACT))


# ══════════════════════════════════════════════════════════════════════════════
#  Two-step confirmation for the one destructive command
# ══════════════════════════════════════════════════════════════════════════════
class ForgetConfirmation:
    """A single-use, session-scoped confirmation token for ``/session-forget-current``.

    The token is minted HERE and matched HERE. It is never derived from, and never
    matchable by, model output: the confirming command is itself an exact alias, so
    the only way to reach the destructive branch is a second deliberate keystroke by
    the operator after the first command armed it.
    """

    def __init__(self, *, ttl_turns: int = 1) -> None:
        self._armed_for: str | None = None
        self._remaining = 0
        self.ttl_turns = max(1, int(ttl_turns))

    def arm(self, session_id: str) -> str:
        self._armed_for = str(session_id or "")
        self._remaining = self.ttl_turns
        return id_hash(self._armed_for)

    def is_armed_for(self, session_id: str) -> bool:
        return bool(self._armed_for) and self._armed_for == str(session_id or "")

    def consume(self, session_id: str) -> bool:
        """Single use: a confirmation is valid exactly once, for exactly that session."""
        if not self.is_armed_for(session_id):
            return False
        self._armed_for = None
        self._remaining = 0
        return True

    def expire_turn(self) -> None:
        """An ordinary turn between arm and confirm cancels the confirmation."""
        if self._armed_for is None:
            return
        self._remaining -= 1
        if self._remaining <= 0:
            self._armed_for = None

    def snapshot(self) -> dict:
        return {"armed": self._armed_for is not None, "remaining": self._remaining}


_confirmation = ForgetConfirmation()


def get_forget_confirmation() -> ForgetConfirmation:
    return _confirmation


def reset_forget_confirmation() -> None:
    global _confirmation
    _confirmation = ForgetConfirmation()


# ══════════════════════════════════════════════════════════════════════════════
#  Rendering — bounded, ASCII, content-free
# ══════════════════════════════════════════════════════════════════════════════
def _en(language: str) -> bool:
    return str(language or "es").lower().startswith("en")


def render_sessions(journal, *, language: str = "es") -> str:
    """List retained sessions by HASHED id — never the raw id, never any content."""
    english = _en(language)
    title = "SESSIONS" if english else "SESIONES"
    try:
        sessions = journal.sessions()
    except Exception:  # noqa: BLE001
        sessions = []
    active = (journal.active_session.session_id
              if getattr(journal, "active_session", None) else None)
    lines = [title, f"  retained={len(sessions)}"]
    for s in sessions[:MAX_LISTED_SESSIONS]:
        marker = "*" if s.session_id == active else " "
        try:
            turns = len(journal.turns(s.session_id))
        except Exception:  # noqa: BLE001
            turns = 0
        lines.append(f" {marker}{id_hash(s.session_id)} state={s.state} "
                     f"turns={turns} lang={s.language}")
    if len(sessions) > MAX_LISTED_SESSIONS:
        lines.append(f"  (+{len(sessions) - MAX_LISTED_SESSIONS} more)")
    note = ("ids are hashed; no conversation text is listed" if english
            else "los ids estan hasheados; no se lista texto de conversacion")
    lines.append(f"  ({note})")
    return "\n".join(lines)


def build_redacted_export(journal, *, session_id: str | None = None,
                          max_bytes: int = MAX_EXPORT_BYTES) -> dict:
    """Build the export PAYLOAD (no disk write). Bounded, redacted, secret-scanned."""
    sess = None
    try:
        sess = (journal.get_session(session_id) if session_id
                else (journal.active_session or journal.last_session()))
    except Exception:  # noqa: BLE001
        sess = None
    if sess is None:
        return {"error": "no_session"}
    try:
        turns = journal.turns(sess.session_id)
    except Exception:  # noqa: BLE001
        turns = []
    payload = {
        "schema": "jarvis.session.export",
        "schema_version": 1,
        "session_id_hash": id_hash(sess.session_id),
        "created_at": sess.created_at,
        "last_active_at": sess.last_active_at,
        "language": sess.language,
        "state": sess.state,
        "persistence_mode": sess.persistence_mode,
        "turn_count": len(turns),
        "turns": [],
    }
    size = 0
    for t in turns:
        # Content is ALREADY redacted at write time; the export copies it, it never
        # re-reads a raw source. Metadata-only sessions export counts and no text.
        entry = {"sequence": t.sequence, "role": t.role,
                 "terminal_state": t.terminal_state,
                 "chars": t.content_chars,
                 "content": t.content if t.content_persisted else ""}
        size += len(entry["content"]) + 120
        if size > max_bytes:
            payload["truncated"] = True
            break
        payload["turns"].append(entry)
    leaks = scan_structure(payload)
    payload["secret_scan"] = "CLEAN" if not leaks else "LEAK_DETECTED"
    if leaks:
        # Refuse rather than patch: a scanner hit means the redaction pipeline has a
        # bug, and shipping a "fixed" bundle would hide it.
        return {"error": "secret_scan_failed", "categories": leaks}
    return payload


def write_redacted_export(journal, *, session_id: str | None = None,
                          name: str | None = None) -> dict:
    """Write the export to the MANAGED exports directory under a generated name.

    The file name is derived from the session id HASH and a fixed prefix — it never
    comes from operator text, so no path, extension or directory can be injected.
    """
    payload = build_redacted_export(journal, session_id=session_id)
    if "error" in payload:
        return payload
    leaf = name or f"session_{payload['session_id_hash']}"
    try:
        target = managed_path(exports_dir(), leaf, suffix=".json")
    except Exception as exc:  # noqa: BLE001
        return {"error": "unsafe_name", "reason": type(exc).__name__}
    blob = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(blob, encoding="utf-8")
        tmp.replace(target)          # atomic rename — never a half-written export
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return {"error": "write_failed", "reason": type(exc).__name__}
    return {"state": "WRITTEN", "file": target.name, "bytes": len(blob),
            "turns": len(payload["turns"]),
            "session_id_hash": payload["session_id_hash"],
            "secret_scan": payload["secret_scan"]}


# ── Localized confirmations (never echo operator text) ───────────────────────
_MSG = {
    "no_journal": ("La persistencia de sesion esta desactivada.",
                   "Session persistence is disabled."),
    "new_session": ("Sesion nueva iniciada ({h}).",
                    "New session started ({h})."),
    "checkpoint_ok": ("Punto de control escrito ({ms} ms).",
                      "Checkpoint written ({ms} ms)."),
    "checkpoint_fail": ("Punto de control fallido: {r}.",
                        "Checkpoint failed: {r}."),
    "forget_arm": ("Vas a borrar el contenido de la sesion actual ({h}). "
                   "Escribe /session-forget-confirm para confirmar.",
                   "This will erase the current session content ({h}). "
                   "Type /session-forget-confirm to confirm."),
    "forget_none": ("No hay confirmacion pendiente; usa /session-forget-current "
                    "primero.",
                    "No pending confirmation; use /session-forget-current first."),
    "forget_done": ("Contenido borrado: {n} turnos. Se conserva el registro minimo.",
                    "Content erased: {n} turns. Minimal audit metadata retained."),
    "resume_none": ("No hay sesion previa para reanudar.",
                    "No previous session to resume."),
    "resume_ok": ("Sesion reanudada: {n} turnos ({p} parciales). "
                  "No se reanudo ninguna accion.",
                  "Session resumed: {n} turns ({p} partial). "
                  "No action was resumed."),
    "resume_meta": ("Sesion encontrada pero sin contenido guardado (modo {m}).",
                    "Session found but no content stored (mode {m})."),
    "export_ok": ("Exportacion redactada escrita: {f} ({b} bytes, {n} turnos).",
                  "Redacted export written: {f} ({b} bytes, {n} turns)."),
    "export_fail": ("Exportacion no realizada: {r}.",
                    "Export not performed: {r}."),
}


def _m(key: str, language: str, **kw) -> str:
    pair = _MSG[key]
    return (pair[1] if _en(language) else pair[0]).format(**kw)


def apply_session_command(parsed: ParsedSessionCommand, *, language: str = "es",
                          journal=None, recovery=None,
                          fingerprints: dict | None = None,
                          restore_turns: int = 12) -> str:
    """Apply ONE parsed session command and return operator-facing text.

    Bounded and never raising. Nothing here starts inference, touches the host, opens
    a socket, executes a tool or reuses an authorization.
    """
    from core.session_continuity import get_session_journal
    j = journal if journal is not None else get_session_journal()
    cmd = parsed.command
    try:
        if not getattr(j, "enabled", False) and cmd is not SessionCommand.SESSIONS:
            return _m("no_journal", language)

        if cmd is SessionCommand.SESSION_STATUS:
            return render_continuity_panel(j, recovery=recovery, language=language)
        if cmd is SessionCommand.SESSIONS:
            return render_sessions(j, language=language)
        if cmd is SessionCommand.RECOVERY_STATUS:
            from core.turn_reconciliation import RecoveryReport, render_recovery_panel
            return render_recovery_panel(recovery or RecoveryReport(),
                                         language=language)

        if cmd is SessionCommand.SESSION_NEW:
            prev = j.active_session
            if prev is not None:
                j.close_session(state=SessionState.CLOSED)
            fp = fingerprints or {}
            sess = j.begin_session(
                language=language,
                authority_fingerprint=fp.get("authority_fingerprint", ""),
                scope_fingerprint=fp.get("scope_fingerprint", ""),
                security_policy_version=fp.get("security_policy_version", ""),
                prompt_fingerprint=fp.get("prompt_fingerprint", ""),
                tool_schema_fingerprint=fp.get("tool_schema_fingerprint", ""))
            return _m("new_session", language, h=id_hash(sess.session_id))

        if cmd is SessionCommand.SESSION_CHECKPOINT:
            res = j.checkpoint_run()
            j.touch_session()
            if res.ok:
                return _m("checkpoint_ok", language, ms=res.duration_ms)
            return _m("checkpoint_fail", language, r=res.reason or "unknown")

        if cmd is SessionCommand.SESSION_RESUME_LAST:
            restored = restore_session(j, current_fingerprints_map=fingerprints,
                                       max_turns=restore_turns)
            if restored.state.value == "NO_SESSION":
                return _m("resume_none", language)
            if restored.state.value == "METADATA_ONLY":
                return _m("resume_meta", language, m=j.mode.value)
            if restored.state.value == "REFUSED":
                return _m("resume_none", language)
            text = _m("resume_ok", language, n=restored.restored_turns,
                      p=restored.partial_turns)
            if restored.invalidated:
                text += ("\n  invalidated=" + ",".join(restored.invalidated))
            return text

        if cmd is SessionCommand.SESSION_FORGET_CURRENT:
            sess = j.active_session
            if sess is None:
                return _m("resume_none", language)
            h = get_forget_confirmation().arm(sess.session_id)
            return _m("forget_arm", language, h=h)

        if cmd is SessionCommand.SESSION_FORGET_CONFIRM:
            sess = j.active_session
            conf = get_forget_confirmation()
            if sess is None or not conf.consume(sess.session_id):
                return _m("forget_none", language)
            out = j.forget_session(sess.session_id)
            return _m("forget_done", language, n=out["turns_removed"])

        if cmd is SessionCommand.SESSION_EXPORT_REDACTED:
            res = write_redacted_export(j)
            if "error" in res:
                return _m("export_fail", language, r=res["error"])
            return _m("export_ok", language, f=res["file"], b=res["bytes"],
                      n=res["turns"])
    except Exception as exc:  # noqa: BLE001 — an operator command never crashes a turn
        return f"{cmd.value}: {type(exc).__name__}"
    return cmd.value
