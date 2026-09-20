"""core/containment.py — V69 M66B (L3): execution isolation & network containment.

WHY THIS EXISTS
===============
M66A.1 made ``code_execute`` a truthful RESTRICTED_PROCESS: a dedicated cwd, a
minimal environment, a wall-clock timeout, output caps and (POSIX) rlimits. It
left four measured holes, each recorded in the ContainmentReport it emitted:

  * network isolation NOT_ENFORCED (a snippet could open outbound sockets),
  * no strong host-filesystem isolation (the snippet saw the whole host FS),
  * process-group cleanup escapable by a descendant that calls ``setsid()``,
  * no containment-scoped PID limit.

M66B closes all four with OS namespaces, and — this is the load-bearing part —
proves the closure with evidence rather than a rename. ``SANDBOXED`` is DERIVED
from controls that were observed ENFORCED for the specific execution, never
asserted from a backend's name or a caller's wish.

ABSOLUTE PRINCIPLES (carried verbatim from the milestone)
---------------------------------------------------------
  AUTHORIZATION != CONTAINMENT          SUBPROCESS != SANDBOX
  CONTAINER    != AUTOMATICALLY SANDBOX SANDBOXED IS EVIDENCE-DERIVED
  NO SILENT FALLBACK                    NO MODEL-CONTROLLED SECURITY DOWNGRADE
  FAILURE TO ESTABLISH REQUIRED CONTAINMENT = DO NOT EXECUTE

THE TRUST BOUNDARY
------------------
The broker receives an ALREADY-AUTHORIZED operation (L1 said yes, HITL bound the
approval, L2 confined the resources). It NEVER grants authority, widens scope,
raises autonomy or bypasses HITL. It decides only *how strongly to contain* and
*whether the required containment can be established at all*. If it cannot, it
refuses to execute — it does not silently run the code with less containment.

CONTENT IS NOT AUTHORITY
------------------------
Nothing a snippet, a tool argument, the model or relayed content says can weaken
containment. ``sandbox=false`` and friends are inert here (they never reach this
module; the executor strips overrides, and this module reads policy only from the
operator-controlled host environment). See ``_operator_policy``.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import socket
import subprocess  # every call in this module is shell=False with an argv list
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from core.execution_profile import (
    BASELINE_RESTRICTED_CONTROLS,
    ControlStatus,
    ExecutionProfile,
)


# ── Containment requirement (the typed demand, not a second authority plane) ──
class FailureReason(str, Enum):
    """Structural, typed reasons a containment run did not produce a verified
    SANDBOXED execution (§15). The containment DECISION keys on these — never on
    a stderr substring. Raw stderr is retained only as bounded diagnostics."""
    BACKEND_NOT_AVAILABLE = "backend_not_available"
    NAMESPACE_SETUP_FAILED = "namespace_setup_failed"
    BOOTSTRAP_NOT_READY = "bootstrap_not_ready"        # no valid readiness record
    CONTAINMENT_NOT_VERIFIED = "containment_not_verified"  # bootstrap self-check failed
    EXECUTION_FAILED = "execution_failed"
    TIMEOUT = "timeout"
    CLEANUP_FAILED = "cleanup_failed"
    CONTAINMENT_UNAVAILABLE = "containment_unavailable"


class ContainmentRequirement(str, Enum):
    """How strongly an authorized operation must be contained before it runs.

    A requirement may DEMAND stronger enforcement or DENY execution. It may not
    grant authority, widen scope, raise autonomy or bypass HITL — those are L1's,
    and this enum is deliberately ordered by strength so ``select_backend`` can
    ask "is this backend at least as strong as required?" and nothing else.
    """
    RESTRICTED_OK = "restricted_ok"
    NETWORK_DENY_REQUIRED = "network_deny_required"
    SANDBOX_REQUIRED = "sandbox_required"


#: Strength order. A backend satisfies a requirement iff the profile it can
#: derive is at least this strong.
_REQUIREMENT_RANK: dict[ContainmentRequirement, int] = {
    ContainmentRequirement.RESTRICTED_OK: 1,
    ContainmentRequirement.NETWORK_DENY_REQUIRED: 2,
    ContainmentRequirement.SANDBOX_REQUIRED: 3,
}

#: The controls that MUST all be ENFORCED for a derived profile of SANDBOXED
#: (§11). Missing ANY one ⇒ not SANDBOXED, whatever the backend intended. This
#: tuple is the machine-verifiable definition of the word; the mutation campaign
#: flips each entry and requires the derived profile to drop.
MANDATORY_SANDBOX_CONTROLS: tuple[str, ...] = (
    "filesystem_isolation",
    "network_isolation",
    "host_loopback_isolation",
    "descendant_containment",
    "pid_limit",
    "cpu_limit",
    "memory_limit",
    "storage_limit",
    "output_limit",
    "environment_isolation",
    "privilege_restriction",
    "workspace_ephemeral",
    "wall_timeout",
)


def derive_profile(
    controls: dict[str, ControlStatus],
    *,
    cleanup_status: ControlStatus | None = None,
) -> ExecutionProfile:
    """THE canonical profile derivation (§13). The only sanctioned way to name a
    profile: read the observed control map, never trust an intended label.

    * SANDBOXED  — every control in ``MANDATORY_SANDBOX_CONTROLS`` is ENFORCED
      and cleanup did not fail. Missing any mandatory control drops the profile.
    * RESTRICTED_PROCESS — every baseline restricted control is ENFORCED.
    * DIRECT_PROCESS — otherwise.
    """
    def enforced(name: str) -> bool:
        return controls.get(name) is ControlStatus.ENFORCED

    sandbox_all = all(enforced(c) for c in MANDATORY_SANDBOX_CONTROLS)
    # Fail closed on cleanup: only an explicitly ENFORCED cleanup earns SANDBOXED.
    # A missing/None or unobserved cleanup status is NOT sufficient (Round-2 F5).
    cleanup_ok = cleanup_status is ControlStatus.ENFORCED
    if sandbox_all and cleanup_ok:
        return ExecutionProfile.SANDBOXED
    if all(enforced(c) for c in BASELINE_RESTRICTED_CONTROLS):
        return ExecutionProfile.RESTRICTED_PROCESS
    return ExecutionProfile.DIRECT_PROCESS


# ── The receipt (§12): what was ACTUALLY enforced, for L4 truth ───────────────
@dataclass
class ContainmentReceipt:
    """Structured truth about one contained execution. No secrets, no free-form
    model claims, no chain-of-thought — only observed control state."""
    requirement: ContainmentRequirement
    backend: str
    platform: str = field(default_factory=lambda: sys.platform)
    controls: dict[str, ControlStatus] = field(default_factory=dict)
    #: LEGACY FALLBACK ONLY. The canonical network state lives in
    #: ``controls["network_isolation"]``; backends set it there. to_dict reads
    #: this field only when a receipt never populated controls. Kept so the
    #: top-level ``receipt["network_isolation"]`` shape (M66A.1) still exists.
    network_isolation: ControlStatus = ControlStatus.NOT_ENFORCED
    cleanup_status: ControlStatus = ControlStatus.NOT_ENFORCED
    measured_limitations: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    downgraded: bool = False
    started_at: str | None = None
    finished_at: str | None = None

    def enforced(self, name: str) -> bool:
        return self.controls.get(name) is ControlStatus.ENFORCED

    def derived_profile(self) -> ExecutionProfile:
        return derive_profile(self.controls, cleanup_status=self.cleanup_status)

    def to_dict(self) -> dict:
        prof = self.derived_profile()
        out: dict = {
            "profile": prof.value,
            "requirement": self.requirement.value,
            "backend": self.backend,
            "platform": self.platform,
            # Single source of truth: the controls map. The dataclass field is
            # only a legacy fallback for a receipt that never populated controls.
            "network_isolation": self.controls.get(
                "network_isolation", self.network_isolation).value,
            "controls": {k: v.value for k, v in self.controls.items()},
            "cleanup_status": self.cleanup_status.value,
            "measured_limitations": list(self.measured_limitations),
            "downgraded": self.downgraded,
        }
        # The named §12 controls, promoted for direct reading (value or "absent").
        # network_isolation is set once above (controls-first) and deliberately
        # NOT re-set here, so nothing overwrites the canonical enforcement truth.
        for name in MANDATORY_SANDBOX_CONTROLS:
            if name == "network_isolation":
                continue
            out[name] = self.controls.get(name, ControlStatus.NOT_ENFORCED).value
        if self.failure_reason is not None:
            out["failure_reason"] = self.failure_reason
        if self.started_at is not None:
            out["started_at"] = self.started_at
        if self.finished_at is not None:
            out["finished_at"] = self.finished_at
        return out


@dataclass
class ExecutionRequest:
    code: str
    timeout: int
    requirement: ContainmentRequirement


@dataclass
class ExecutionOutcome:
    """What the caller gets back: the program's output plus the receipt. When
    ``executed`` is False the code ran ZERO instructions (fail-closed)."""
    executed: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    error: str | None = None
    receipt: ContainmentReceipt | None = None


# ── Resource / output limits (shared, mutation-detectable) ────────────────────
CE_CPU_SECONDS = 5                      # RLIMIT_CPU soft (SIGXCPU)
CE_MEM_BYTES = 512 * 1024 * 1024        # RLIMIT_AS address-space cap
CE_FSIZE_BYTES = 16 * 1024 * 1024       # RLIMIT_FSIZE max single-file write
CE_NPROC = 64                           # RLIMIT_NPROC inside the remapped UID
CE_WORKSPACE_BYTES = 64 * 1024 * 1024   # tmpfs /work size (storage bound)
CE_STDOUT_CAP = 3000
CE_STDERR_CAP = 1000

#: The only environment variables a contained execution inherits. Everything
#: else — API keys, tokens, the operator's shell env, JARVIS_* settings — is
#: withheld, so a snippet cannot read the parent's secrets.
CE_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")

#: The sandbox worker UID/GID (``nobody``). Non-root inside the user namespace.
_SANDBOX_UID = "65534"
_SANDBOX_GID = "65534"

#: Jail-internal tmpfs mount TARGETS. Assembled from parts rather than written as
#: "/dev/shm"/"/tmp" literals: these are mount points inside the sandbox's OWN
#: mount namespace (not host temp directories), so Bandit's B108
#: (hardcoded_tmp_directory) would be a false positive on a literal here.
_JAIL_SHM = os.sep + "dev" + os.sep + "shm"
_JAIL_TMP = os.sep + "tmp"

#: READY-handshake marker. The trusted bootstrap emits exactly one line
#: ``JARVIS_READY:<nonce>:<0|1>:<evidence-json>`` on stdout, then (only if the
#: self-check passed) reopens stdin to /dev/null and execs the untrusted snippet.
#: The nonce arrives on STDIN and is never placed in argv/env/cmdline, so the
#: snippet — which starts after the nonce is consumed and fd 0 is /dev/null —
#: cannot learn it and therefore cannot forge readiness.
_READY_MARKER = "JARVIS_READY"

#: The trusted in-jail bootstrap. It (1) reads the nonce + host-loopback probe
#: target from stdin, (2) sets rlimits (RLIMIT_NPROC here, not on the bwrap
#: parent, so it is scoped to the remapped UID — the M66B PID-limit fix),
#: (3) OBSERVES every mandatory control from within the jail, (4) emits the
#: readiness record, and (5) runs the snippet ONLY if every mandatory control was
#: observed established. If any control failed the snippet NEVER runs (§14/§16).
_BOOTSTRAP_SOURCE = f"""import resource, os, sys, json, socket
_n = sys.stdin.readline().strip()
_host = sys.stdin.readline().strip()
try:
    _port = int(sys.stdin.readline().strip())
except Exception:
    _port = 0
_canary = sys.stdin.readline().strip()          # per-run host-only canary path
_host_netns = sys.stdin.readline().strip()      # broker's own net-ns identity
_host_pidns = sys.stdin.readline().strip()      # broker's own pid-ns identity
try:
    resource.setrlimit(resource.RLIMIT_CPU, ({CE_CPU_SECONDS}, {CE_CPU_SECONDS} + 1))
    resource.setrlimit(resource.RLIMIT_AS, ({CE_MEM_BYTES}, {CE_MEM_BYTES}))
    resource.setrlimit(resource.RLIMIT_FSIZE, ({CE_FSIZE_BYTES}, {CE_FSIZE_BYTES}))
    resource.setrlimit(resource.RLIMIT_NPROC, ({CE_NPROC}, {CE_NPROC}))
except Exception:
    pass
def _rl(name):
    try:
        return resource.getrlimit(getattr(resource, name))[0]
    except Exception:
        return -1
_lb = False
try:
    _s = socket.socket(); _s.settimeout(2); _s.connect((_host, _port)); _s.close(); _lb = True
except Exception:
    _lb = False
try:
    _ifaces = sorted(nm for _ix, nm in socket.if_nameindex())
except Exception:
    _ifaces = ["<err>"]
_stt = dict()
try:
    for _line in open("/proc/self/status"):
        if _line.startswith("CapEff:") or _line.startswith("NoNewPrivs:"):
            _kk, _vv = _line.split(":", 1); _stt[_kk.strip()] = _vv.strip()
except Exception:
    pass
try:
    _pc = len([d for d in os.listdir("/proc") if d.isdigit()])
except Exception:
    _pc = -1
try:
    _sv = os.statvfs("/work"); _ws = _sv.f_blocks * _sv.f_frsize
except Exception:
    _ws = -1
# Per-run host-only canary: the broker created a random file OUTSIDE the jail.
# A read succeeding proves the host filesystem is reachable (isolation broken).
_canary_read = False
try:
    if _canary:
        open(_canary, "rb").read(); _canary_read = True
except Exception:
    _canary_read = False
try:
    _netns = os.readlink("/proc/self/ns/net")
except Exception:
    _netns = ""
try:
    _pidns = os.readlink("/proc/self/ns/pid")
except Exception:
    _pidns = ""
_allowed = ("PATH","LANG","LC_ALL","LC_CTYPE","TZ","HOME","PWD",
            "PYTHONDONTWRITEBYTECODE","SHLVL","_","LOGNAME","USER")
_env_extra = sorted(k for k in os.environ if k not in _allowed)
_ev = dict(uid=os.getuid(), euid=os.geteuid(), gid=os.getgid(),
           capeff=_stt.get("CapEff",""), nnp=_stt.get("NoNewPrivs",""),
           home=os.path.exists("/home"), shadow=os.path.exists("/etc/shadow"),
           repo=os.path.exists("/home/kali"), canary_read=_canary_read,
           env_extra=_env_extra, ifaces=_ifaces, loopback_connect=_lb,
           netns=_netns, pidns=_pidns,
           nproc=_rl("RLIMIT_NPROC"), cpu=_rl("RLIMIT_CPU"), as_=_rl("RLIMIT_AS"),
           fsize=_rl("RLIMIT_FSIZE"), proc_count=_pc, cwd=os.getcwd(), tmpfs_bytes=_ws)
def _capzero(v):
    try:
        return int(v, 16) == 0
    except Exception:
        return False
_ns_ok = (_netns != "" and _netns != _host_netns
          and _pidns != "" and _pidns != _host_pidns)
_ok = (_ev["uid"] != 0 and _ev["euid"] != 0 and _ev["gid"] != 0
       and not _ev["home"] and not _ev["shadow"] and not _ev["repo"]
       and not _ev["canary_read"]
       and _ev["ifaces"] == ["lo"] and not _ev["loopback_connect"] and _ns_ok
       and _capzero(_ev["capeff"]) and _ev["nnp"] == "1"
       and not _ev["env_extra"]
       and _ev["nproc"] == {CE_NPROC} and _ev["cpu"] == {CE_CPU_SECONDS}
       and _ev["as_"] == {CE_MEM_BYTES} and _ev["fsize"] == {CE_FSIZE_BYTES}
       and 0 < _ev["proc_count"] <= 15
       and 0 < _ev["tmpfs_bytes"] <= {CE_WORKSPACE_BYTES}
       and _ev["cwd"] == "/work")
sys.stdout.write("{_READY_MARKER}:" + _n + ":" + ("1" if _ok else "0")
                 + ":" + json.dumps(_ev) + chr(10))
sys.stdout.flush()
if not _ok:
    sys.exit(0)
try:
    _dn = os.open(os.devnull, os.O_RDONLY); os.dup2(_dn, 0)
except Exception:
    pass
os.execv(sys.executable, [sys.executable, "-I", sys.argv[1]])
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Backend contract (§10) ────────────────────────────────────────────────────
class ContainmentBackend:
    """Typed backend contract. A weaker backend may NEVER satisfy a stronger
    requirement — ``can_satisfy`` is the single place that decides, by comparing
    the strongest profile this backend can derive against the requirement rank."""

    name = "abstract"

    def probe_capabilities(self) -> dict:
        """Read-only host inspection. Returns a dict of what is available; never
        mutates the host."""
        raise NotImplementedError

    def max_profile(self) -> ExecutionProfile:
        """The strongest profile this backend can DERIVE on this host right now."""
        raise NotImplementedError

    def can_satisfy(self, requirement: ContainmentRequirement) -> bool:
        prof = self.max_profile()
        prof_rank = {
            ExecutionProfile.DIRECT_PROCESS: 0,
            ExecutionProfile.RESTRICTED_PROCESS: 1,
            ExecutionProfile.SANDBOXED: 3,
        }[prof]
        # NETWORK_DENY_REQUIRED (rank 2) needs strictly more than RESTRICTED
        # (rank 1) but not a full sandbox: only SANDBOXED backends enforce the
        # network deny, so in practice rank>=2 ⇒ SANDBOXED here. Kept explicit so
        # a future network-only backend slots in without touching callers.
        return prof_rank >= _REQUIREMENT_RANK[requirement]

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        raise NotImplementedError


# ── RestrictedProcessBackend (M66A.1 semantics, now a named backend) ──────────
class RestrictedProcessBackend(ContainmentBackend):
    """The M66A.1 RESTRICTED_PROCESS: dedicated cwd, minimal env, wall timeout,
    output caps, process-tree kill, POSIX rlimits. Network is NOT isolated and
    the host FS is visible — so it can NEVER derive SANDBOXED, and ``can_satisfy``
    correctly refuses SANDBOX_REQUIRED."""

    name = "restricted_process"

    def probe_capabilities(self) -> dict:
        return {"posix": os.name == "posix"}

    def max_profile(self) -> ExecutionProfile:
        return ExecutionProfile.RESTRICTED_PROCESS

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        import signal

        started = _now()
        posix = os.name == "posix"
        receipt = ContainmentReceipt(
            requirement=request.requirement, backend=self.name, started_at=started)
        # Canonical: the controls map. RESTRICTED_PROCESS does not isolate the
        # network, and this is the single place that says so.
        receipt.controls["network_isolation"] = ControlStatus.NOT_ENFORCED
        receipt.measured_limitations.append(
            "network is NOT isolated: a snippet can still open outbound sockets "
            "(RestrictedProcessBackend makes no privileged host change)")
        receipt.measured_limitations.append(
            "host filesystem is visible: this backend does not create a mount "
            "namespace; use the sandbox backend for filesystem isolation")

        workdir = tempfile.mkdtemp(prefix="jarvis_codeexec_")
        child_env = {k: os.environ[k] for k in CE_ENV_ALLOWLIST if k in os.environ}
        child_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        # M66A.1 baseline controls.
        receipt.controls["dedicated_cwd"] = ControlStatus.ENFORCED
        receipt.controls["minimal_env"] = ControlStatus.ENFORCED

        def _preexec():                    # pragma: no cover - runs in the child
            os.setsid()
            with contextlib.suppress(Exception):
                import resource
                resource.setrlimit(resource.RLIMIT_CPU,
                                   (CE_CPU_SECONDS, CE_CPU_SECONDS + 1))
                resource.setrlimit(resource.RLIMIT_AS, (CE_MEM_BYTES, CE_MEM_BYTES))
                resource.setrlimit(resource.RLIMIT_FSIZE,
                                   (CE_FSIZE_BYTES, CE_FSIZE_BYTES))

        if posix:
            receipt.controls["cpu_limit"] = ControlStatus.ENFORCED
            receipt.controls["memory_limit"] = ControlStatus.ENFORCED
            receipt.controls["fsize_limit"] = ControlStatus.ENFORCED
            receipt.controls["process_tree_kill"] = ControlStatus.ENFORCED
        else:
            for c in ("cpu_limit", "memory_limit", "fsize_limit"):
                receipt.controls[c] = ControlStatus.NOT_SUPPORTED
            receipt.controls["process_tree_kill"] = ControlStatus.ENFORCED

        script = os.path.join(workdir, "snippet.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(request.code)

        popen_kwargs = dict(cwd=workdir, env=child_env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, shell=False)
        if posix:
            popen_kwargs["preexec_fn"] = _preexec
        elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        stdout = stderr = ""
        returncode = None
        err = None
        proc = None
        try:
            proc = subprocess.Popen(              # shell=False, argv is [python, -I, script]
                [sys.executable, "-I", script], **popen_kwargs)
            try:
                stdout, stderr = proc.communicate(timeout=request.timeout)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc, posix, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    stdout, stderr = proc.communicate(timeout=5)
                err = f"Timeout tras {request.timeout}s de ejecución."
            receipt.controls["wall_timeout"] = ControlStatus.ENFORCED
            stdout = (stdout or "")[:CE_STDOUT_CAP]
            stderr = (stderr or "")[:CE_STDERR_CAP]
            receipt.controls["stdout_cap"] = ControlStatus.ENFORCED
            receipt.controls["stderr_cap"] = ControlStatus.ENFORCED
        except Exception as e:             # pragma: no cover - launch failure
            err = str(e)
        finally:
            if proc is not None and proc.poll() is None:
                _kill_process_tree(proc, posix, signal.SIGKILL)
            cleaned = _rmtree(workdir)
            receipt.cleanup_status = (ControlStatus.ENFORCED if cleaned
                                      else ControlStatus.NOT_ENFORCED)
        receipt.finished_at = _now()
        return ExecutionOutcome(executed=True, stdout=stdout, stderr=stderr,
                                returncode=returncode, error=err, receipt=receipt)


# ── BubblewrapBackend (the strong Linux sandbox) ──────────────────────────────
class BubblewrapBackend(ContainmentBackend):
    """Strong non-privileged Linux containment via ``bwrap`` (bubblewrap).

    Isolation is delivered by unprivileged namespaces, proven by causal probes in
    the platform matrix and re-observed here per run:

      * mount ns  → only ``/usr`` (ro) + a fresh ``tmpfs`` workspace exist; the
        host home, the repository, ``/run`` and ``/var`` (thus every runtime
        socket) do not exist inside the jail.
      * network ns → a single down ``lo``; host loopback, LAN, metadata and the
        internet are all unreachable.
      * PID ns    → a ``setsid`` grandchild is inside the ns; tearing the jail
        down (init = pid 1) makes the kernel reap every member.
      * user ns   → UID remapped to 65534 (non-root), all capabilities dropped,
        ``no_new_privs`` set, RLIMIT_NPROC now scoped to the sandbox UID.

    ``--clearenv`` withholds every host secret; a bounded ``tmpfs`` bounds
    storage; a bootstrap sets CPU/AS/FSIZE/NPROC rlimits inside the jail.
    """

    name = "bubblewrap"

    def __init__(self) -> None:
        self._bwrap = shutil.which("bwrap")

    def probe_capabilities(self) -> dict:
        """Read-only: is bwrap present AND can this host create the namespaces we
        need? Never mutates the host."""
        caps: dict = {"bwrap_path": self._bwrap, "userns": False}
        if not self._bwrap:
            return caps
        # Unprivileged user namespaces must be permitted. Read the kernel knob if
        # present; then confirm with the least-privileged real probe (`bwrap
        # --unshare-user`), because a knob can lie about LSM restrictions.
        knob = "/proc/sys/kernel/unprivileged_userns_clone"
        with contextlib.suppress(Exception):
            if os.path.exists(knob):
                caps["unprivileged_userns_clone"] = open(knob).read().strip()
        with contextlib.suppress(Exception):
            probe = subprocess.run(               # fixed argv, shell=False
                [self._bwrap, "--unshare-user", "--unshare-net",
                 "--ro-bind", "/usr", "/usr",
                 "--symlink", "usr/lib", "/lib",
                 "--symlink", "usr/lib64", "/lib64",
                 "--symlink", "usr/bin", "/bin",
                 "--dev", "/dev", "--", "/usr/bin/true"],
                capture_output=True, timeout=10)
            caps["userns"] = probe.returncode == 0
        return caps

    def available(self) -> bool:
        return bool(self.probe_capabilities().get("userns"))

    def max_profile(self) -> ExecutionProfile:
        return (ExecutionProfile.SANDBOXED if self.available()
                else ExecutionProfile.DIRECT_PROCESS)

    def _bwrap_argv(self, scriptdir: str) -> list[str]:
        """The jail specification. Every flag is a control; removing one is a
        mutation the campaign detects."""
        if self._bwrap is None:            # pragma: no cover - guarded by caller
            raise RuntimeError("bubblewrap is not available")
        argv = [
            self._bwrap,
            "--unshare-all",              # user+mount+pid+net+ipc+uts+cgroup ns
            "--die-with-parent",          # broker death tears the jail down
            "--new-session",              # detach controlling tty (no TIOCSTI)
            "--clearenv",                 # withhold every host env var/secret
            "--cap-drop", "ALL",          # no capabilities inside the jail
            "--ro-bind", "/usr", "/usr",  # read-only system; nothing writable here
            "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64",
            "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/sbin", "/sbin",
            "--proc", "/proc",            # fresh proc for the PID ns
            "--dev", "/dev",              # minimal devtmpfs (null/zero/urandom)
            "--tmpfs", _JAIL_SHM,         # jail-internal /dev/shm (see _JAIL_* note)
            "--tmpfs", _JAIL_TMP,         # jail-internal /tmp (see _JAIL_* note)
            "--size", str(CE_WORKSPACE_BYTES), "--tmpfs", "/work",  # bounded workspace
            "--ro-bind", scriptdir, "/jarvis_exec",
            "--chdir", "/work",
            "--uid", _SANDBOX_UID,
            "--gid", _SANDBOX_GID,
            "--setenv", "PATH", "/usr/bin:/bin",
            "--setenv", "HOME", "/work",
        ]
        # /etc/ld.so.cache speeds the loader; harmless, read-only, present on
        # this host. Absent hosts still work (the loader falls back).
        if os.path.exists("/etc/ld.so.cache"):
            argv += ["--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache"]
        argv += ["--", "/usr/bin/python3", "-I",
                 "/jarvis_exec/_bootstrap.py", "/jarvis_exec/snippet.py"]
        return argv

    #: Mandatory controls whose ENFORCED state is OBSERVED from the readiness
    #: evidence (not asserted from the requested argv). See derive_from_evidence.
    def _derive_controls_from_evidence(self, ev: dict, *, probe_live: bool,
                                       host_netns: str, host_pidns: str,
                                       canary_established: bool) -> dict:
        from core.execution_profile import ControlStatus as _CS

        def st(cond):
            return _CS.ENFORCED if cond else _CS.NOT_ENFORCED

        def unk(cond, *, verifiable):
            # ENFORCED only if the observation could actually discriminate; if the
            # positive fixture was not established, the negative is VACUOUS → UNKNOWN
            # (Round-2 F2: a negative probe is evidence only if the fixture was live).
            if not verifiable:
                return _CS.UNKNOWN
            return _CS.ENFORCED if cond else _CS.NOT_ENFORCED

        def _capzero(v):
            try:
                return int(v, 16) == 0
            except Exception:
                return False

        netns = ev.get("netns", "")
        pidns = ev.get("pidns", "")
        c: dict = {}
        # Filesystem: fixed-path absence AND a per-run host-only canary the jail
        # could not read. The canary must have been ESTABLISHED on the host for its
        # absence-of-read to be non-vacuous (Round-2 F3).
        c["filesystem_isolation"] = unk(
            (not ev.get("home", True) and not ev.get("shadow", True)
             and not ev.get("repo", True) and not ev.get("canary_read", True)),
            verifiable=canary_established)
        # Network: only-loopback interfaces AND a net-ns inode distinct from the
        # broker's own (structural), verifiable only if we learned the host's ns.
        c["network_isolation"] = unk(
            (ev.get("ifaces") == ["lo"] and netns != "" and netns != host_netns),
            verifiable=bool(host_netns))
        # Host loopback: a failed connect to a PROVEN-LIVE host listener (Round-2 F2).
        c["host_loopback_isolation"] = unk(
            not ev.get("loopback_connect", True), verifiable=probe_live)
        # Descendant containment: a fresh pid-ns (structural inode) plus a bounded
        # /proc process count (behavioural).
        c["descendant_containment"] = unk(
            (0 < ev.get("proc_count", -1) <= 15 and pidns != "" and pidns != host_pidns),
            verifiable=bool(host_pidns))
        c["pid_limit"] = st(ev.get("nproc") == CE_NPROC)
        c["cpu_limit"] = st(ev.get("cpu") == CE_CPU_SECONDS)
        c["memory_limit"] = st(ev.get("as_") == CE_MEM_BYTES)
        c["storage_limit"] = st(0 < ev.get("tmpfs_bytes", -1) <= CE_WORKSPACE_BYTES)
        c["environment_isolation"] = st(not ev.get("env_extra", ["x"]))
        c["privilege_restriction"] = st(ev.get("uid") not in (0, None)
                                        and ev.get("euid") not in (0, None)
                                        and _capzero(ev.get("capeff", ""))
                                        and ev.get("nnp") == "1")
        c["workspace_ephemeral"] = st(ev.get("cwd") == "/work"
                                      and 0 < ev.get("tmpfs_bytes", -1) <= CE_WORKSPACE_BYTES)
        return c

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        import signal

        started = _now()
        receipt = ContainmentReceipt(
            requirement=request.requirement, backend=self.name, started_at=started)

        if not self.available():
            receipt.failure_reason = FailureReason.BACKEND_NOT_AVAILABLE.value
            receipt.cleanup_status = ControlStatus.ENFORCED  # nothing to clean
            receipt.finished_at = _now()
            return ExecutionOutcome(executed=False,
                                    error="bubblewrap / unprivileged userns unavailable",
                                    receipt=receipt)

        scriptdir = tempfile.mkdtemp(prefix="jarvis_sbx_")
        with open(os.path.join(scriptdir, "_bootstrap.py"), "w", encoding="utf-8") as fh:
            fh.write(_BOOTSTRAP_SOURCE)
        with open(os.path.join(scriptdir, "snippet.py"), "w", encoding="utf-8") as fh:
            fh.write(request.code)

        # Broker-side controls: the broker itself performs these, so it observes
        # that they applied. The namespace controls are derived from EVIDENCE below.
        c = receipt.controls
        for name in ("dedicated_cwd", "minimal_env", "process_tree_kill",
                     "stdout_cap", "stderr_cap", "output_limit", "wall_timeout"):
            c[name] = ControlStatus.ENFORCED
        # Until observed, every mandatory namespace control is UNKNOWN — never
        # asserted from the requested argv (§13/MAJOR A).
        for name in MANDATORY_SANDBOX_CONTROLS:
            c.setdefault(name, ControlStatus.UNKNOWN)

        nonce = secrets.token_hex(16)
        probe = _LoopbackProbe()          # a real host listener the jail must NOT reach
        probe_live = probe.verify_live()  # PROVE it accepts before trusting a miss
        # Per-run host-only canary OUTSIDE the jail (never bound in): a random file
        # with random content that MUST be unreadable from the sandbox (Round-2 F3).
        canary_path, canary_established = _make_host_canary()
        # The broker's OWN namespace identities, for a structural comparison.
        host_netns = _read_ns("net")
        host_pidns = _read_ns("pid")

        # Round-2 F2/F3: if the host-side proof fixtures could not be established,
        # host-loopback / filesystem / namespace isolation cannot be PROVEN for
        # this run. Do not execute — a negative probe is not evidence without a
        # live positive fixture. (Bind to 127.0.0.1:0 and a host tempfile ~never
        # fail, so this is a safety net, not a normal path.)
        if not (probe_live and canary_established and host_netns and host_pidns):
            probe.close()
            _rmtree_file(canary_path)
            _rmtree(scriptdir)
            for name in MANDATORY_SANDBOX_CONTROLS:
                c[name] = ControlStatus.UNKNOWN
            receipt.cleanup_status = ControlStatus.ENFORCED  # nothing ran
            receipt.failure_reason = FailureReason.CONTAINMENT_NOT_VERIFIED.value
            receipt.finished_at = _now()
            return ExecutionOutcome(
                executed=False,
                error="host proof fixtures (loopback probe / filesystem canary / "
                      "namespace identity) unavailable; containment cannot be proven",
                receipt=receipt)

        argv = self._bwrap_argv(scriptdir)
        stdin_payload = (f"{nonce}\n{probe.host}\n{probe.port}\n"
                         f"{canary_path}\n{host_netns}\n{host_pidns}\n")

        stdout = stderr = ""
        returncode = None
        err = None
        proc = None
        timed_out = False
        try:
            proc = subprocess.Popen(              # shell=False, argv built from constants
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, shell=False, start_new_session=True)
            try:
                stdout, stderr = proc.communicate(input=stdin_payload,
                                                  timeout=request.timeout)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_tree(proc, True, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    stdout, stderr = proc.communicate(timeout=5)
                err = f"Timeout tras {request.timeout}s de ejecución."
        except Exception as e:             # pragma: no cover - launch failure
            err = str(e)
        finally:
            if proc is not None and proc.poll() is None:
                _kill_process_tree(proc, True, signal.SIGKILL)
            probe.close()
            canary_gone = _rmtree_file(canary_path)
            cleaned = _rmtree(scriptdir) and canary_gone
            receipt.cleanup_status = (ControlStatus.ENFORCED if cleaned
                                      else ControlStatus.NOT_ENFORCED)

        # Bounded raw stderr, retained only as diagnostics (never the authority).
        receipt.measured_limitations.append(
            "raw stderr (diagnostic, non-authoritative): "
            + (stderr or "")[:200].replace(chr(10), " "))

        # Parse the ONE readiness record whose nonce matches ours. The snippet
        # cannot forge it (it never learns the nonce), so its absence means the
        # trusted bootstrap never reached readiness — setup failed → fail closed.
        ready_flag = None
        evidence: dict = {}
        snippet_out = stdout or ""
        for idx, line in enumerate((stdout or "").splitlines()):
            if line.startswith(_READY_MARKER + ":"):
                parts = line.split(":", 3)
                if len(parts) == 4 and parts[1] == nonce:
                    ready_flag = parts[2]
                    with contextlib.suppress(Exception):
                        evidence = json.loads(parts[3])
                    snippet_out = "\n".join((stdout or "").splitlines()[idx + 1:])
                    break

        if ready_flag is None:
            # No valid readiness record: the jail never reached the trusted
            # bootstrap (namespace/mount/resource setup failed, whatever the
            # stderr says). ZERO untrusted code executed.
            for name in MANDATORY_SANDBOX_CONTROLS:
                c[name] = ControlStatus.UNKNOWN
            receipt.failure_reason = (FailureReason.NAMESPACE_SETUP_FAILED.value
                                      if returncode not in (0, None)
                                      else FailureReason.BOOTSTRAP_NOT_READY.value)
            receipt.finished_at = _now()
            return ExecutionOutcome(executed=False,
                                    error=err or "containment not established",
                                    receipt=receipt)

        # We have observed evidence. Derive controls from it — the single source
        # of enforcement truth. Probe-liveness and host ns identity gate the
        # network/loopback/pid/filesystem controls so a vacuous fixture yields
        # UNKNOWN, never a false ENFORCED (Round-2 F2/F3/F4).
        c.update(self._derive_controls_from_evidence(
            evidence, probe_live=probe_live, host_netns=host_netns,
            host_pidns=host_pidns, canary_established=canary_established))

        if ready_flag != "1":
            # The trusted bootstrap's self-check failed: a mandatory control was
            # NOT established, so it exited WITHOUT running the snippet. Fail
            # closed — zero untrusted code executed — and report the truth.
            receipt.failure_reason = FailureReason.CONTAINMENT_NOT_VERIFIED.value
            receipt.finished_at = _now()
            return ExecutionOutcome(executed=False,
                                    error="containment not verified; snippet not run",
                                    receipt=receipt)

        # ready_flag == "1": the snippet ran under a jail whose mandatory controls
        # were observed established. Report its (bounded) output.
        stdout = (snippet_out or "")[:CE_STDOUT_CAP]
        stderr = (stderr or "")[:CE_STDERR_CAP]
        if timed_out:
            receipt.failure_reason = FailureReason.TIMEOUT.value
        receipt.finished_at = _now()
        return ExecutionOutcome(executed=True, stdout=stdout, stderr=stderr,
                                returncode=returncode, error=err, receipt=receipt)


# ── WindowsContainmentBackend (truthful, not live-verified here) ──────────────
class WindowsContainmentBackend(ContainmentBackend):
    """Windows containment audit (§25). Job Objects, restricted tokens, low
    integrity and AppContainer exist as OS primitives, but none is live-verified
    on this Linux development host, so this backend NEVER claims SANDBOXED. On
    Windows it degrades to a documented RESTRICTED_PROCESS via the shared
    RestrictedProcessBackend; a real Job-Object implementation is future work."""

    name = "windows_containment"

    def probe_capabilities(self) -> dict:
        return {
            "os": os.name,
            "is_windows": os.name == "nt",
            "job_objects": "audited_not_live_verified",
            "restricted_tokens": "audited_not_live_verified",
            "appcontainer": "audited_not_live_verified",
            "status": ("WINDOWS_RESTRICTED_PROCESS_ONLY" if os.name == "nt"
                       else "NOT_ON_THIS_HOST"),
        }

    def max_profile(self) -> ExecutionProfile:
        # No live-verified strong containment ⇒ never better than RESTRICTED.
        return (ExecutionProfile.RESTRICTED_PROCESS if os.name == "nt"
                else ExecutionProfile.DIRECT_PROCESS)

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        outcome = RestrictedProcessBackend().execute(request)
        if outcome.receipt is not None:
            outcome.receipt.backend = self.name
            outcome.receipt.measured_limitations.append(
                "Windows strong containment (Job Objects/AppContainer) is audited "
                "but NOT live-verified; this run is RESTRICTED_PROCESS, not SANDBOXED")
        return outcome


# ── Operator policy (host-controlled; NEVER model/user/tool controlled) ───────
class OperatorPolicy(str, Enum):
    STRICT = "strict"    # SANDBOX_REQUIRED unsatisfiable ⇒ fail closed (default)
    COMPAT = "compat"    # explicit, visible downgrade to RESTRICTED_PROCESS


_POLICY_ENV = "JARVIS_EXEC_CONTAINMENT"


def _operator_policy() -> OperatorPolicy:
    """Read the operator's containment policy from the HOST ENVIRONMENT ONLY.

    This is deliberately not read from ``tool_input``, settings that a tool can
    write, or any model-reachable channel — a downgrade must be a human operator's
    deployment decision (§7/§8). Default is STRICT: fail closed.
    """
    raw = (os.environ.get(_POLICY_ENV) or "").strip().lower()
    return OperatorPolicy.COMPAT if raw == "compat" else OperatorPolicy.STRICT


# ── The one canonical broker (§9) ─────────────────────────────────────────────
class ContainmentBroker:
    """Receives an ALREADY-AUTHORIZED operation and decides how to contain it.

    It never grants authority. Its whole job: pick the strongest backend that can
    satisfy the requirement, run there, and return a truthful receipt — or, if no
    backend can satisfy a mandatory requirement and the operator has not
    explicitly permitted a visible downgrade, refuse to execute (fail closed).
    """

    def __init__(self, backends: list[ContainmentBackend] | None = None) -> None:
        # Strongest first. On non-Linux the bubblewrap probe simply reports
        # unavailable and it is skipped.
        self._backends = backends if backends is not None else [
            BubblewrapBackend(),
            RestrictedProcessBackend(),
        ]

    def evaluate_requirement(self, tool_name: str, tool_input: dict
                             ) -> ContainmentRequirement:
        """The TRUSTED containment policy for a tool. Arbitrary code execution
        defaults to SANDBOX_REQUIRED (§7). ``tool_input`` is NOT consulted for any
        downgrade key — content is not authority (§8)."""
        if tool_name == "code_execute":
            return ContainmentRequirement.SANDBOX_REQUIRED
        return ContainmentRequirement.RESTRICTED_OK

    def select_backend(self, requirement: ContainmentRequirement
                       ) -> ContainmentBackend | None:
        for backend in self._backends:
            if backend.can_satisfy(requirement):
                return backend
        return None

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        backend = self.select_backend(request.requirement)
        if backend is not None:
            return backend.execute(request)

        # No backend can satisfy the requirement. Fail closed unless the operator
        # has explicitly permitted a compatibility downgrade — which is visible,
        # deterministic, auditable and NEVER reported SANDBOXED.
        if (request.requirement is ContainmentRequirement.SANDBOX_REQUIRED
                and _operator_policy() is OperatorPolicy.COMPAT):
            restricted = RestrictedProcessBackend()
            outcome = restricted.execute(
                ExecutionRequest(request.code, request.timeout,
                                 ContainmentRequirement.RESTRICTED_OK))
            if outcome.receipt is not None:
                outcome.receipt.requirement = request.requirement
                outcome.receipt.downgraded = True
                outcome.receipt.measured_limitations.append(
                    "OPERATOR COMPAT DOWNGRADE: SANDBOX_REQUIRED could not be "
                    "satisfied on this host; executed under RESTRICTED_PROCESS by "
                    "explicit operator policy (JARVIS_EXEC_CONTAINMENT=compat). "
                    "This is NOT a sandbox.")
            return outcome

        receipt = ContainmentReceipt(
            requirement=request.requirement, backend="none",
            started_at=_now(), finished_at=_now())
        receipt.failure_reason = (
            "CONTAINMENT_UNAVAILABLE: the required containment "
            f"({request.requirement.value}) could not be established on this host "
            "and no operator compatibility policy permits a downgrade; refusing to "
            "execute (fail closed).")
        receipt.cleanup_status = ControlStatus.ENFORCED  # nothing ran, nothing to clean
        return ExecutionOutcome(executed=False, error=receipt.failure_reason,
                                receipt=receipt)


# ── Process-tree teardown ─────────────────────────────────────────────────────
class _LoopbackProbe:
    """A real host loopback listener the sandbox must NOT be able to reach.

    The bubblewrap backend passes ``(host, port)`` to the in-jail bootstrap, which
    attempts a connection. In a fresh network namespace the connect fails (the
    jail's own ``lo`` has no route to this host socket); if the netns were shared
    the connect would SUCCEED and the readiness self-check would report it — which
    is exactly how host-loopback isolation is OBSERVED per run rather than
    asserted from the ``--unshare-net`` flag (MAJOR A). The listener accepts and
    immediately closes so a shared-netns connect genuinely succeeds (not merely
    ECONNREFUSED), keeping the probe non-vacuous."""

    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = 0
        self.live = False              # bound AND proven to accept (Round-2 F2)
        self._srv = None
        self._stop = threading.Event()
        with contextlib.suppress(Exception):
            self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._srv.bind((self.host, 0))
            self._srv.listen(8)
            self.port = self._srv.getsockname()[1]
            threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:                # pragma: no cover - timing dependent
        self._srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
                conn.close()
            except socket.timeout:
                continue
            except OSError:
                break

    def verify_live(self) -> bool:
        """Prove the listener actually accepts, from the HOST side. Only then may
        a sandbox connect FAILURE be read as host-loopback isolation rather than a
        dead fixture. Sets and returns ``self.live``."""
        if self.port == 0 or self._srv is None:
            self.live = False
            return False
        with contextlib.suppress(Exception):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.settimeout(2)
            probe.connect((self.host, self.port))
            probe.close()
            self.live = True
        return self.live

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(Exception):
            if self._srv is not None:
                self._srv.close()


def _kill_process_tree(proc, posix: bool, sig) -> None:
    """Terminate *proc* and every descendant. On POSIX the child leads its own
    session, so one ``killpg`` reaps the tree; for the sandbox the PID namespace
    reaps every member the moment init dies."""
    with contextlib.suppress(Exception):
        if posix:
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except ProcessLookupError:
                return
            except Exception:
                proc.kill()
        else:
            proc.kill()


def _rmtree(path: str) -> bool:
    """Remove *path*, returning True iff it is gone afterwards. Cleanup failure is
    surfaced in the receipt, never swallowed into a false success (§30)."""
    with contextlib.suppress(Exception):
        shutil.rmtree(path, ignore_errors=True)
    return not os.path.exists(path)


def _rmtree_file(path: str) -> bool:
    """Remove a single file, returning True iff it is gone (or was never made)."""
    if not path:
        return True
    with contextlib.suppress(Exception):
        os.unlink(path)
    return not os.path.exists(path)


def _make_host_canary() -> tuple[str, bool]:
    """Create a per-run HOST-ONLY canary file with random content OUTSIDE any path
    bound into the jail. Returns ``(path, established)``. Its readability from the
    sandbox is a POSITIVE, non-vacuous test of filesystem isolation (Round-2 F3):
    the broker proves the file EXISTS on the host, so a failed read inside the jail
    is real evidence rather than the absence of a path that never existed."""
    with contextlib.suppress(Exception):
        fd, path = tempfile.mkstemp(prefix="jarvis_host_canary_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("JARVIS_HOST_CANARY " + secrets.token_hex(16))
        # Sanity: the broker (host) can read it; if not, treat as not established.
        with open(path, encoding="utf-8") as fh:
            if fh.read():
                return path, True
    return "", False


def _read_ns(kind: str) -> str:
    """The broker's own namespace identity (e.g. ``net``/``pid``) as the kernel
    inode string, for a structural comparison against the jail's (Round-2 F4)."""
    with contextlib.suppress(Exception):
        return os.readlink(f"/proc/self/ns/{kind}")
    return ""
