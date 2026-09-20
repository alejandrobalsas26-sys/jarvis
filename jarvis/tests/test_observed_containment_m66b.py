"""tests/test_observed_containment_m66b.py — V69 M66B Round-1 (§13–§18).

Round 1 found two MAJORs:
  A. the receipt ASSERTED controls ENFORCED from the requested argv rather than
     OBSERVING them per run (a silently-ineffective --unshare-net still read
     "sandboxed" with a live host connection);
  B. fail-closed keyed on one stderr substring, so a non-namespace setup failure
     certified a SANDBOXED run in which zero code executed.

Both are fixed by a READY handshake: the trusted in-jail bootstrap OBSERVES every
mandatory control, emits ``JARVIS_READY:<nonce>:<0|1>:<evidence>`` on stdout, and
runs the untrusted snippet ONLY if all controls were observed established. The
broker derives the profile from that evidence; the decision keys on the typed
FailureReason, never on stderr wording.
"""
from __future__ import annotations

import pytest

import core.containment as containment
from core.containment import (
    BubblewrapBackend, ContainmentRequirement, ExecutionRequest, FailureReason,
    CE_NPROC, CE_CPU_SECONDS, CE_MEM_BYTES, CE_WORKSPACE_BYTES,
)
from core.execution_profile import ControlStatus, ExecutionProfile
from _m66b_sandbox_helpers import requires_sandbox  # noqa: E402


_HOST_NETNS = "net:[4026500000]"
_HOST_PIDNS = "pid:[4026500001]"


def _good_evidence() -> dict:
    return dict(uid=65534, euid=65534, gid=65534, capeff="0000000000000000",
                nnp="1", home=False, shadow=False, repo=False, canary_read=False,
                env_extra=[], ifaces=["lo"], loopback_connect=False,
                netns="net:[4026533333]", pidns="pid:[4026534444]",
                nproc=CE_NPROC, cpu=CE_CPU_SECONDS, as_=CE_MEM_BYTES,
                fsize=16 * 1024 * 1024, proc_count=2, cwd="/work",
                tmpfs_bytes=CE_WORKSPACE_BYTES)


def _derive(ev, *, probe_live=True, canary_established=True,
            host_netns=_HOST_NETNS, host_pidns=_HOST_PIDNS):
    return BubblewrapBackend()._derive_controls_from_evidence(
        ev, probe_live=probe_live, host_netns=host_netns, host_pidns=host_pidns,
        canary_established=canary_established)


# ── Logic: controls are DERIVED from evidence, per mandatory control (runs anywhere)
class TestEvidenceDerivation:
    def test_good_evidence_all_enforced(self):
        c = _derive(_good_evidence())
        for name in ("filesystem_isolation", "network_isolation",
                     "host_loopback_isolation", "descendant_containment",
                     "pid_limit", "cpu_limit", "memory_limit", "storage_limit",
                     "environment_isolation", "privilege_restriction",
                     "workspace_ephemeral"):
            assert c[name] is ControlStatus.ENFORCED, name

    @pytest.mark.parametrize("mutate,control", [
        (dict(ifaces=["lo", "eth0"]), "network_isolation"),
        (dict(loopback_connect=True), "host_loopback_isolation"),
        (dict(home=True), "filesystem_isolation"),
        (dict(repo=True), "filesystem_isolation"),
        (dict(canary_read=True), "filesystem_isolation"),
        (dict(uid=0), "privilege_restriction"),
        (dict(capeff="00000000a80425fb"), "privilege_restriction"),
        (dict(nnp="0"), "privilege_restriction"),
        (dict(env_extra=["OPENAI_API_KEY"]), "environment_isolation"),
        (dict(nproc=999999), "pid_limit"),
        (dict(cpu=999999), "cpu_limit"),
        (dict(as_=1 << 60), "memory_limit"),
        (dict(tmpfs_bytes=1 << 40), "storage_limit"),
        (dict(proc_count=400), "descendant_containment"),
        (dict(cwd="/"), "workspace_ephemeral"),
    ])
    def test_bad_evidence_drops_the_control(self, mutate, control):
        ev = _good_evidence(); ev.update(mutate)
        c = _derive(ev)
        assert c[control] is not ControlStatus.ENFORCED, (control, mutate)

    # ── Round-2 F2/F3/F4: a vacuous fixture yields UNKNOWN, never false-ENFORCED ─
    def test_dead_loopback_probe_yields_unknown(self):
        # probe not proven live → host_loopback_isolation cannot be ENFORCED even
        # though loopback_connect is False.
        c = _derive(_good_evidence(), probe_live=False)
        assert c["host_loopback_isolation"] is ControlStatus.UNKNOWN

    def test_unestablished_canary_yields_unknown_filesystem(self):
        c = _derive(_good_evidence(), canary_established=False)
        assert c["filesystem_isolation"] is ControlStatus.UNKNOWN

    def test_shared_netns_inode_drops_network(self):
        ev = _good_evidence(); ev["netns"] = _HOST_NETNS   # same inode as host
        assert _derive(ev)["network_isolation"] is not ControlStatus.ENFORCED

    def test_shared_pidns_inode_drops_descendant(self):
        ev = _good_evidence(); ev["pidns"] = _HOST_PIDNS
        assert _derive(ev)["descendant_containment"] is not ControlStatus.ENFORCED

    def test_missing_host_ns_makes_observation_unknown(self):
        # If the broker could not read its own ns identity, the comparison is
        # not discriminating → UNKNOWN, not ENFORCED.
        c = _derive(_good_evidence(), host_netns="", host_pidns="")
        assert c["network_isolation"] is ControlStatus.UNKNOWN
        assert c["descendant_containment"] is ControlStatus.UNKNOWN

    def test_unknown_control_never_yields_sandboxed(self):
        from core.containment import derive_profile, MANDATORY_SANDBOX_CONTROLS
        controls = {n: ControlStatus.ENFORCED for n in MANDATORY_SANDBOX_CONTROLS}
        controls["network_isolation"] = ControlStatus.UNKNOWN
        assert derive_profile(controls, cleanup_status=ControlStatus.ENFORCED) \
            is not ExecutionProfile.SANDBOXED

    def test_verify_live_is_false_for_unbound_probe(self):
        # Round-2 F2: verify_live() must report False when the listener never bound
        # (so a sandbox connect miss cannot be read as isolation). Uses a REAL
        # unbound probe (no monkeypatch of verify_live), so a mutation making
        # verify_live always-True is caught here.
        import threading
        from core.containment import _LoopbackProbe

        class _Dead(_LoopbackProbe):
            def __init__(self):
                self.host = "127.0.0.1"; self.port = 0; self.live = False
                self._srv = None; self._stop = threading.Event()

        d = _Dead()
        assert d.verify_live() is False
        assert d.live is False

    def test_failure_reason_enum_is_structural(self):
        # Typed reasons exist and are not stderr text (§15).
        for r in ("BACKEND_NOT_AVAILABLE", "NAMESPACE_SETUP_FAILED",
                  "BOOTSTRAP_NOT_READY", "CONTAINMENT_NOT_VERIFIED", "TIMEOUT"):
            assert hasattr(FailureReason, r)


# ── Live: the handshake observes reality (requires bubblewrap) ────────────────
@requires_sandbox
class TestLiveObservation:
    def test_normal_run_is_sandboxed_by_observation(self):
        out = BubblewrapBackend().execute(ExecutionRequest(
            "print('OK42')", 15, ContainmentRequirement.SANDBOX_REQUIRED))
        assert out.executed
        assert "OK42" in out.stdout
        assert out.receipt.to_dict()["profile"] == "sandboxed"

    def test_major_a_net_share_not_sandboxed_and_zero_effect(self):
        # A silently-ineffective --unshare-net: the readiness self-check observes
        # a reachable host loopback → snippet NEVER runs, receipt is not sandboxed.
        class NetShare(BubblewrapBackend):
            def _bwrap_argv(self, scriptdir):
                a = super()._bwrap_argv(scriptdir)
                return ["bwrap", "--unshare-user", "--unshare-pid", "--unshare-ipc",
                        "--unshare-uts", "--unshare-cgroup"] + a[2:]
        out = NetShare().execute(ExecutionRequest(
            "print('SNIPPET_RAN_A')", 15, ContainmentRequirement.SANDBOX_REQUIRED))
        assert out.executed is False
        assert "SNIPPET_RAN_A" not in (out.stdout or "")
        d = out.receipt.to_dict()
        assert d["profile"] != "sandboxed"
        assert d["network_isolation"] == "not_enforced"
        assert d["failure_reason"] == FailureReason.CONTAINMENT_NOT_VERIFIED.value

    def test_major_b_bad_bind_fails_closed_structural(self):
        # A non-namespace setup failure (bad --ro-bind) must fail closed with a
        # STRUCTURAL reason, not a stderr string, and run zero code.
        class BadBind(BubblewrapBackend):
            def _bwrap_argv(self, scriptdir):
                a = super()._bwrap_argv(scriptdir)
                return a[:2] + ["--ro-bind", "/nonexistent_redteam_xyz", "/mnt/x"] + a[2:]
        out = BadBind().execute(ExecutionRequest(
            "print('SNIPPET_RAN_B')", 10, ContainmentRequirement.SANDBOX_REQUIRED))
        assert out.executed is False
        assert "SNIPPET_RAN_B" not in (out.stdout or "")
        d = out.receipt.to_dict()
        assert d["profile"] != "sandboxed"
        assert d["failure_reason"] == FailureReason.NAMESPACE_SETUP_FAILED.value

    def test_bootstrap_exits_before_ready_is_zero_effect(self, monkeypatch):
        # Setup failure before READY (e.g. a resource step raising): no readiness
        # record ⇒ the snippet never runs ⇒ fail closed.
        monkeypatch.setattr(containment, "_BOOTSTRAP_SOURCE",
                            "import sys\nsys.stdin.readline()\nsys.exit(1)\n")
        out = BubblewrapBackend().execute(ExecutionRequest(
            "print('SNIPPET_RAN_C')", 10, ContainmentRequirement.SANDBOX_REQUIRED))
        assert out.executed is False
        assert "SNIPPET_RAN_C" not in (out.stdout or "")
        assert out.receipt.to_dict()["profile"] != "sandboxed"

    def test_ready_zero_flag_does_not_execute(self, monkeypatch):
        # A bootstrap whose self-check fails emits READY:<nonce>:0 and exits —
        # the snippet must not run and the receipt must not be sandboxed.
        boot = ("import sys, json\n"
                "n = sys.stdin.readline().strip()\n"
                "sys.stdin.readline(); sys.stdin.readline()\n"
                "sys.stdout.write('JARVIS_READY:'+n+':0:'+json.dumps({})+chr(10))\n"
                "sys.stdout.flush()\n"
                "sys.exit(0)\n")
        monkeypatch.setattr(containment, "_BOOTSTRAP_SOURCE", boot)
        out = BubblewrapBackend().execute(ExecutionRequest(
            "print('SNIPPET_RAN_D')", 10, ContainmentRequirement.SANDBOX_REQUIRED))
        assert out.executed is False
        assert "SNIPPET_RAN_D" not in (out.stdout or "")
        assert out.receipt.to_dict()["failure_reason"] == \
            FailureReason.CONTAINMENT_NOT_VERIFIED.value

    def test_snippet_cannot_forge_readiness(self):
        # The snippet does not know the nonce, so a forged READY line (wrong
        # nonce) is ignored — the receipt still reflects the REAL bootstrap
        # evidence, and the forged line is treated as ordinary output.
        out = BubblewrapBackend().execute(ExecutionRequest(
            "print('JARVIS_READY:deadbeef:1:{}')\nprint('AFTER')", 15,
            ContainmentRequirement.SANDBOX_REQUIRED))
        assert out.executed is True
        assert out.receipt.to_dict()["profile"] == "sandboxed"  # not fooled
        assert "AFTER" in out.stdout

    def test_forged_wrong_nonce_ready_is_rejected(self, monkeypatch):
        # A bootstrap that never emits READY but execs the snippet; the snippet
        # forges a READY line with a WRONG nonce and good-looking evidence. The
        # broker's nonce check must reject it → not certified (fail closed).
        boot = ("import sys, os\n"
                "sys.stdin.readline(); sys.stdin.readline(); sys.stdin.readline()\n"
                "os.execv(sys.executable, [sys.executable, '-I', sys.argv[1]])\n")
        monkeypatch.setattr(containment, "_BOOTSTRAP_SOURCE", boot)
        ev = dict(uid=65534, euid=65534, gid=65534, capeff="0" * 16, nnp="1",
                  home=False, shadow=False, repo=False, env_extra=[],
                  ifaces=["lo"], loopback_connect=False, nproc=CE_NPROC,
                  cpu=CE_CPU_SECONDS, as_=CE_MEM_BYTES, fsize=16 * 1024 * 1024,
                  proc_count=2, cwd="/work", tmpfs_bytes=CE_WORKSPACE_BYTES)
        import json as _j
        forge = ("print('JARVIS_READY:deadbeefdeadbeef:1:' + %r)\n" % _j.dumps(ev))
        out = BubblewrapBackend().execute(ExecutionRequest(
            forge, 15, ContainmentRequirement.SANDBOX_REQUIRED))
        assert out.receipt.to_dict()["profile"] != "sandboxed"
        assert out.executed is False

    def test_dead_loopback_probe_fails_closed(self, monkeypatch):
        # Round-2 F2: if the host loopback probe is not proven live, the run must
        # fail closed (zero execution) and never be SANDBOXED — a sandbox connect
        # failure is not evidence without a live host listener.
        monkeypatch.setattr(containment._LoopbackProbe, "verify_live",
                            lambda self: False)
        out = BubblewrapBackend().execute(ExecutionRequest(
            "print('SNIPPET_RAN_PROBE')", 15, ContainmentRequirement.SANDBOX_REQUIRED))
        assert out.executed is False
        assert "SNIPPET_RAN_PROBE" not in (out.stdout or "")
        assert out.receipt.to_dict()["profile"] != "sandboxed"
        assert out.receipt.to_dict()["failure_reason"] == \
            FailureReason.CONTAINMENT_NOT_VERIFIED.value

    def test_unestablished_canary_fails_closed(self, monkeypatch):
        # Round-2 F3: if the per-run host canary could not be established, the
        # filesystem-isolation proof is vacuous → fail closed.
        monkeypatch.setattr(containment, "_make_host_canary", lambda: ("", False))
        out = BubblewrapBackend().execute(ExecutionRequest(
            "print('SNIPPET_RAN_CANARY')", 15, ContainmentRequirement.SANDBOX_REQUIRED))
        assert out.executed is False
        assert "SNIPPET_RAN_CANARY" not in (out.stdout or "")
        assert out.receipt.to_dict()["profile"] != "sandboxed"

    def test_live_host_canary_is_unreadable_in_jail(self, tmp_path):
        # Round-2 F3 positive: a real host-only canary with random content exists on
        # the host and is provably UNREADABLE from inside the jail. Non-vacuous.
        from core.containment import _make_host_canary
        path, established = _make_host_canary()
        assert established and __import__("os").path.exists(path)
        try:
            out = BubblewrapBackend().execute(ExecutionRequest(
                f"print(open({path!r}).read())", 15,
                ContainmentRequirement.SANDBOX_REQUIRED))
            # The jail cannot read the host canary; the run is still SANDBOXED.
            assert "JARVIS_HOST_CANARY" not in (out.stdout or "")
            assert out.receipt.to_dict()["profile"] == "sandboxed"
        finally:
            __import__("os").unlink(path)

    def test_decision_ignores_stderr_wording(self, monkeypatch):
        # MAJOR B non-vacuity: a valid READY with arbitrary stderr noise is still
        # SANDBOXED (decision from the handshake, not stderr text).
        boot = containment._BOOTSTRAP_SOURCE.replace(
            'sys.stdout.flush()',
            'sys.stderr.write("Creating new namespace blah blah\\n"); sys.stderr.flush(); sys.stdout.flush()',
            1)
        monkeypatch.setattr(containment, "_BOOTSTRAP_SOURCE", boot)
        out = BubblewrapBackend().execute(ExecutionRequest(
            "print('STILL_OK')", 15, ContainmentRequirement.SANDBOX_REQUIRED))
        assert out.executed is True
        assert out.receipt.to_dict()["profile"] == "sandboxed"
        assert "STILL_OK" in out.stdout
