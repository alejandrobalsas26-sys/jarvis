"""tests/test_containment_m66b.py — V69 M66B: the containment CORE LOGIC.

Pure logic, runs in every environment (no bwrap required). Proves:
  * the profile is DERIVED, never asserted, and cannot overclaim (§13),
  * SANDBOXED requires EVERY mandatory control (§11) — flipping any one drops it,
  * a weaker backend never satisfies a stronger requirement (§10),
  * the broker fails closed with ZERO effect on setup failure (§28),
  * content is not authority — tool_input downgrade keys are inert (§8),
  * the operator compat downgrade is host-controlled, visible, never SANDBOXED (§7),
  * the receipt carries the §12 fields and no secrets.
"""
from __future__ import annotations

import pytest

from core.containment import (
    BubblewrapBackend,
    ContainmentBackend,
    ContainmentBroker,
    ContainmentReceipt,
    ContainmentRequirement,
    ExecutionOutcome,
    ExecutionRequest,
    MANDATORY_SANDBOX_CONTROLS,
    OperatorPolicy,
    RestrictedProcessBackend,
    WindowsContainmentBackend,
    derive_profile,
    _operator_policy,
)
from core.execution_profile import (
    BASELINE_RESTRICTED_CONTROLS,
    ControlStatus,
    ExecutionProfile,
)


def _all_sandbox_controls() -> dict:
    return {c: ControlStatus.ENFORCED for c in MANDATORY_SANDBOX_CONTROLS}


# ── Profile derivation (§13) ──────────────────────────────────────────────────
class TestProfileDerivation:
    def test_full_control_set_derives_sandboxed(self):
        assert derive_profile(_all_sandbox_controls(),
                              cleanup_status=ControlStatus.ENFORCED) \
            is ExecutionProfile.SANDBOXED

    @pytest.mark.parametrize("missing", MANDATORY_SANDBOX_CONTROLS)
    def test_missing_any_mandatory_control_is_not_sandboxed(self, missing):
        controls = _all_sandbox_controls()
        controls[missing] = ControlStatus.NOT_ENFORCED
        prof = derive_profile(controls, cleanup_status=ControlStatus.ENFORCED)
        assert prof is not ExecutionProfile.SANDBOXED, (
            f"dropping {missing} still derived SANDBOXED — overclaim")

    def test_baseline_only_is_restricted(self):
        controls = {c: ControlStatus.ENFORCED for c in BASELINE_RESTRICTED_CONTROLS}
        assert derive_profile(controls) is ExecutionProfile.RESTRICTED_PROCESS

    def test_empty_controls_is_direct(self):
        assert derive_profile({}) is ExecutionProfile.DIRECT_PROCESS

    def test_cleanup_failure_blocks_sandboxed(self):
        prof = derive_profile(_all_sandbox_controls(),
                              cleanup_status=ControlStatus.NOT_ENFORCED)
        assert prof is not ExecutionProfile.SANDBOXED

    def test_cleanup_none_default_is_not_sandboxed(self):
        # Round-2 F5: the default (unobserved) cleanup status must fail closed —
        # only an explicitly ENFORCED cleanup earns SANDBOXED.
        assert derive_profile(_all_sandbox_controls()) is not ExecutionProfile.SANDBOXED

    def test_receipt_derives_profile_from_controls_not_label(self):
        # A receipt whose backend "claims" sandbox but has a missing control must
        # never render SANDBOXED — the classifier reads controls, not intent.
        r = ContainmentReceipt(
            requirement=ContainmentRequirement.SANDBOX_REQUIRED, backend="liar")
        for c in MANDATORY_SANDBOX_CONTROLS:
            r.controls[c] = ControlStatus.ENFORCED
        r.controls["network_isolation"] = ControlStatus.NOT_ENFORCED  # the lie
        r.cleanup_status = ControlStatus.ENFORCED
        assert r.to_dict()["profile"] != "sandboxed"


# ── Backend contract (§10) ────────────────────────────────────────────────────
class TestBackendContract:
    def test_restricted_cannot_satisfy_sandbox_required(self):
        # SANDBOX_REQUIRED + RESTRICTED_PROCESS = DENY (§10).
        b = RestrictedProcessBackend()
        assert b.can_satisfy(ContainmentRequirement.RESTRICTED_OK)
        assert not b.can_satisfy(ContainmentRequirement.SANDBOX_REQUIRED)
        assert not b.can_satisfy(ContainmentRequirement.NETWORK_DENY_REQUIRED)

    def test_windows_backend_never_satisfies_sandbox_off_windows(self):
        b = WindowsContainmentBackend()
        assert not b.can_satisfy(ContainmentRequirement.SANDBOX_REQUIRED)

    def test_bubblewrap_satisfies_sandbox_iff_available(self):
        b = BubblewrapBackend()
        available = b.available()
        assert b.can_satisfy(ContainmentRequirement.SANDBOX_REQUIRED) is available


class _FakeBackend(ContainmentBackend):
    def __init__(self, name, profile, executed=True):
        self.name = name
        self._profile = profile
        self._executed = executed
        self.calls = 0

    def probe_capabilities(self):
        return {}

    def max_profile(self):
        return self._profile

    def execute(self, request):
        self.calls += 1
        r = ContainmentReceipt(requirement=request.requirement, backend=self.name)
        return ExecutionOutcome(executed=self._executed, stdout="ran",
                                returncode=0, receipt=r)


# ── Broker selection + fail-closed (§9/§28) ───────────────────────────────────
class TestBrokerSelection:
    def test_selects_strongest_satisfying_backend(self):
        weak = _FakeBackend("weak", ExecutionProfile.RESTRICTED_PROCESS)
        strong = _FakeBackend("strong", ExecutionProfile.SANDBOXED)
        broker = ContainmentBroker([strong, weak])
        sel = broker.select_backend(ContainmentRequirement.SANDBOX_REQUIRED)
        assert sel is strong

    def test_weaker_backend_never_selected_for_sandbox(self):
        weak = _FakeBackend("weak", ExecutionProfile.RESTRICTED_PROCESS)
        broker = ContainmentBroker([weak])
        assert broker.select_backend(ContainmentRequirement.SANDBOX_REQUIRED) is None

    def test_fail_closed_when_no_backend_and_strict(self, monkeypatch):
        monkeypatch.delenv("JARVIS_EXEC_CONTAINMENT", raising=False)
        # The only backend cannot satisfy SANDBOX_REQUIRED; its execute() is a
        # side-effect canary that must NEVER be reached (§28 zero-effect).
        weak = _FakeBackend("weak", ExecutionProfile.RESTRICTED_PROCESS)
        broker = ContainmentBroker([weak])
        out = broker.execute(ExecutionRequest("print(1)", 5,
                                              ContainmentRequirement.SANDBOX_REQUIRED))
        assert out.executed is False
        assert "CONTAINMENT_UNAVAILABLE" in (out.error or "")
        assert weak.calls == 0  # zero effect — the weak backend never ran (§28)
        assert out.receipt.to_dict()["profile"] != "sandboxed"

    def test_compat_downgrade_only_via_operator_env(self, monkeypatch):
        # Without the env: fail closed. With compat: visible RESTRICTED downgrade.
        weak_only = ContainmentBroker([RestrictedProcessBackend()])
        monkeypatch.delenv("JARVIS_EXEC_CONTAINMENT", raising=False)
        out = weak_only.execute(ExecutionRequest("print(1)", 5,
                                                 ContainmentRequirement.SANDBOX_REQUIRED))
        assert out.executed is False

        monkeypatch.setenv("JARVIS_EXEC_CONTAINMENT", "compat")
        out2 = weak_only.execute(ExecutionRequest("print(1)", 5,
                                                  ContainmentRequirement.SANDBOX_REQUIRED))
        assert out2.executed is True
        d = out2.receipt.to_dict()
        assert d["downgraded"] is True
        assert d["profile"] != "sandboxed"          # NEVER sandboxed on a downgrade
        assert d["profile"] == "restricted_process"
        assert any("OPERATOR COMPAT DOWNGRADE" in m for m in d["measured_limitations"])


# ── Content is not authority (§8) ─────────────────────────────────────────────
class TestNoUntrustedDowngrade:
    @pytest.mark.parametrize("hostile", [
        {"code": "x", "sandbox": False},
        {"code": "x", "privileged": True},
        {"code": "x", "host_network": True},
        {"code": "x", "allow_host_fs": True},
        {"code": "x", "disable_network_isolation": True},
        {"code": "x", "use_direct_process": True},
        {"code": "x", "bypass_containment": True},
    ])
    def test_tool_input_cannot_change_requirement(self, hostile):
        broker = ContainmentBroker()
        req = broker.evaluate_requirement("code_execute", hostile)
        assert req is ContainmentRequirement.SANDBOX_REQUIRED

    def test_operator_policy_reads_only_env(self, monkeypatch):
        monkeypatch.delenv("JARVIS_EXEC_CONTAINMENT", raising=False)
        assert _operator_policy() is OperatorPolicy.STRICT
        monkeypatch.setenv("JARVIS_EXEC_CONTAINMENT", "compat")
        assert _operator_policy() is OperatorPolicy.COMPAT
        monkeypatch.setenv("JARVIS_EXEC_CONTAINMENT", "SANDBOX=false")  # junk
        assert _operator_policy() is OperatorPolicy.STRICT


# ── Receipt shape and secret-freeness (§12) ───────────────────────────────────
class TestReceipt:
    def test_receipt_has_the_mandatory_fields(self):
        r = ContainmentReceipt(
            requirement=ContainmentRequirement.SANDBOX_REQUIRED, backend="bubblewrap")
        d = r.to_dict()
        for key in ("requirement", "backend", "platform", "profile",
                    "network_isolation", "controls", "cleanup_status",
                    "measured_limitations", "downgraded"):
            assert key in d, f"receipt missing §12 field {key}"

    def test_receipt_carries_no_secret_env(self, monkeypatch):
        monkeypatch.setenv("JARVIS_M66B_SECRET_CANARY", "LEAK-9931")
        r = ContainmentReceipt(
            requirement=ContainmentRequirement.SANDBOX_REQUIRED, backend="x")
        import json
        assert "LEAK-9931" not in json.dumps(r.to_dict())

    def test_requirement_ranking_is_ordered(self):
        # Strength order used by can_satisfy.
        from core.containment import _REQUIREMENT_RANK
        assert (_REQUIREMENT_RANK[ContainmentRequirement.RESTRICTED_OK]
                < _REQUIREMENT_RANK[ContainmentRequirement.NETWORK_DENY_REQUIRED]
                < _REQUIREMENT_RANK[ContainmentRequirement.SANDBOX_REQUIRED])


# ── Extra targeted properties (mutation detectors) ────────────────────────────
class TestDerivationExtra:
    def test_partial_baseline_is_direct(self):
        # A strict subset of the baseline controls must NOT be RESTRICTED — it is
        # DIRECT. (Catches an `all`→`any` weakening of the baseline check.)
        one = {BASELINE_RESTRICTED_CONTROLS[0]: ControlStatus.ENFORCED}
        assert derive_profile(one) is ExecutionProfile.DIRECT_PROCESS

    def test_network_isolation_is_mandatory(self):
        assert "network_isolation" in MANDATORY_SANDBOX_CONTROLS
        # A receipt with every mandatory control EXCEPT network must not be sandboxed.
        r = ContainmentReceipt(
            requirement=ContainmentRequirement.SANDBOX_REQUIRED, backend="x")
        for c in MANDATORY_SANDBOX_CONTROLS:
            r.controls[c] = ControlStatus.ENFORCED
        r.controls["network_isolation"] = ControlStatus.NOT_ENFORCED
        r.cleanup_status = ControlStatus.ENFORCED
        assert r.to_dict()["profile"] != "sandboxed"

    def test_promoted_named_field_reflects_reality(self):
        # to_dict promotes each mandatory control to a named field; a NOT_ENFORCED
        # control must render as such, never hard-coded enforced.
        r = ContainmentReceipt(
            requirement=ContainmentRequirement.SANDBOX_REQUIRED, backend="x")
        r.controls["filesystem_isolation"] = ControlStatus.NOT_ENFORCED
        assert r.to_dict()["filesystem_isolation"] == "not_enforced"


class TestRestrictedBackendProperties:
    def test_restricted_network_is_not_enforced(self):
        out = RestrictedProcessBackend().execute(
            ExecutionRequest("print(1)", 10, ContainmentRequirement.RESTRICTED_OK))
        assert out.receipt.to_dict()["network_isolation"] == "not_enforced"

    @pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX rlimits")
    def test_restricted_cpu_limit_kills_busy_loop(self):
        import time
        start = time.monotonic()
        out = RestrictedProcessBackend().execute(
            ExecutionRequest("\nwhile True:\n    pass\n", 25,
                             ContainmentRequirement.RESTRICTED_OK))
        elapsed = time.monotonic() - start
        assert out.returncode not in (0, None) or out.error
        assert elapsed < 15, f"restricted CPU limit did not bite ({elapsed:.1f}s)"

    def test_restricted_env_allowlist_withholds_secret(self, monkeypatch):
        monkeypatch.setenv("JARVIS_M66B_SECRET_CANARY", "LEAK-RS")
        out = RestrictedProcessBackend().execute(
            ExecutionRequest(
                "import os;print(os.environ.get('JARVIS_M66B_SECRET_CANARY'))", 10,
                ContainmentRequirement.RESTRICTED_OK))
        assert "LEAK-RS" not in (out.stdout or "")


class TestExecutorRouting:
    def test_code_execute_is_sandboxed_when_available(self):
        from tools.executor import ToolExecutor
        from core.containment import BubblewrapBackend
        if not BubblewrapBackend().available():
            pytest.skip("no sandbox on this host")
        r = ToolExecutor().execute("code_execute", {"code": "print('ok')"})
        assert r["containment"]["profile"] == "sandboxed"
        assert r["containment"]["backend"] == "bubblewrap"

    def test_code_execute_fails_closed_when_broker_cannot(self, monkeypatch):
        from tools.executor import ToolExecutor
        import core.containment as containment

        def _fail(self, request):
            r = containment.ContainmentReceipt(
                requirement=request.requirement, backend="none")
            r.failure_reason = "CONTAINMENT_UNAVAILABLE: forced"
            return containment.ExecutionOutcome(
                executed=False, error=r.failure_reason, receipt=r)

        monkeypatch.setattr(containment.ContainmentBroker, "execute", _fail)
        r = ToolExecutor().execute("code_execute", {"code": "print('should not run')"})
        assert r.get("error_class") == "containment_unavailable"
        assert "stdout" not in r
