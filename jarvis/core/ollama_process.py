"""core/ollama_process.py — V69 M56.1: read-only Ollama process & service discovery.

WHY THIS EXISTS
---------------
M55.5 established the honest posture categories on the API side: JARVIS's
RECOMMENDED values are not this process's environment, and neither is the SERVER's.
But it stopped at "unknown": the Ollama API does not expose OLLAMA_NUM_PARALLEL or
OLLAMA_MAX_LOADED_MODELS, so the live run could only say so.

There is exactly one honest way to learn what the running server actually inherited:
read the SERVER PROCESS's own environment block. That is what this module does — and
it is careful to claim verification ONLY from that source. A value present in the
Windows user/machine registry environment proves nothing about a server that launched
before the value was written, and matching values are a coincidence, not a proof.

TRUTH CATEGORIES (never conflated)
----------------------------------
  RECOMMENDED                  what JARVIS's hardware profile advises
  JARVIS_PROCESS               os.environ of THIS python process
  WINDOWS_USER_ENV             HKCU\\Environment (persistent, future processes)
  WINDOWS_MACHINE_ENV          HKLM ... \\Session Manager\\Environment
  SERVER_PROCESS_OBSERVED      a likely ollama server process was found (pid/exe/start)
  SERVER_INHERITANCE_VERIFIED  the server process's OWN environment block was read
  SERVER_BEHAVIOR_OBSERVED     the server's behavior implies it (residency evidence)
  UNKNOWN                      none of the above established it

WHAT THIS MODULE NEVER DOES
---------------------------
It never starts, stops, restarts, kills or reconfigures anything; it never writes to
the registry; it never captures a process command line (which can carry secrets — only
an argument COUNT is recorded); and it never runs a WMI query or an unbounded process
scan on the interactive path (one bounded ``psutil`` pass, TTL-cached).

Everything is injectable so the full state machine is testable with no live server and
no live Windows: :func:`collect_process_truth` takes a process source, a registry
reader and a clock.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable

# The ONLY variables this subsystem reads or reasons about anywhere in M56.
POSTURE_VARS: tuple[str, ...] = (
    "OLLAMA_NUM_PARALLEL",
    "OLLAMA_MAX_LOADED_MODELS",
    "OLLAMA_KEEP_ALIVE",
)
# Additionally reported (read-only) because it identifies the endpoint, not posture.
_CONTEXT_VARS: tuple[str, ...] = ("OLLAMA_HOST",)
_ALL_READ_VARS: tuple[str, ...] = POSTURE_VARS + _CONTEXT_VARS

# Bounded discovery: a host with more than this many ollama processes is reported as
# ambiguous rather than scanned further.
_MAX_CANDIDATES = 8
_DEFAULT_TTL_S = 30.0

_HKCU_ENV = r"Environment"
_HKLM_ENV = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"

# Process names that identify a candidate Ollama server.
_OLLAMA_NAMES = frozenset({"ollama.exe", "ollama", "ollama_llama_server.exe",
                           "ollama-windows-amd64.exe"})
# Parent-process names that classify how the server was launched.
_PARENT_SERVICE = frozenset({"services.exe"})
_PARENT_SCHEDULED = frozenset({"taskeng.exe", "svchost.exe", "schtasks.exe"})
_PARENT_INTERACTIVE = frozenset({"explorer.exe", "cmd.exe", "powershell.exe",
                                 "pwsh.exe", "windowsterminal.exe", "conhost.exe",
                                 "code.exe", "bash.exe", "sh.exe"})
# The Ollama desktop tray application. On this host the live server is its child
# (observed M56.1: ollama.exe pid parent = "ollama app.exe"), and the tray app installs
# itself as a per-user logon item — so the server is a STARTUP APP, not a service. This
# matters operationally: its environment comes from the USER scope at LOGON, which is
# why a user-scope change needs a full tray-app restart (not just `ollama serve`).
_PARENT_TRAY_APP = frozenset({"ollama app.exe", "ollama app", "ollamaapp.exe"})


class PostureSource(str, Enum):
    """Where a posture value came from. Ordering is NOT strength — read the name."""

    RECOMMENDED = "RECOMMENDED"
    JARVIS_PROCESS = "JARVIS_PROCESS"
    WINDOWS_USER_ENV = "WINDOWS_USER_ENV"
    WINDOWS_MACHINE_ENV = "WINDOWS_MACHINE_ENV"
    SERVER_PROCESS_OBSERVED = "SERVER_PROCESS_OBSERVED"
    SERVER_INHERITANCE_VERIFIED = "SERVER_INHERITANCE_VERIFIED"
    SERVER_BEHAVIOR_OBSERVED = "SERVER_BEHAVIOR_OBSERVED"
    UNKNOWN = "UNKNOWN"


class LaunchMode(str, Enum):
    """How the discovered server process appears to have been started."""

    MANUAL = "MANUAL"                    # started from an interactive shell/session
    STARTUP_APP = "STARTUP_APP"          # launched by the user's shell at logon
    WINDOWS_SERVICE = "WINDOWS_SERVICE"  # child of services.exe
    SCHEDULED_TASK = "SCHEDULED_TASK"    # child of the task/service host
    UNKNOWN = "UNKNOWN"                  # not discoverable (or permission denied)


class DiscoveryState(str, Enum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    NO_PROCESS_FOUND = "NO_PROCESS_FOUND"
    SINGLE_CANDIDATE = "SINGLE_CANDIDATE"
    MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    UNSUPPORTED = "UNSUPPORTED"          # psutil missing / non-Windows helper absent


@dataclass(frozen=True)
class ProcessCandidate:
    """One discovered candidate server process. SAFE fields only.

    ``arg_count`` replaces the command line deliberately: a command line can carry an
    API key or a path that identifies the operator, and this record is surfaced to
    logs, health snapshots and the operator UI.
    """

    pid: int
    name: str
    exe: str | None = None
    create_time: float | None = None
    parent_name: str | None = None
    arg_count: int | None = None
    env_readable: bool = False
    env: dict = field(default_factory=dict)     # allowlisted OLLAMA_* only
    error: str | None = None                    # e.g. "AccessDenied"

    def uptime_s(self, now: float | None = None) -> float | None:
        if self.create_time is None:
            return None
        return round(max(0.0, (now if now is not None else time.time()) - self.create_time), 1)

    def snapshot(self, *, now: float | None = None) -> dict:
        return {
            "pid": self.pid,
            "name": self.name,
            "exe": self.exe,
            "create_time": self.create_time,
            "uptime_s": self.uptime_s(now),
            "parent_name": self.parent_name,
            "arg_count": self.arg_count,
            "env_readable": self.env_readable,
            "env": dict(self.env),
            "error": self.error,
        }


def classify_launch_mode(candidate: ProcessCandidate | None) -> LaunchMode:
    """Classify a candidate by its PARENT process. Deterministic and total.

    Deliberately conservative: an unknown or unreadable parent is UNKNOWN, never a
    guess. Distinguishing STARTUP_APP from MANUAL is not decidable from the parent
    alone (explorer.exe is both the shell and the logon-item launcher), so an
    explorer parent reports STARTUP_APP only when the process is older than the
    JARVIS process could plausibly have started it — which we do not know here.
    Therefore explorer.exe maps to MANUAL and the operator is told the distinction
    is not provable.
    """
    if candidate is None:
        return LaunchMode.UNKNOWN
    parent = (candidate.parent_name or "").lower()
    if not parent:
        return LaunchMode.UNKNOWN
    if parent in _PARENT_SERVICE:
        return LaunchMode.WINDOWS_SERVICE
    if parent in _PARENT_SCHEDULED:
        return LaunchMode.SCHEDULED_TASK
    if parent in _PARENT_TRAY_APP:
        return LaunchMode.STARTUP_APP
    if parent in _PARENT_INTERACTIVE:
        return LaunchMode.MANUAL
    return LaunchMode.UNKNOWN


# ── Process source (injectable) ───────────────────────────────────────────────
def psutil_process_source() -> tuple[list[ProcessCandidate], DiscoveryState]:
    """One bounded ``psutil`` pass. Tolerates permission errors on every field.

    Returns (candidates, state). Never raises: a missing psutil yields UNSUPPORTED.
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return [], DiscoveryState.UNSUPPORTED

    found: list[ProcessCandidate] = []
    denied = 0
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            if len(found) >= _MAX_CANDIDATES:
                break
            try:
                name = (proc.info.get("name") or "").lower()
            except Exception:  # noqa: BLE001
                continue
            if name not in _OLLAMA_NAMES:
                continue
            pid = int(proc.info.get("pid") or 0)
            exe = create = parent_name = None
            arg_count = None
            env: dict = {}
            env_readable = False
            err: str | None = None
            try:
                exe = proc.exe()
            except Exception as exc:  # noqa: BLE001
                err = type(exc).__name__
                denied += 1
            try:
                create = proc.create_time()
            except Exception:  # noqa: BLE001
                pass
            try:
                parent = proc.parent()
                parent_name = (parent.name() if parent is not None else None)
            except Exception:  # noqa: BLE001
                pass
            try:
                # COUNT only — a command line may carry secrets and is never stored.
                arg_count = len(proc.cmdline() or [])
            except Exception:  # noqa: BLE001
                pass
            try:
                raw = proc.environ() or {}
                env = {k: raw.get(k) for k in _ALL_READ_VARS if k in raw}
                env_readable = True
            except Exception as exc:  # noqa: BLE001
                err = err or type(exc).__name__
                denied += 1
            found.append(ProcessCandidate(
                pid=pid, name=name, exe=exe, create_time=create,
                parent_name=parent_name, arg_count=arg_count,
                env_readable=env_readable, env=env, error=err,
            ))
    except Exception:  # noqa: BLE001
        if not found:
            return [], DiscoveryState.UNSUPPORTED

    if not found:
        return [], DiscoveryState.NO_PROCESS_FOUND
    if len(found) > 1:
        return found, DiscoveryState.MULTIPLE_CANDIDATES
    if denied and not found[0].env_readable and found[0].exe is None:
        return found, DiscoveryState.PERMISSION_DENIED
    return found, DiscoveryState.SINGLE_CANDIDATE


# ── Windows persistent environment (read-only registry) ───────────────────────
def winreg_env_reader(scope: str) -> tuple[dict, str | None]:
    """Read the allowlisted OLLAMA_* values from the persistent Windows environment.

    ``scope`` is ``"user"`` (HKCU) or ``"machine"`` (HKLM). READ-ONLY: the key is
    opened with KEY_READ and nothing is ever written. Returns (values, error) and
    never raises — a non-Windows host or a denied key yields ({}, reason).
    """
    try:
        import winreg  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return {}, "winreg_unavailable"
    if scope == "user":
        root, path = winreg.HKEY_CURRENT_USER, _HKCU_ENV
    elif scope == "machine":
        root, path = winreg.HKEY_LOCAL_MACHINE, _HKLM_ENV
    else:
        return {}, "unknown_scope"
    out: dict = {}
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            for var in _ALL_READ_VARS:
                try:
                    value, _kind = winreg.QueryValueEx(key, var)
                    out[var] = str(value)
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
    except PermissionError:
        return {}, "PermissionError"
    except FileNotFoundError:
        return {}, "key_missing"
    except OSError as exc:
        return {}, type(exc).__name__
    return out, None


# ── The truth record ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SettingTruth:
    """One posture variable resolved to a value AND the category that proves it."""

    name: str
    value: str | None
    source: PostureSource
    detail: str = ""

    @property
    def verified(self) -> bool:
        """Only a read of the SERVER process's own environment verifies a setting."""
        return self.source is PostureSource.SERVER_INHERITANCE_VERIFIED

    def snapshot(self) -> dict:
        return {"name": self.name, "value": self.value, "source": self.source.value,
                "verified": self.verified, "detail": self.detail}


@dataclass(frozen=True)
class OllamaProcessTruth:
    """A bounded, read-only view of the Ollama server process and env provenance."""

    discovered_at: float = 0.0
    state: DiscoveryState = DiscoveryState.NOT_ATTEMPTED
    candidates: tuple[ProcessCandidate, ...] = ()
    primary_pid: int | None = None
    launch_mode: LaunchMode = LaunchMode.UNKNOWN
    jarvis_env: dict = field(default_factory=dict)
    user_env: dict = field(default_factory=dict)
    machine_env: dict = field(default_factory=dict)
    user_env_error: str | None = None
    machine_env_error: str | None = None
    server_env_readable: bool = False

    @property
    def primary(self) -> ProcessCandidate | None:
        for c in self.candidates:
            if c.pid == self.primary_pid:
                return c
        return self.candidates[0] if self.candidates else None

    def server_env(self) -> dict:
        """The SERVER process's own OLLAMA_* block, or {} when it could not be read."""
        p = self.primary
        return dict(p.env) if (p is not None and p.env_readable) else {}

    def resolve(self, name: str) -> SettingTruth:
        """Resolve ONE posture variable to a value plus the category that proves it.

        Precedence is by EVIDENCE STRENGTH, not by convenience:
          1. the server process's own environment  -> SERVER_INHERITANCE_VERIFIED
          2. a server process exists but its env is unreadable -> the value is
             UNKNOWN, even when the same value sits in the Windows environment,
             because an already-running server may predate that value entirely.
        Windows user/machine values are reported separately as what a FUTURE server
        would inherit, never as the running server's configuration.
        """
        if name not in _ALL_READ_VARS:
            return SettingTruth(name=name, value=None, source=PostureSource.UNKNOWN,
                                detail="not an allowlisted posture variable")
        senv = self.server_env()
        if name in senv and senv[name] is not None:
            return SettingTruth(name=name, value=str(senv[name]),
                                source=PostureSource.SERVER_INHERITANCE_VERIFIED,
                                detail="read from the server process environment block")
        if self.server_env_readable:
            # We DID read the server's block and the variable is absent — that is a
            # verified absence, which is stronger than "unknown".
            return SettingTruth(name=name, value=None,
                                source=PostureSource.SERVER_INHERITANCE_VERIFIED,
                                detail="absent from the server process environment block")
        if self.candidates:
            return SettingTruth(
                name=name, value=None, source=PostureSource.UNKNOWN,
                detail="server process observed but its environment is unreadable; "
                       "Windows environment values are not proof of inheritance")
        return SettingTruth(name=name, value=None, source=PostureSource.UNKNOWN,
                            detail="no server process observed")

    def future_inheritance(self, name: str) -> SettingTruth:
        """What a server started FROM NOW ON would inherit for ``name``.

        Machine scope loses to user scope on Windows for a user-launched process, so
        user is reported first. This is a statement about future launches only.
        """
        if name in self.user_env:
            return SettingTruth(name=name, value=str(self.user_env[name]),
                                source=PostureSource.WINDOWS_USER_ENV,
                                detail="a future server launched by this user inherits it")
        if name in self.machine_env:
            return SettingTruth(name=name, value=str(self.machine_env[name]),
                                source=PostureSource.WINDOWS_MACHINE_ENV,
                                detail="a future server launched on this machine inherits it")
        return SettingTruth(name=name, value=None, source=PostureSource.UNKNOWN,
                            detail="not set in the persistent Windows environment")

    def restart_required(self) -> bool:
        """True when applying a persistent change could not affect the RUNNING server.

        Always true while a server process is observed: Windows reads a process's
        environment block at creation, so no registry write reaches it. With no
        server observed, the next launch picks the value up and no restart is needed.
        """
        return bool(self.candidates)

    def snapshot(self, *, now: float | None = None) -> dict:
        return {
            "discovered_at": self.discovered_at,
            "state": self.state.value,
            "candidate_count": len(self.candidates),
            "candidates": [c.snapshot(now=now) for c in self.candidates],
            "primary_pid": self.primary_pid,
            "launch_mode": self.launch_mode.value,
            "server_env_readable": self.server_env_readable,
            "jarvis_env": dict(self.jarvis_env),
            "windows_user_env": dict(self.user_env),
            "windows_machine_env": dict(self.machine_env),
            "user_env_error": self.user_env_error,
            "machine_env_error": self.machine_env_error,
            "restart_required": self.restart_required(),
            "resolved": {v: self.resolve(v).snapshot() for v in POSTURE_VARS},
            "future_inheritance": {v: self.future_inheritance(v).snapshot()
                                   for v in POSTURE_VARS},
        }

    def summary(self) -> str:
        """A compact ASCII one-liner (Windows/TTS-safe)."""
        p = self.primary
        return (
            "OLLAMA PROCESS: state={} candidates={} pid={} launch={} "
            "server_env_readable={} restart_required={}".format(
                self.state.value, len(self.candidates),
                p.pid if p is not None else "?", self.launch_mode.value,
                self.server_env_readable, self.restart_required(),
            )
        )


def _pick_primary(candidates: Iterable[ProcessCandidate]) -> ProcessCandidate | None:
    """The oldest candidate whose environment we could read, else simply the oldest.

    The long-lived parent is the serving process; short-lived children are runners.
    Preferring a readable environment maximizes the chance of a VERIFIED answer
    without ever inventing one.
    """
    cands = list(candidates)
    if not cands:
        return None
    readable = [c for c in cands if c.env_readable]
    pool = readable or cands
    return min(pool, key=lambda c: (c.create_time if c.create_time is not None else float("inf")))


def collect_process_truth(
    *,
    process_source: Callable[[], tuple[list[ProcessCandidate], DiscoveryState]] | None = None,
    env_reader: Callable[[str], tuple[dict, str | None]] | None = None,
    process_env: dict | None = None,
    clock: Callable[[], float] = time.time,
) -> OllamaProcessTruth:
    """Compose the read-only process/environment truth. Bounded and never raising."""
    src = process_source or psutil_process_source
    reader = env_reader or winreg_env_reader
    try:
        candidates, state = src()
    except Exception:  # noqa: BLE001
        candidates, state = [], DiscoveryState.UNSUPPORTED
    try:
        user_env, user_err = reader("user")
    except Exception as exc:  # noqa: BLE001
        user_env, user_err = {}, type(exc).__name__
    try:
        machine_env, machine_err = reader("machine")
    except Exception as exc:  # noqa: BLE001
        machine_env, machine_err = {}, type(exc).__name__

    penv = process_env if process_env is not None else os.environ
    jarvis_env = {k: penv.get(k) for k in _ALL_READ_VARS if penv.get(k) is not None}

    primary = _pick_primary(candidates)
    return OllamaProcessTruth(
        discovered_at=clock(),
        state=state,
        candidates=tuple(candidates),
        primary_pid=primary.pid if primary is not None else None,
        launch_mode=classify_launch_mode(primary),
        jarvis_env=jarvis_env,
        user_env=user_env,
        machine_env=machine_env,
        user_env_error=user_err,
        machine_env_error=machine_err,
        server_env_readable=bool(primary is not None and primary.env_readable),
    )


# ── Process-global TTL cache (no scan on the interactive path) ────────────────
_cached: OllamaProcessTruth | None = None


def get_process_truth(*, max_age_s: float = _DEFAULT_TTL_S, refresh: bool = False,
                      clock: Callable[[], float] = time.time, **kw) -> OllamaProcessTruth:
    """The cached truth, re-collected only when older than ``max_age_s``.

    A WMI loop or a full process scan per turn is exactly what this cache prevents;
    ``refresh=True`` is the operator's explicit way to pay for a fresh pass.
    """
    global _cached
    now = clock()
    if (not refresh and _cached is not None
            and (now - _cached.discovered_at) <= max_age_s):
        return _cached
    _cached = collect_process_truth(clock=clock, **kw)
    return _cached


def reset_process_truth(instance: OllamaProcessTruth | None = None) -> None:
    """Tests / a fresh process."""
    global _cached
    _cached = instance
