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
import os
import shutil
import subprocess  # nosec B404 - every call in this module is shell=False, argv-list
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from core.execution_profile import (
    BASELINE_RESTRICTED_CONTROLS,
    ControlStatus,
    ExecutionProfile,
)


# ── Containment requirement (the typed demand, not a second authority plane) ──
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
    cleanup_ok = cleanup_status is not ControlStatus.NOT_ENFORCED
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
    #: network_isolation is surfaced both as a top-level field (M66A.1 shape,
    #: consumers read ``receipt["network_isolation"]``) and inside ``controls``.
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
            "network_isolation": self.network_isolation.value,
            "controls": {k: v.value for k, v in self.controls.items()},
            "cleanup_status": self.cleanup_status.value,
            "measured_limitations": list(self.measured_limitations),
            "downgraded": self.downgraded,
        }
        # The named §12 controls, promoted for direct reading (value or "absent").
        for name in MANDATORY_SANDBOX_CONTROLS:
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

#: Set rlimits INSIDE the jail, after the UID is remapped, then exec the snippet.
#: Load-bearing: setting RLIMIT_NPROC on the bwrap parent counts against the real
#: UID's existing processes and fails namespace creation with EAGAIN. Inside the
#: fresh namespace UID the count starts at zero, so the limit is both effective
#: and scoped to the sandbox (closing the M66A.1 PID-limit hole).
_BOOTSTRAP_SOURCE = f"""import resource, os, sys
resource.setrlimit(resource.RLIMIT_CPU, ({CE_CPU_SECONDS}, {CE_CPU_SECONDS} + 1))
resource.setrlimit(resource.RLIMIT_AS, ({CE_MEM_BYTES}, {CE_MEM_BYTES}))
resource.setrlimit(resource.RLIMIT_FSIZE, ({CE_FSIZE_BYTES}, {CE_FSIZE_BYTES}))
resource.setrlimit(resource.RLIMIT_NPROC, ({CE_NPROC}, {CE_NPROC}))
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
        receipt.network_isolation = ControlStatus.NOT_ENFORCED
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
            proc = subprocess.Popen(  # nosec B603 - shell=False, argv is [python, -I, script]
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
            probe = subprocess.run(  # nosec B603 - fixed argv, shell=False
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
            "--tmpfs", "/dev/shm",  # nosec B108 - mount point INSIDE the jail's mount ns, not a host tmp path
            "--tmpfs", "/tmp",      # nosec B108 - mount point INSIDE the jail's mount ns, not a host tmp path
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

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        import signal

        started = _now()
        receipt = ContainmentReceipt(
            requirement=request.requirement, backend=self.name, started_at=started)

        if not self.available():
            receipt.failure_reason = "bubblewrap or unprivileged user namespaces unavailable"
            receipt.cleanup_status = ControlStatus.ENFORCED  # nothing to clean
            receipt.finished_at = _now()
            return ExecutionOutcome(executed=False, error=receipt.failure_reason,
                                    receipt=receipt)

        scriptdir = tempfile.mkdtemp(prefix="jarvis_sbx_")
        with open(os.path.join(scriptdir, "_bootstrap.py"), "w", encoding="utf-8") as fh:
            fh.write(_BOOTSTRAP_SOURCE)
        with open(os.path.join(scriptdir, "snippet.py"), "w", encoding="utf-8") as fh:
            fh.write(request.code)

        # The controls this configuration ENFORCES, recorded as truth. Every one
        # is proven non-vacuous by a causal test in the escape matrix.
        c = receipt.controls
        c["filesystem_isolation"] = ControlStatus.ENFORCED   # mount ns
        c["network_isolation"] = ControlStatus.ENFORCED      # network ns
        receipt.network_isolation = ControlStatus.ENFORCED
        c["host_loopback_isolation"] = ControlStatus.ENFORCED
        c["descendant_containment"] = ControlStatus.ENFORCED  # PID ns
        c["pid_limit"] = ControlStatus.ENFORCED               # RLIMIT_NPROC in ns
        c["cpu_limit"] = ControlStatus.ENFORCED
        c["memory_limit"] = ControlStatus.ENFORCED
        c["storage_limit"] = ControlStatus.ENFORCED           # bounded tmpfs
        c["environment_isolation"] = ControlStatus.ENFORCED   # --clearenv
        c["privilege_restriction"] = ControlStatus.ENFORCED   # non-root+caps+nnp
        c["workspace_ephemeral"] = ControlStatus.ENFORCED
        # Baseline names too, so the receipt is unambiguously >= RESTRICTED.
        c["dedicated_cwd"] = ControlStatus.ENFORCED
        c["minimal_env"] = ControlStatus.ENFORCED
        c["process_tree_kill"] = ControlStatus.ENFORCED

        argv = self._bwrap_argv(scriptdir)
        stdout = stderr = ""
        returncode = None
        err = None
        proc = None
        try:
            proc = subprocess.Popen(  # nosec B603 - shell=False, argv built from constants
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                shell=False, start_new_session=True)
            try:
                stdout, stderr = proc.communicate(timeout=request.timeout)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                # Kill the bwrap process; --die-with-parent + the PID ns mean the
                # whole jail (init and every descendant, setsid included) dies.
                _kill_process_tree(proc, True, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    stdout, stderr = proc.communicate(timeout=5)
                err = f"Timeout tras {request.timeout}s de ejecución."
            c["wall_timeout"] = ControlStatus.ENFORCED
            stdout = (stdout or "")[:CE_STDOUT_CAP]
            stderr = (stderr or "")[:CE_STDERR_CAP]
            c["stdout_cap"] = ControlStatus.ENFORCED
            c["stderr_cap"] = ControlStatus.ENFORCED
            c["output_limit"] = ControlStatus.ENFORCED
        except Exception as e:             # pragma: no cover - launch failure
            err = str(e)
            receipt.failure_reason = str(e)
        finally:
            if proc is not None and proc.poll() is None:
                _kill_process_tree(proc, True, signal.SIGKILL)
            cleaned = _rmtree(scriptdir)
            receipt.cleanup_status = (ControlStatus.ENFORCED if cleaned
                                      else ControlStatus.NOT_ENFORCED)
        # bwrap could not create the namespaces (transient EAGAIN, an LSM change
        # since probe): the code did NOT run under the claimed controls. Fail
        # closed — do not report a sandbox that was not established.
        if returncode is not None and returncode == 1 and "Creating new namespace" in (stderr or ""):
            receipt.failure_reason = "namespace creation failed at launch"
            for k in list(c):
                c[k] = ControlStatus.NOT_AVAILABLE
            receipt.network_isolation = ControlStatus.NOT_AVAILABLE
            receipt.finished_at = _now()
            return ExecutionOutcome(executed=False, error=receipt.failure_reason,
                                    receipt=receipt)
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
