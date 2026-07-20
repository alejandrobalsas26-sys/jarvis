"""core/ollama_posture.py — V69 M56.2: operator-gated Ollama server posture workflow.

M56.1 made the server's real configuration KNOWABLE (its own environment block). This
module turns that knowledge into a controlled change workflow — and it is built so the
dangerous half can never be reached by accident, by an LLM, or by a background task.

THE SIX STEPS
-------------
  status    read-only truth report                       (no mutation, no authority)
  plan      recommend values from measured hardware      (no mutation, no authority)
  dry-run   exactly what would change, and its scope     (no mutation, no authority)
  apply     write the allowlisted user-scope variables   (OPERATOR/HITL REQUIRED)
  verify    re-read the SERVER PROCESS after a restart   (no mutation)
  rollback  restore the previous managed values          (OPERATOR/HITL REQUIRED)

SAFETY MODEL (fatal rules, enforced in code and locked by tests)
----------------------------------------------------------------
  * Only three variables may ever be written: OLLAMA_NUM_PARALLEL,
    OLLAMA_MAX_LOADED_MODELS, OLLAMA_KEEP_ALIVE. Anything else is rejected by name.
  * Values are validated against strict patterns/bounds — never interpolated anywhere.
  * The write is performed through ``winreg.SetValueEx`` on HKCU\\Environment with
    typed arguments. There is NO shell, NO ``setx``, NO PowerShell fragment, and
    therefore no place a metacharacter could execute.
  * Machine scope (HKLM) is NEVER written: it needs admin rights and would change the
    posture for every user on the host.
  * No service is started, stopped, restarted or killed — ever. Applying a change is
    inert until the OPERATOR restarts the server themselves; ``verify`` is the only
    thing that may then claim success, and only from the server process's own block.
  * Every apply/rollback writes a durable, deterministic journal entry containing the
    PREVIOUS values, so rollback is exact rather than reconstructed.
  * ``apply`` and ``rollback`` demand an explicit :class:`OperatorAuthorization`. The
    default posture of the whole subsystem is observe/dry-run only.

Nothing here selects a PID, a service name, an executable path, a variable name, a
value or a command fragment from model output: the variable set is a frozen constant,
values are integers/durations parsed from operator input, and the target scope is a
fixed enum.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from core.ollama_process import (
    POSTURE_VARS,
    OllamaProcessTruth,
    PostureSource,
    get_process_truth,
)

# ── Validation: the complete, closed grammar of what may be written ──────────
_INT_VARS = frozenset({"OLLAMA_NUM_PARALLEL", "OLLAMA_MAX_LOADED_MODELS"})
_DURATION_VARS = frozenset({"OLLAMA_KEEP_ALIVE"})
# Bounds chosen for a 6-core / 15 W CPU host: more than 4 concurrent generations or 6
# resident models is a mistake on this machine, not a preference.
_INT_BOUNDS = {"OLLAMA_NUM_PARALLEL": (1, 4), "OLLAMA_MAX_LOADED_MODELS": (1, 6)}
# A duration is digits + a single unit. Anchored, so no prefix/suffix can smuggle
# anything, and 0 means "unload immediately" which is legitimate.
_DURATION_RE = re.compile(r"^(?:0|[1-9][0-9]{0,4})(?:s|m|h)$")
# Defence in depth: even though values never reach a shell, a metacharacter in one is
# a signal of injection and is refused outright.
_FORBIDDEN_CHARS_RE = re.compile(r"[;&|<>$`\"'\\\r\n\t%!^(){}\[\]*?~]")

_JOURNAL_NAME = "ollama_posture_journal.jsonl"
_MAX_JOURNAL_ENTRIES = 200


class PostureAction(str, Enum):
    STATUS = "status"
    PLAN = "plan"
    DRY_RUN = "dry-run"
    APPLY = "apply"
    VERIFY = "verify"
    ROLLBACK = "rollback"


# Actions that may never mutate anything. Enforced by assertion in the dispatcher.
READ_ONLY_ACTIONS = frozenset({PostureAction.STATUS, PostureAction.PLAN,
                               PostureAction.DRY_RUN, PostureAction.VERIFY})
# Actions that demand an explicit operator authorization.
EFFECTFUL_ACTIONS = frozenset({PostureAction.APPLY, PostureAction.ROLLBACK})


class PostureScope(str, Enum):
    """The only writable scope. HKLM is present so it can be REFUSED by name."""

    WINDOWS_USER_ENV = "WINDOWS_USER_ENV"
    WINDOWS_MACHINE_ENV = "WINDOWS_MACHINE_ENV"   # never written


class VerifyState(str, Enum):
    VERIFIED_APPLIED = "VERIFIED_APPLIED"        # server env block matches the target
    VERIFIED_NOT_APPLIED = "VERIFIED_NOT_APPLIED"  # block read, values differ/absent
    RESTART_PENDING = "RESTART_PENDING"          # written, server predates the write
    UNVERIFIABLE = "UNVERIFIABLE"                # server env unreadable / no process
    NO_MANAGED_STATE = "NO_MANAGED_STATE"        # nothing was ever applied


@dataclass(frozen=True)
class ValidationError(Exception):
    """A rejected variable or value. Carries a SAFE reason — never the raw input."""

    variable: str
    reason: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.variable}: {self.reason}"


def validate_variable(name: str) -> str:
    """Return the canonical variable name or raise. Allowlist, never a denylist."""
    canon = (name or "").strip().upper()
    if canon not in POSTURE_VARS:
        raise ValidationError(variable=canon or "<empty>",
                              reason="not an allowlisted posture variable")
    return canon


def validate_value(name: str, value) -> str:
    """Validate ONE value for an already-validated variable. Returns the canonical
    string form or raises :class:`ValidationError`."""
    canon = validate_variable(name)
    raw = "" if value is None else str(value).strip()
    if not raw:
        raise ValidationError(variable=canon, reason="empty value")
    if len(raw) > 16:
        raise ValidationError(variable=canon, reason="value too long")
    if _FORBIDDEN_CHARS_RE.search(raw):
        raise ValidationError(variable=canon, reason="value contains forbidden characters")
    if canon in _INT_VARS:
        if not raw.isdigit():
            raise ValidationError(variable=canon, reason="value must be a positive integer")
        num = int(raw)
        lo, hi = _INT_BOUNDS[canon]
        if not (lo <= num <= hi):
            raise ValidationError(variable=canon,
                                  reason=f"value out of bounds (expected {lo}..{hi})")
        return str(num)
    if canon in _DURATION_VARS:
        low = raw.lower()
        if not _DURATION_RE.match(low):
            raise ValidationError(variable=canon,
                                  reason="value must be a duration like 30m, 2h or 900s")
        return low
    raise ValidationError(variable=canon, reason="no validator for this variable")


def validate_target(target: dict) -> dict:
    """Validate a whole {var: value} target map. All-or-nothing: one bad entry
    rejects the request, so a partial posture is never written."""
    if not isinstance(target, dict) or not target:
        raise ValidationError(variable="<target>", reason="empty or malformed target")
    if len(target) > len(POSTURE_VARS):
        raise ValidationError(variable="<target>", reason="too many variables")
    return {validate_variable(k): validate_value(k, v) for k, v in target.items()}


# ── Operator authorization ───────────────────────────────────────────────────
@dataclass(frozen=True)
class OperatorAuthorization:
    """Proof that a HUMAN approved this specific effectful posture change.

    ``granted`` alone is not enough: the authorization names the ACTION and the exact
    target map it approved, so an approval for one change can never be replayed onto
    a different one. It is constructed only by the interactive operator surface —
    never from tool output, never from model text.
    """

    granted: bool = False
    action: PostureAction | None = None
    approved_target: dict = field(default_factory=dict)
    operator: str = "operator"
    reason: str = ""
    granted_at: float = 0.0

    def covers(self, action: PostureAction, target: dict) -> bool:
        if not self.granted or self.action is not action:
            return False
        return dict(self.approved_target) == dict(target)


class AuthorizationRequired(Exception):
    """Raised when an effectful action is attempted without a covering approval."""


# ── The plan ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PostureChange:
    variable: str
    current: str | None
    current_source: PostureSource
    target: str
    changes: bool

    def snapshot(self) -> dict:
        return {"variable": self.variable, "current": self.current,
                "current_source": self.current_source.value, "target": self.target,
                "changes": self.changes}


@dataclass(frozen=True)
class PosturePlan:
    """A validated, previewable change set. Pure data — building it mutates nothing."""

    changes: tuple[PostureChange, ...] = ()
    scope: PostureScope = PostureScope.WINDOWS_USER_ENV
    restart_required: bool = True
    rationale: str = ""
    created_at: float = 0.0

    @property
    def target(self) -> dict:
        return {c.variable: c.target for c in self.changes}

    @property
    def effective_changes(self) -> tuple[PostureChange, ...]:
        return tuple(c for c in self.changes if c.changes)

    def is_noop(self) -> bool:
        return not self.effective_changes

    def snapshot(self) -> dict:
        return {
            "scope": self.scope.value,
            "restart_required": self.restart_required,
            "rationale": self.rationale,
            "created_at": self.created_at,
            "changes": [c.snapshot() for c in self.changes],
            "effective_change_count": len(self.effective_changes),
            "is_noop": self.is_noop(),
        }

    def render(self) -> str:
        """A compact ASCII preview for the operator (dry-run output)."""
        lines = ["OLLAMA POSTURE DRY-RUN (no changes applied)",
                 f"  scope: {self.scope.value} (HKCU\\Environment, current user only)"]
        for c in self.changes:
            cur = c.current if c.current is not None else "unset"
            mark = "CHANGE" if c.changes else "same"
            lines.append(f"  {c.variable}: {cur} -> {c.target}  [{mark}]"
                         f"  (current source: {c.current_source.value})")
        lines.append(f"  restart required: {self.restart_required}"
                     " (the running server keeps its launch-time environment)")
        lines.append("  nothing was written; apply requires explicit operator approval")
        return "\n".join(lines)


def recommend_posture(*, hw_profile=None, profile: str | None = None,
                      observed_dual_residency: bool | None = None) -> dict:
    """Recommend posture values from MEASURED hardware, not from assumption.

    The recommendation is deliberately conservative for this class of machine:
      NUM_PARALLEL=1       one CPU generation at a time; a second concurrent decode on
                           6 cores at 15 W makes BOTH turns slower, not one faster.
      MAX_LOADED_MODELS=2  FAST + EMBEDDING must coexist, or every semantic write
                           evicts qwen3:8b and the next turn pays a cold load.
      KEEP_ALIVE           long enough that an idle pause does not cost a reload;
                           shortened on battery where holding weights costs power.
    These remain RECOMMENDED until the server process's own environment proves them.

    ``observed_dual_residency`` — M56.3 MEASUREMENT, not assumption. M55 inferred a
    single model slot from a slow turn and recommended MAX_LOADED_MODELS=2 on that
    basis. The M56.3 live run DISPROVED it: qwen3:8b and nomic-embed-text were
    observed resident together on this server's DEFAULTS. When dual residency has
    actually been observed, recommending 2 would cap a server that already allows
    more, so the variable is dropped from the recommendation entirely. Advising a
    change that measurement shows is unnecessary is exactly the M55 mistake.
    """
    parallel = 1
    keep_alive = "30m"
    try:
        if hw_profile is None:
            from core.hardware_profile import get_cached_profile
            hw_profile = get_cached_profile()
        if hw_profile is not None:
            parallel = max(1, min(4, int(getattr(hw_profile, "recommended_pools",
                                                 getattr(hw_profile, "pools", 1)) or 1)))
            keep_alive = "30m" if getattr(hw_profile, "is_dual_channel", False) else "10m"
            if getattr(hw_profile, "on_battery", False):
                keep_alive = "5m"
    except Exception:  # noqa: BLE001
        pass
    prof = (profile or "").upper()
    if prof == "BATTERY_SAVER":
        parallel, keep_alive = 1, "5m"
    elif prof == "AC_PERFORMANCE":
        keep_alive = "30m"
    out = {
        "OLLAMA_NUM_PARALLEL": str(parallel),
        # Room for the interactive FAST model AND the embedding model, never less.
        "OLLAMA_MAX_LOADED_MODELS": str(max(2, parallel)),
        "OLLAMA_KEEP_ALIVE": keep_alive,
    }
    if observed_dual_residency:
        # Measurement beats the recommendation: the server already keeps both models
        # resident, so pinning a cap could only make it worse.
        out.pop("OLLAMA_MAX_LOADED_MODELS", None)
    return out


def build_plan(*, truth: OllamaProcessTruth, target: dict | None = None,
               hw_profile=None, profile: str | None = None,
               clock: Callable[[], float] = time.time) -> PosturePlan:
    """Build a validated plan. READ-ONLY: nothing is written by planning.

    ``current`` for each variable is the SERVER-VERIFIED value when the server's own
    environment could be read; otherwise the current value is honestly unknown and the
    plan says so through ``current_source`` — it never substitutes the Windows value.
    """
    validated = validate_target(target or recommend_posture(hw_profile=hw_profile,
                                                            profile=profile))
    changes: list[PostureChange] = []
    for var in POSTURE_VARS:
        if var not in validated:
            continue
        resolved = truth.resolve(var)
        # A change is only provably unnecessary when the CURRENT value is verified and
        # equal. Unknown current -> we must treat it as a change.
        same = resolved.verified and resolved.value == validated[var]
        changes.append(PostureChange(
            variable=var, current=resolved.value, current_source=resolved.source,
            target=validated[var], changes=not same,
        ))
    rationale = ("one CPU generation at a time; FAST and EMBEDDING resident together "
                 "so a semantic write cannot evict the interactive model")
    return PosturePlan(changes=tuple(changes), scope=PostureScope.WINDOWS_USER_ENV,
                       restart_required=truth.restart_required(), rationale=rationale,
                       created_at=clock())


# ── Durable rollback journal ─────────────────────────────────────────────────
def default_journal_path() -> Path:
    """The durable journal location (under the operator's JARVIS data dir)."""
    try:
        from core.config import settings
        base = getattr(settings, "data_dir", None) or getattr(settings, "base_dir", None)
        if base:
            return Path(base) / _JOURNAL_NAME
    except Exception:  # noqa: BLE001
        pass
    return Path(os.path.expanduser("~")) / ".jarvis" / _JOURNAL_NAME


@dataclass
class PostureJournal:
    """Append-only, deterministic record of every effectful posture change.

    Each entry stores the PREVIOUS values verbatim, which is what makes rollback exact
    instead of reconstructed from a recommendation. Bounded: the file is truncated to
    the most recent entries on write, so it can never grow without limit.
    """

    path: Path = field(default_factory=default_journal_path)

    def _read_all(self) -> list[dict]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return []
        out: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue    # a corrupt line is skipped, never fatal
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def append(self, entry: dict) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            entries = self._read_all()
            entries.append(entry)
            entries = entries[-_MAX_JOURNAL_ENTRIES:]
            payload = "\n".join(json.dumps(e, sort_keys=True) for e in entries) + "\n"
            self.path.write_text(payload, encoding="utf-8")
            return True
        except OSError:
            return False

    def entries(self) -> list[dict]:
        return self._read_all()

    def last_applied(self) -> dict | None:
        """The most recent APPLY that has not already been rolled back."""
        for entry in reversed(self._read_all()):
            action = entry.get("action")
            if action == PostureAction.APPLY.value:
                return entry
            if action == PostureAction.ROLLBACK.value:
                # This rollback consumed the apply before it.
                continue
        return None


# ── The effectful write (typed registry API only — no shell, ever) ───────────
def winreg_user_writer(values: dict) -> tuple[bool, str | None]:
    """Write allowlisted values to HKCU\\Environment via the typed registry API.

    No subprocess, no ``setx``, no shell string is constructed anywhere on this path,
    so shell metacharacters have no execution context even in principle. A value of
    ``None`` deletes the variable (used by rollback to restore a previously-unset one).
    """
    try:
        import winreg  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return False, "winreg_unavailable"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0,
                            winreg.KEY_SET_VALUE) as key:
            for name, value in values.items():
                canon = validate_variable(name)      # allowlist again at the boundary
                if value is None:
                    try:
                        winreg.DeleteValue(key, canon)
                    except FileNotFoundError:
                        pass
                    continue
                winreg.SetValueEx(key, canon, 0, winreg.REG_SZ,
                                  validate_value(canon, value))
    except ValidationError as exc:
        return False, f"rejected:{exc.variable}"
    except PermissionError:
        return False, "PermissionError"
    except OSError as exc:
        return False, type(exc).__name__
    return True, None


@dataclass(frozen=True)
class PostureResult:
    """The outcome of ONE posture action. Never raises out of the dispatcher."""

    action: PostureAction
    ok: bool
    mutated: bool = False
    message: str = ""
    detail: dict = field(default_factory=dict)

    def snapshot(self) -> dict:
        return {"action": self.action.value, "ok": self.ok, "mutated": self.mutated,
                "message": self.message, "detail": dict(self.detail)}


class PostureController:
    """The single seam through which every posture action passes.

    Constructed with injectable collaborators so the whole workflow — including the
    refusal paths — is testable with no registry, no Ollama and no Windows.
    """

    def __init__(self, *, truth_provider: Callable[..., OllamaProcessTruth] | None = None,
                 writer: Callable[[dict], tuple[bool, str | None]] | None = None,
                 journal: PostureJournal | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._truth_provider = truth_provider or (lambda **kw: get_process_truth(**kw))
        self._writer = writer or winreg_user_writer
        self._journal = journal if journal is not None else PostureJournal()
        self._clock = clock

    # ── read-only ────────────────────────────────────────────────────────────
    def status(self) -> PostureResult:
        truth = self._truth_provider()
        detail = truth.snapshot()
        detail["recommended"] = recommend_posture()
        last = self._journal.last_applied()
        detail["managed_state"] = last.get("target") if last else None
        return PostureResult(action=PostureAction.STATUS, ok=True, mutated=False,
                             message=truth.summary(), detail=detail)

    def plan(self, *, target: dict | None = None, profile: str | None = None) -> PostureResult:
        truth = self._truth_provider()
        try:
            plan = build_plan(truth=truth, target=target, profile=profile,
                              clock=self._clock)
        except ValidationError as exc:
            return PostureResult(action=PostureAction.PLAN, ok=False, mutated=False,
                                 message=f"rejected: {exc}",
                                 detail={"variable": exc.variable, "reason": exc.reason})
        return PostureResult(action=PostureAction.PLAN, ok=True, mutated=False,
                             message=plan.rationale, detail=plan.snapshot())

    def dry_run(self, *, target: dict | None = None,
                profile: str | None = None) -> PostureResult:
        truth = self._truth_provider()
        try:
            plan = build_plan(truth=truth, target=target, profile=profile,
                              clock=self._clock)
        except ValidationError as exc:
            return PostureResult(action=PostureAction.DRY_RUN, ok=False, mutated=False,
                                 message=f"rejected: {exc}",
                                 detail={"variable": exc.variable, "reason": exc.reason})
        detail = plan.snapshot()
        detail["preview"] = plan.render()
        return PostureResult(action=PostureAction.DRY_RUN, ok=True, mutated=False,
                             message=plan.render(), detail=detail)

    # ── effectful (operator/HITL only) ───────────────────────────────────────
    def apply(self, *, target: dict | None = None,
              authorization: OperatorAuthorization | None = None,
              profile: str | None = None) -> PostureResult:
        """Write the plan. Requires an authorization that covers THIS exact target.

        Never restarts anything: the change is inert until the operator restarts the
        Ollama server themselves, and ``verify`` is the only path that may then claim
        it took effect.
        """
        truth = self._truth_provider()
        try:
            plan = build_plan(truth=truth, target=target, profile=profile,
                              clock=self._clock)
        except ValidationError as exc:
            return PostureResult(action=PostureAction.APPLY, ok=False, mutated=False,
                                 message=f"rejected: {exc}",
                                 detail={"variable": exc.variable, "reason": exc.reason})
        if authorization is None or not authorization.covers(PostureAction.APPLY,
                                                             plan.target):
            return PostureResult(
                action=PostureAction.APPLY, ok=False, mutated=False,
                message=("operator authorization required: apply is refused without an "
                         "explicit approval covering this exact target"),
                detail={"authorization": "missing_or_mismatched",
                        "target": plan.target})
        if plan.is_noop():
            return PostureResult(action=PostureAction.APPLY, ok=True, mutated=False,
                                 message="no change required; posture already matches",
                                 detail=plan.snapshot())

        # Capture the PREVIOUS user-scope values verbatim so rollback is exact. A
        # variable absent from the scope is recorded as None -> rollback deletes it.
        previous = {c.variable: truth.user_env.get(c.variable) for c in plan.changes}
        ok, err = self._writer(plan.target)
        entry = {
            "action": PostureAction.APPLY.value,
            "at": self._clock(),
            "scope": plan.scope.value,
            "target": plan.target,
            "previous": previous,
            "ok": bool(ok),
            "error": err,
            "operator": authorization.operator,
            "reason": authorization.reason,
            "restart_required": plan.restart_required,
        }
        journaled = self._journal.append(entry)
        if not ok:
            return PostureResult(action=PostureAction.APPLY, ok=False, mutated=False,
                                 message=f"write failed: {err}",
                                 detail={"error": err, "journaled": journaled})
        return PostureResult(
            action=PostureAction.APPLY, ok=True, mutated=True,
            message=("posture written to the current user's environment; the RUNNING "
                     "server keeps its launch-time values until you restart it yourself"),
            detail={"target": plan.target, "previous": previous,
                    "journaled": journaled, "restart_required": plan.restart_required,
                    "server_restarted": False})

    def verify(self) -> PostureResult:
        """Compare the managed target against the SERVER PROCESS's own environment.

        Read-only, and deliberately unable to succeed from configuration alone: with an
        unreadable server block the answer is UNVERIFIABLE, never "applied".
        """
        last = self._journal.last_applied()
        truth = self._truth_provider(refresh=True)
        if last is None:
            return PostureResult(action=PostureAction.VERIFY, ok=True, mutated=False,
                                 message=VerifyState.NO_MANAGED_STATE.value,
                                 detail={"state": VerifyState.NO_MANAGED_STATE.value})
        target = dict(last.get("target") or {})
        if not truth.server_env_readable:
            state = (VerifyState.UNVERIFIABLE if truth.candidates
                     else VerifyState.RESTART_PENDING)
            return PostureResult(
                action=PostureAction.VERIFY, ok=True, mutated=False,
                message=state.value,
                detail={"state": state.value, "target": target,
                        "reason": ("the server process environment could not be read; "
                                   "configuration files and registry values are not "
                                   "proof that the running server uses them")})
        server_env = truth.server_env()
        mismatches = {k: {"target": v, "server": server_env.get(k)}
                      for k, v in target.items() if server_env.get(k) != v}
        applied_at = float(last.get("at") or 0.0)
        primary = truth.primary
        started = primary.create_time if primary is not None else None
        predates = bool(started is not None and started < applied_at)
        if not mismatches:
            state = VerifyState.VERIFIED_APPLIED
        elif predates:
            state = VerifyState.RESTART_PENDING
        else:
            state = VerifyState.VERIFIED_NOT_APPLIED
        return PostureResult(
            action=PostureAction.VERIFY, ok=True, mutated=False, message=state.value,
            detail={"state": state.value, "target": target, "server_env": server_env,
                    "mismatches": mismatches,
                    "server_predates_apply": predates,
                    "source": PostureSource.SERVER_INHERITANCE_VERIFIED.value})

    def rollback(self, *,
                 authorization: OperatorAuthorization | None = None) -> PostureResult:
        """Restore the previous managed values from the journal. Operator/HITL only."""
        last = self._journal.last_applied()
        if last is None:
            return PostureResult(action=PostureAction.ROLLBACK, ok=False, mutated=False,
                                 message="nothing to roll back",
                                 detail={"state": VerifyState.NO_MANAGED_STATE.value})
        previous = dict(last.get("previous") or {})
        # Validate everything we are about to restore; a corrupted journal must not
        # become a write primitive. None (delete) is legitimate and skips value checks.
        restore: dict = {}
        for name, value in previous.items():
            try:
                canon = validate_variable(name)
                restore[canon] = None if value is None else validate_value(canon, value)
            except ValidationError as exc:
                return PostureResult(action=PostureAction.ROLLBACK, ok=False,
                                     mutated=False,
                                     message=f"journal entry rejected: {exc}",
                                     detail={"variable": exc.variable,
                                             "reason": exc.reason})
        if authorization is None or not authorization.covers(
                PostureAction.ROLLBACK, {k: v for k, v in restore.items() if v is not None}):
            return PostureResult(
                action=PostureAction.ROLLBACK, ok=False, mutated=False,
                message=("operator authorization required: rollback is refused without "
                         "an explicit approval covering the restored values"),
                detail={"authorization": "missing_or_mismatched", "restore": restore})
        ok, err = self._writer(restore)
        entry = {"action": PostureAction.ROLLBACK.value, "at": self._clock(),
                 "scope": last.get("scope"), "target": restore,
                 "previous": dict(last.get("target") or {}),
                 "ok": bool(ok), "error": err, "operator": authorization.operator,
                 "reason": authorization.reason, "restart_required": True}
        journaled = self._journal.append(entry)
        if not ok:
            return PostureResult(action=PostureAction.ROLLBACK, ok=False, mutated=False,
                                 message=f"rollback write failed: {err}",
                                 detail={"error": err, "journaled": journaled})
        return PostureResult(
            action=PostureAction.ROLLBACK, ok=True, mutated=True,
            message=("previous posture restored; the running server is unaffected "
                     "until you restart it yourself"),
            detail={"restored": restore, "journaled": journaled,
                    "server_restarted": False})

    # ── dispatcher ───────────────────────────────────────────────────────────
    def dispatch(self, action: PostureAction | str, *, target: dict | None = None,
                 authorization: OperatorAuthorization | None = None,
                 profile: str | None = None) -> PostureResult:
        """Route ONE action. Read-only actions can never receive the writer."""
        try:
            act = action if isinstance(action, PostureAction) else PostureAction(str(action).lower())
        except ValueError:
            return PostureResult(action=PostureAction.STATUS, ok=False, mutated=False,
                                 message="unknown posture action",
                                 detail={"requested": str(action)[:32]})
        if act is PostureAction.STATUS:
            result = self.status()
        elif act is PostureAction.PLAN:
            result = self.plan(target=target, profile=profile)
        elif act is PostureAction.DRY_RUN:
            result = self.dry_run(target=target, profile=profile)
        elif act is PostureAction.APPLY:
            result = self.apply(target=target, authorization=authorization,
                                profile=profile)
        elif act is PostureAction.VERIFY:
            result = self.verify()
        else:
            result = self.rollback(authorization=authorization)
        if act in READ_ONLY_ACTIONS and result.mutated:   # pragma: no cover - invariant
            raise AssertionError(f"read-only action {act.value} reported a mutation")
        return result


# ── Operator command surface (deterministic parse; no LLM selection) ─────────
_COMMAND_PREFIXES = ("ollama-posture-", "/ollama-posture-", "/posture-")
_ALIASES = {"dry_run": "dry-run", "dryrun": "dry-run"}


def parse_posture_command(text: str) -> PostureAction | None:
    """Parse an operator posture command. Returns None for anything else.

    Deliberately accepts NO arguments: the target is never taken from free text, so
    no variable name, value or scope can arrive from a transcript or a model. The
    action alone is selected here; values come from the validated recommendation or an
    explicit operator surface.
    """
    raw = (text or "").strip().lower()
    if not raw or len(raw) > 64:
        return None
    for prefix in _COMMAND_PREFIXES:
        if raw.startswith(prefix):
            verb = _ALIASES.get(raw[len(prefix):].strip(), raw[len(prefix):].strip())
            try:
                return PostureAction(verb)
            except ValueError:
                return None
    return None
