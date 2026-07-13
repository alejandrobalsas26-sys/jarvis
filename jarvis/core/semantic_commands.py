"""core/semantic_commands.py — V69 M53: operator-controlled migration commands.

One deterministic parser (the same command-surface pattern as core.mode_commands /
core.consent_commands) mapping operator text to semantic-migration actions. The
logical collection argument is ALWAYS validated against a fixed allowlist
(``MANAGED_LOGICAL``) — model output can never name an arbitrary collection,
physical alias, or migration target. Read-only actions (status/plan/dry-run) are
side-effect-free; effectful actions (migrate/validate/activate/rollback/abort/
resume) are flagged so the caller can route them through operator/HITL gating.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.semantic_migration import MANAGED_LOGICAL

# action → (aliases, effectful, needs_logical)
_ACTIONS: dict[str, tuple[tuple[str, ...], bool, bool]] = {
    "status":   (("semantic-status", "semantic status"), False, False),
    "plan":     (("semantic-plan", "semantic plan"), False, True),
    "migrate":  (("semantic-migrate", "semantic migrate"), True, True),
    "resume":   (("semantic-resume", "semantic resume"), True, True),
    "abort":    (("semantic-abort", "semantic abort"), True, True),
    "validate": (("semantic-validate", "semantic validate"), False, True),
    "activate": (("semantic-activate", "semantic activate"), True, True),
    "rollback": (("semantic-rollback", "semantic rollback"), True, True),
}

# Effectful actions that mutate on-disk vector state / the active alias.
EFFECTFUL_ACTIONS = frozenset(a for a, (_, eff, _) in _ACTIONS.items() if eff)


@dataclass(frozen=True)
class SemanticCommand:
    action: str
    logical: str | None
    dry_run: bool
    effectful: bool


def parse_semantic_command(text: str) -> SemanticCommand | None:
    """Return a validated SemanticCommand if *text* is a semantic-* command, else
    None. The logical argument must be a known managed collection."""
    t = (text or "").strip().lower()
    if not t.startswith("semantic"):
        return None
    dry_run = "--dry-run" in t or " dry-run" in t or " dry run" in t
    # Longest alias first so "semantic-status" isn't shadowed by a prefix match.
    for action, (aliases, effectful, needs_logical) in sorted(
        _ACTIONS.items(), key=lambda kv: -max(len(a) for a in kv[1][0])
    ):
        if any(t.startswith(a) or f" {a} " in f" {t} " for a in aliases):
            logical = _extract_logical(t)
            if needs_logical and logical is None:
                return None
            # A dry-run is never effectful.
            return SemanticCommand(
                action=action, logical=logical,
                dry_run=dry_run, effectful=effectful and not dry_run,
            )
    return None


def _extract_logical(text: str) -> str | None:
    """Find a known managed logical name mentioned in *text* (allowlist only)."""
    for name in MANAGED_LOGICAL:
        if name in text:
            return name
    return None


def dispatch_semantic_command(cmd: SemanticCommand, controller) -> dict:
    """Execute a parsed command against a SemanticMigrationController. Returns a
    plain dict (safe to surface). Effectful actions assume the caller already
    applied operator/HITL authorization — this function does not bypass it."""
    a = cmd.action
    if a == "status":
        return {"action": "status", "collections": controller.status()}
    if a == "plan":
        return {"action": "plan", **controller.plan(cmd.logical).__dict__}
    if a == "migrate":
        return {"action": "migrate", **controller.migrate(cmd.logical, dry_run=cmd.dry_run).__dict__}
    if a == "resume":
        return {"action": "resume", **controller.resume(cmd.logical).__dict__}
    if a == "abort":
        return {"action": "abort", **controller.abort(cmd.logical).__dict__}
    if a == "validate":
        return {"action": "validate", **controller.validate(cmd.logical).__dict__}
    if a == "activate":
        return {"action": "activate", **controller.activate(cmd.logical).__dict__}
    if a == "rollback":
        return {"action": "rollback", **controller.rollback(cmd.logical).__dict__}
    return {"action": a, "status": "unknown_action"}
