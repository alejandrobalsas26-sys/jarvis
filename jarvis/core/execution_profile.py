"""core/execution_profile.py — V69 M66A.1 (L3): truth-bearing execution profiles.

WHY THIS EXISTS
---------------
"isolated", "sandboxed" and "contained" were words in a docstring, not properties
of the process. `code_execute` ran `subprocess.run([sys.executable, tmp])` with the
parent's full environment, the parent's cwd, no resource limits, an open network
and no process-tree cleanup — and called itself an "isolated subprocess". §20
requires that the classification be ENFORCEABLE, never inferred from intent or a
function name.

THE PROFILES (least → most contained)
-------------------------------------
* DIRECT_PROCESS   — normal OS user/process authority. No meaningful limits.
* RESTRICTED_PROCESS — meaningful, enforceable limits actually exist (a dedicated
  cwd, a minimal environment, a hard wall-clock timeout, output caps, process-tree
  termination, and — where the OS supports it — CPU/memory/fsize/nproc rlimits).
* SANDBOXED        — strong resource isolation actually exists (namespaces / a
  real sandbox). JARVIS does not currently provide this; the value exists so a
  future implementation has a name, and so a test can assert nothing claims it
  today without proof.

CONTROL STATUS
--------------
Each individual control reports one of:
* ENFORCED       — the control is in place and was applied to this execution.
* NOT_SUPPORTED  — this OS/interpreter cannot provide it (e.g. POSIX rlimits on
  Windows). An honest absence, not a failure.
* NOT_AVAILABLE  — supported in principle but a dependency/permission is missing.
* NOT_ENFORCED   — deliberately not enforced (e.g. network isolation, which JARVIS
  does not attempt without privileged host changes — §22).

`subprocess != sandbox`: a profile may be named RESTRICTED_PROCESS only when at
least the portable baseline controls (cwd, env, timeout, output caps, tree kill)
are ENFORCED. `network_isolation = NOT_ENFORCED` is truthful and is preferred over
a false SANDBOXED (§22).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ExecutionProfile(str, Enum):
    DIRECT_PROCESS = "direct_process"
    RESTRICTED_PROCESS = "restricted_process"
    SANDBOXED = "sandboxed"


class ControlStatus(str, Enum):
    ENFORCED = "enforced"
    NOT_SUPPORTED = "not_supported"
    NOT_AVAILABLE = "not_available"
    NOT_ENFORCED = "not_enforced"


#: The controls a RESTRICTED_PROCESS must have ENFORCED to earn the name. Network
#: isolation is deliberately NOT in this set — see §22: a truthful
#: RESTRICTED_PROCESS with network NOT_ENFORCED beats a fake SANDBOXED.
BASELINE_RESTRICTED_CONTROLS: tuple[str, ...] = (
    "dedicated_cwd",
    "minimal_env",
    "wall_timeout",
    "stdout_cap",
    "stderr_cap",
    "process_tree_kill",
)


@dataclass
class ContainmentReport:
    """What was ACTUALLY enforced for one execution. Truth, not aspiration."""
    profile: ExecutionProfile
    controls: dict[str, ControlStatus] = field(default_factory=dict)
    network_isolation: ControlStatus = ControlStatus.NOT_ENFORCED
    measured_limitations: list[str] = field(default_factory=list)

    def enforced(self, name: str) -> bool:
        return self.controls.get(name) is ControlStatus.ENFORCED

    def classify(self) -> ExecutionProfile:
        """Derive the profile from the controls actually enforced.

        This is the ONLY sanctioned way to name a profile: it reads the control
        map rather than trusting the caller's `profile` field. A profile that
        claims more than the controls prove is downgraded to the truth.
        """
        baseline_all = all(self.enforced(c) for c in BASELINE_RESTRICTED_CONTROLS)
        if self.profile is ExecutionProfile.SANDBOXED:
            # SANDBOXED requires strong isolation INCLUDING network. JARVIS does
            # not provide that, so a SANDBOXED claim with network NOT_ENFORCED is
            # downgraded rather than believed.
            if self.network_isolation is ControlStatus.ENFORCED and baseline_all:
                return ExecutionProfile.SANDBOXED
            return (ExecutionProfile.RESTRICTED_PROCESS if baseline_all
                    else ExecutionProfile.DIRECT_PROCESS)
        if baseline_all:
            return ExecutionProfile.RESTRICTED_PROCESS
        return ExecutionProfile.DIRECT_PROCESS

    def to_dict(self) -> dict:
        return {
            "profile": self.classify().value,
            "claimed_profile": self.profile.value,
            "controls": {k: v.value for k, v in self.controls.items()},
            "network_isolation": self.network_isolation.value,
            "measured_limitations": list(self.measured_limitations),
        }
