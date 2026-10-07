"""V69 M68D — H04: truthful network-quarantine effects.

The invariant: the effect REPORTED must be the effect that EXISTS.

Before M68D a quarantine was two firewall rules folded into one optimistic bool
and a release was two deletions whose return codes were discarded. MEASURED with
a deterministic fake backend:

* all three release scenarios — both deletions failed, one failed, both
  succeeded — returned the byte-identical ``{"released": True}``, and
  ``_active.pop(ip, None)`` ran in every one of them;
* ``release()`` wrote no audit receipt at all (four receipts for four ADD
  scenarios, zero for three RELEASE scenarios);
* IN-succeeds / OUT-fails produced one real firewall rule, ``host_isolated =
  False`` and NO entry in ``_active`` — a live rule nothing in the process knew
  about.

NO TEST HERE INVOKES ``netsh``. The backend is a stateful fake and no test needs
administrator rights.
"""
from __future__ import annotations

import asyncio
import io
import json
import tokenize
from pathlib import Path

import pytest

import core.network_quarantine as nq
from core.network_quarantine import ContainmentEffect, RuleEvidence

_SRC = Path(nq.__file__).read_text(encoding="utf-8")
_LINES = _SRC.splitlines()

#: TEST-NET-3 documentation address. Never routed, never contacted.
TARGET = "203.0.113.77"
OTHER = "203.0.113.78"


def _code_lines(src: str) -> list[str]:
    out = [""] * (len(src.splitlines()) + 2)
    prev = tokenize.ENCODING
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and prev in (
                tokenize.NEWLINE, tokenize.NL, tokenize.INDENT,
                tokenize.DEDENT, tokenize.ENCODING):
            prev = tok.type
            continue
        if tok.string.strip():
            out[tok.start[0]] += " " + tok.string
        prev = tok.type
    return out


_CODE_LINES = _code_lines(_SRC)


def code_region(header: str) -> str:
    starts = [i for i, line in enumerate(_LINES) if header in line]
    assert starts, f"region header not found: {header!r}"
    start = starts[0]
    indent = len(_LINES[start]) - len(_LINES[start].lstrip())
    end = len(_LINES)
    for i in range(start + 1, len(_LINES)):
        line = _LINES[i]
        if not line.strip():
            continue
        cur = len(line) - len(line.lstrip())
        if cur <= indent and line.lstrip().startswith(("def ", "class ", "@", "#")):
            end = i
            break
    return " ".join(_CODE_LINES[start + 1:end + 1])


# ── the fake firewall ────────────────────────────────────────────────────────

class FakeFirewall:
    """A STATEFUL stand-in for ``netsh advfirewall``.

    Stateful on purpose. A fake that maps a command substring to a return code
    cannot answer a query consistently with its own add/delete history, so it
    cannot tell a truthful implementation from a lying one — the point of H04 is
    that the two differ precisely in whether they LOOK.

    ``fail`` names rule/action pairs whose command reports failure. ``silent``
    names pairs whose command fails AND whose state does not change (a real
    failure). A pair in ``fail`` but not ``silent`` is the nastier case: the
    command reports failure and the rule exists anyway.
    ``opaque`` names rules whose QUERY cannot answer (a timeout).
    """

    def __init__(self, *, rules=(), fail=(), silent=(), opaque=()):
        self.rules: set[str] = set(rules)
        self.fail = set(fail)
        self.silent = set(silent)
        self.opaque = set(opaque)
        self.commands: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> tuple[int, str]:
        self.commands.append(list(cmd))
        # `netsh advfirewall firewall <action> rule name=…` — the action is at
        # index 3. (It is index 4 only if you miscount `netsh` itself.)
        action = cmd[3] if len(cmd) > 3 else ""
        name = next((a.split("=", 1)[1] for a in cmd if a.startswith("name=")), "")
        key = f"{action}:{name}"
        if action == "show":
            if name in self.opaque:
                return 1, "The operation timed out"
            if name in self.rules:
                return 0, f"Rule Name: {name}\nEnabled: Yes"
            return 1, "No rules match the specified criteria."
        failing = key in self.fail
        if action == "add":
            if not (failing and key in self.silent):
                self.rules.add(name)
        elif action == "delete":
            if not (failing and key in self.silent):
                self.rules.discard(name)
        return (1, "synthetic failure") if failing else (0, "Ok.")


@pytest.fixture
def fw(monkeypatch, tmp_path):
    """Install a fake backend and an elevated Windows-like platform."""
    monkeypatch.setattr(nq, "_IS_WINDOWS", True)
    monkeypatch.setattr(nq, "_is_admin", lambda: True)
    monkeypatch.setattr(nq, "_is_protected", lambda ip: False)
    monkeypatch.setattr(nq, "_log_path", lambda: tmp_path / "nq.jsonl")

    async def no_nac(ip, reason):
        return False

    monkeypatch.setattr(nq, "_nac_isolate", no_nac)
    nq._active.clear()

    def install(firewall: FakeFirewall) -> FakeFirewall:
        monkeypatch.setattr(nq, "_run", firewall)
        return firewall

    install.audit = tmp_path / "nq.jsonl"                      # type: ignore
    yield install
    nq._active.clear()


def _quarantine(ip=TARGET, reason="test"):
    return asyncio.run(nq.quarantine.__wrapped__(ip, reason=reason))


def _release(ip=TARGET):
    return asyncio.run(nq.release.__wrapped__(ip))


def _receipts(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _in(ip=TARGET):
    return f"{nq._RULE_PREFIX}-{ip}-IN"


def _out(ip=TARGET):
    return f"{nq._RULE_PREFIX}-{ip}-OUT"


# ── quarantine ───────────────────────────────────────────────────────────────

class TestQuarantineEffects:
    def test_both_rules_succeed_is_a_verified_full_effect(self, fw):
        firewall = fw(FakeFirewall())
        res = _quarantine()
        assert res["effect"] == ContainmentEffect.FULL_EFFECT.value
        assert res["host_isolated"] is True
        assert res["outcome"] == "PROVEN_COMMITTED"
        assert res["reconciliation_required"] is False
        assert {_in(), _out()} <= firewall.rules
        assert TARGET in nq._active
        # Every rule carries its own evidence, and it was QUERIED.
        assert [r["observed_present"] for r in res["rules"]] == [True, True]
        assert [r["command_rc"] for r in res["rules"]] == [0, 0]

    def test_in_succeeds_out_fails_is_partial_and_stays_tracked(self, fw):
        """THE measured failure: a live rule that nothing knew about."""
        firewall = fw(FakeFirewall(fail={f"add:{_out()}"},
                                   silent={f"add:{_out()}"}))
        res = _quarantine()
        assert _in() in firewall.rules, "the IN rule really was created"
        assert _out() not in firewall.rules
        assert res["host_isolated"] is False, "a partial effect is not isolation"
        assert res["effect"] in (ContainmentEffect.PARTIAL_EFFECT.value,
                                 ContainmentEffect.RECONCILIATION_REQUIRED.value)
        assert res["reconciliation_required"] is True
        assert res["outcome"] != "PROVEN_COMMITTED"
        assert TARGET in nq._active, \
            "the partial effect was dropped from local tracking"
        assert nq.pending_reconciliation().get(TARGET)

    def test_out_succeeds_in_fails_is_partial_and_stays_tracked(self, fw):
        firewall = fw(FakeFirewall(fail={f"add:{_in()}"}, silent={f"add:{_in()}"}))
        res = _quarantine()
        assert _out() in firewall.rules and _in() not in firewall.rules
        assert res["host_isolated"] is False
        assert res["reconciliation_required"] is True
        assert TARGET in nq._active

    def test_both_rules_fail_is_no_effect_and_is_not_tracked(self, fw):
        firewall = fw(FakeFirewall(fail={f"add:{_in()}", f"add:{_out()}"},
                                   silent={f"add:{_in()}", f"add:{_out()}"}))
        res = _quarantine()
        assert firewall.rules == set()
        assert res["effect"] == ContainmentEffect.NO_EFFECT.value
        assert res["outcome"] == "PROVEN_NOT_EXECUTED"
        assert res["host_isolated"] is False
        assert TARGET not in nq._active, \
            "nothing exists, so there is nothing to reconcile"

    def test_a_command_that_reports_failure_but_created_the_rule_is_seen(self, fw):
        """The query is what makes this detectable; the rc says the opposite."""
        firewall = fw(FakeFirewall(fail={f"add:{_in()}"}))      # NOT silent
        res = _quarantine()
        assert _in() in firewall.rules, "the rule exists despite the failure"
        in_rule = next(r for r in res["rules"] if r["direction"] == "in")
        assert in_rule["command_rc"] == 1
        assert in_rule["observed_present"] is True
        assert in_rule["outcome"] == "PROVEN_COMMITTED", \
            "a rule that EXISTS must not be reported as not executed"
        assert res["effect"] == ContainmentEffect.FULL_EFFECT.value
        assert res["host_isolated"] is True

    def test_an_unanswerable_query_is_unknown_not_a_guess(self, fw):
        fw(FakeFirewall(fail={f"add:{_out()}"}, silent={f"add:{_out()}"},
                        opaque={_out()}))
        res = _quarantine()
        out_rule = next(r for r in res["rules"] if r["direction"] == "out")
        assert out_rule["observed_present"] is None
        assert out_rule["outcome"] == "UNKNOWN"
        assert res["effect"] == ContainmentEffect.RECONCILIATION_REQUIRED.value
        assert res["host_isolated"] is False
        assert TARGET in nq._active

    def test_both_queries_unanswerable_is_unknown_effect(self, fw):
        fw(FakeFirewall(fail={f"add:{_in()}", f"add:{_out()}"},
                        silent={f"add:{_in()}", f"add:{_out()}"},
                        opaque={_in(), _out()}))
        res = _quarantine()
        assert res["effect"] == ContainmentEffect.UNKNOWN_EFFECT.value
        assert res["outcome"] == "UNKNOWN"
        assert res["host_isolated"] is False
        assert TARGET in nq._active, "an unknown effect must stay reconcilable"

    @pytest.mark.parametrize("setup,reason", [
        ({"enabled": False}, "disabled"),
        ({"admin": False}, "no admin / unsupported"),
        ({"protected": True}, "protected infra/self IP"),
    ])
    def test_a_refusal_is_proven_not_executed_and_audited(self, fw, monkeypatch,
                                                           setup, reason):
        firewall = fw(FakeFirewall())
        if setup.get("enabled") is False:
            monkeypatch.setattr(nq, "_QUARANTINE_ENABLED", False)
        if setup.get("admin") is False:
            monkeypatch.setattr(nq, "_is_admin", lambda: False)
        if setup.get("protected"):
            monkeypatch.setattr(nq, "_is_protected", lambda ip: True)
        res = _quarantine()
        assert res["skipped"] == reason
        assert res["effect"] == ContainmentEffect.NO_EFFECT.value
        assert res["outcome"] == "PROVEN_NOT_EXECUTED"
        assert firewall.commands == [], "a refusal must reach no command"
        assert TARGET not in nq._active
        assert _receipts(fw.audit), "a refusal must still be audited"

    def test_an_invalid_ip_literal_is_refused_before_any_command(self, fw):
        firewall = fw(FakeFirewall())
        res = asyncio.run(nq.quarantine.__wrapped__("not-an-ip"))
        assert res["skipped"] == "invalid IP literal"
        assert firewall.commands == []

    def test_the_active_cap_is_honoured_and_audited(self, fw, monkeypatch):
        firewall = fw(FakeFirewall())
        monkeypatch.setattr(nq, "_MAX_ACTIVE", 1)
        _quarantine(TARGET)
        res = _quarantine(OTHER)
        assert res["skipped"] == "max active quarantines reached"
        assert OTHER not in nq._active
        assert not any(OTHER in " ".join(c) for c in firewall.commands[2:])


class TestQuarantineIdempotence:
    def test_a_duplicate_request_reports_the_tracked_state_and_audits_it(self, fw):
        firewall = fw(FakeFirewall())
        first = _quarantine()
        before = len(firewall.commands)
        second = _quarantine()
        assert second["skipped"] == "already quarantined"
        assert len(firewall.commands) == before, "a duplicate issued new commands"
        assert second["effect"] == first["effect"]
        assert second["host_isolated"] == first["host_isolated"]
        # Was silently un-audited before M68D.
        assert len(_receipts(fw.audit)) == 2

    def test_a_duplicate_of_a_partial_quarantine_does_not_claim_isolation(self, fw):
        fw(FakeFirewall(fail={f"add:{_out()}"}, silent={f"add:{_out()}"}))
        _quarantine()
        second = _quarantine()
        assert second["skipped"] == "already quarantined"
        assert second["host_isolated"] is False
        assert second["reconciliation_required"] is True

    def test_retry_after_a_partial_failure_can_complete_it(self, fw):
        firewall = fw(FakeFirewall(fail={f"add:{_out()}"},
                                   silent={f"add:{_out()}"}))
        _quarantine()
        assert nq.pending_reconciliation().get(TARGET)
        # The operator clears the tracking after reconciling, the failure
        # condition goes away, and a fresh attempt completes.
        nq._active.pop(TARGET, None)
        firewall.fail.clear()
        firewall.silent.clear()
        res = _quarantine()
        assert res["effect"] == ContainmentEffect.FULL_EFFECT.value
        assert res["host_isolated"] is True


# ── release ──────────────────────────────────────────────────────────────────

class TestReleaseEffects:
    def test_both_deletions_verified_absent_is_a_real_release(self, fw):
        firewall = fw(FakeFirewall())
        _quarantine()
        res = _release()
        assert res["released"] is True
        assert res["effect"] == ContainmentEffect.FULL_EFFECT.value
        assert res["outcome"] == "PROVEN_COMMITTED"
        assert res["reconciliation_required"] is False
        assert firewall.rules == set()
        assert TARGET not in nq._active
        assert res["still_tracked"] is False

    def test_both_deletions_failing_does_not_report_release(self, fw):
        """THE measured lie: ``released = True`` with both deletions failed."""
        firewall = fw(FakeFirewall())
        _quarantine()
        firewall.fail = {f"delete:{_in()}", f"delete:{_out()}"}
        firewall.silent = set(firewall.fail)
        res = _release()
        assert {_in(), _out()} <= firewall.rules, "the rules really are still there"
        assert res["released"] is False
        assert res["outcome"] == "PROVEN_NOT_EXECUTED"
        assert res["effect"] == ContainmentEffect.NO_EFFECT.value
        assert res["reconciliation_required"] is True
        assert TARGET in nq._active, "tracking was erased with the rules still live"
        assert res["still_tracked"] is True

    def test_one_deletion_failing_is_partial_and_stays_tracked(self, fw):
        firewall = fw(FakeFirewall())
        _quarantine()
        firewall.fail = {f"delete:{_out()}"}
        firewall.silent = set(firewall.fail)
        res = _release()
        assert _in() not in firewall.rules and _out() in firewall.rules
        assert res["released"] is False
        assert res["effect"] == ContainmentEffect.PARTIAL_EFFECT.value
        assert res["reconciliation_required"] is True
        assert TARGET in nq._active

    def test_the_three_release_outcomes_are_distinguishable(self, fw):
        """Before M68D all three returned the byte-identical result."""
        seen = []
        for failures in (set(), {f"delete:{_out()}"},
                         {f"delete:{_in()}", f"delete:{_out()}"}):
            nq._active.clear()
            firewall = fw(FakeFirewall())
            _quarantine()
            firewall.fail = set(failures)
            firewall.silent = set(failures)
            res = _release()
            seen.append((res["released"], res["effect"], res["outcome"],
                         res["reconciliation_required"]))
        assert len(set(seen)) == 3, f"the three outcomes collapsed: {seen}"

    def test_releasing_something_already_absent_is_a_verified_full_effect(self, fw):
        """Idempotent: nothing to delete IS the desired end state."""
        firewall = fw(FakeFirewall())
        res = _release()
        assert firewall.rules == set()
        assert res["released"] is True
        assert res["effect"] == ContainmentEffect.FULL_EFFECT.value
        assert TARGET not in nq._active

    def test_releasing_with_one_rule_already_absent_still_completes(self, fw):
        fw(FakeFirewall(rules={_out()}))
        res = _release()
        assert res["released"] is True
        assert res["effect"] == ContainmentEffect.FULL_EFFECT.value

    def test_an_unanswerable_query_blocks_the_release_claim(self, fw):
        firewall = fw(FakeFirewall())
        _quarantine()
        firewall.opaque = {_out()}
        res = _release()
        assert res["released"] is False
        assert res["outcome"] == "UNKNOWN"
        assert res["reconciliation_required"] is True
        assert TARGET in nq._active

    def test_a_deletion_that_reports_failure_but_worked_is_seen_as_released(self, fw):
        firewall = fw(FakeFirewall())
        _quarantine()
        firewall.fail = {f"delete:{_in()}", f"delete:{_out()}"}   # NOT silent
        res = _release()
        assert firewall.rules == set()
        assert res["released"] is True, \
            "the rules are gone; a non-zero rc must not outrank the query"
        assert res["effect"] == ContainmentEffect.FULL_EFFECT.value

    def test_retrying_a_failed_release_can_succeed(self, fw):
        firewall = fw(FakeFirewall())
        _quarantine()
        firewall.fail = {f"delete:{_in()}", f"delete:{_out()}"}
        firewall.silent = set(firewall.fail)
        assert _release()["released"] is False
        assert TARGET in nq._active
        firewall.fail.clear()
        firewall.silent.clear()
        res = _release()
        assert res["released"] is True
        assert TARGET not in nq._active

    def test_the_receipt_states_what_each_delete_command_returned(self, fw):
        """The query decides the OUTCOME; the rc is still evidence.

        Because absence is proven by a query, discarding the deletion return
        codes does not change what `released` says — which is exactly why the
        per-rule `command_rc` has to be asserted. Without this, a mutation that
        throws the return codes away again is invisible (measured: it survived
        the first campaign run).
        """
        firewall = fw(FakeFirewall())
        _quarantine()
        firewall.fail = {f"delete:{_out()}"}
        firewall.silent = set(firewall.fail)
        res = _release()
        by_direction = {r["direction"]: r for r in res["rules"]}
        assert by_direction["in"]["command_rc"] == 0
        assert by_direction["out"]["command_rc"] == 1, \
            "the receipt does not carry the failing command's return code"
        assert by_direction["out"]["attempted"] is True

    def test_the_receipt_states_what_each_add_command_returned(self, fw):
        firewall = fw(FakeFirewall(fail={f"add:{_out()}"},
                                   silent={f"add:{_out()}"}))
        res = _quarantine()
        by_direction = {r["direction"]: r for r in res["rules"]}
        assert by_direction["in"]["command_rc"] == 0
        assert by_direction["out"]["command_rc"] == 1
        assert firewall.commands, "non-vacuity: no command was issued"

    def test_every_release_writes_an_audit_receipt(self, fw):
        """``release()`` used to write none at all."""
        firewall = fw(FakeFirewall())
        _quarantine()
        before = len(_receipts(fw.audit))
        firewall.fail = {f"delete:{_out()}"}
        firewall.silent = set(firewall.fail)
        _release()
        after = _receipts(fw.audit)
        assert len(after) == before + 1, "the release was not audited"
        assert after[-1]["ip"] == TARGET
        assert after[-1]["released"] is False
        assert after[-1]["rules"], "the receipt carries no per-rule evidence"

    def test_a_release_without_privileges_does_not_erase_tracking(self, fw,
                                                                  monkeypatch):
        fw(FakeFirewall())
        _quarantine()
        monkeypatch.setattr(nq, "_is_admin", lambda: False)
        res = _release()
        assert res["released"] is False
        assert res.get("error")
        assert TARGET in nq._active
        assert res["reconciliation_required"] is True

    def test_an_invalid_ip_release_is_refused_and_audited(self, fw):
        firewall = fw(FakeFirewall())
        res = asyncio.run(nq.release.__wrapped__("not-an-ip"))
        assert res["released"] is False and res.get("error")
        assert firewall.commands == []
        assert _receipts(fw.audit), "the refusal was not audited"


# ── the derivation itself ────────────────────────────────────────────────────

class TestEffectDerivation:
    @staticmethod
    def _ev(outcome, direction="in"):
        return RuleEvidence(name="n", direction=direction, action="add",
                            attempted=True, command_rc=0, outcome=outcome)

    @pytest.mark.parametrize("outcomes,expected", [
        ([], ContainmentEffect.NO_EFFECT),
        (["PROVEN_COMMITTED", "PROVEN_COMMITTED"], ContainmentEffect.FULL_EFFECT),
        (["PROVEN_NOT_EXECUTED", "PROVEN_NOT_EXECUTED"], ContainmentEffect.NO_EFFECT),
        (["PROVEN_COMMITTED", "PROVEN_NOT_EXECUTED"],
         ContainmentEffect.PARTIAL_EFFECT),
        (["UNKNOWN", "UNKNOWN"], ContainmentEffect.UNKNOWN_EFFECT),
        (["PROVEN_COMMITTED", "UNKNOWN"],
         ContainmentEffect.RECONCILIATION_REQUIRED),
        (["PROVEN_NOT_EXECUTED", "UNKNOWN"], ContainmentEffect.UNKNOWN_EFFECT),
    ])
    def test_the_fold_never_rounds_an_unknown_into_a_certainty(self, outcomes,
                                                               expected):
        rules = [self._ev(o) for o in outcomes]
        assert nq._derive_effect(rules) is expected

    @pytest.mark.parametrize("rc,present,want,expected", [
        (0, None, True, "PROVEN_COMMITTED"),
        (1, None, True, "UNKNOWN"),
        (None, None, True, "UNKNOWN"),
        (0, True, True, "PROVEN_COMMITTED"),
        (1, True, True, "PROVEN_COMMITTED"),
        (0, False, True, "PROVEN_NOT_EXECUTED"),
        (0, False, False, "PROVEN_COMMITTED"),
        (1, True, False, "PROVEN_NOT_EXECUTED"),
        (1, None, False, "UNKNOWN"),
    ])
    def test_a_query_outranks_a_return_code(self, rc, present, want, expected):
        assert nq._outcome_for(rc, present, want_present=want) == expected

    def test_a_failing_rc_is_never_proven_not_executed_without_a_query(self):
        """A timeout returns ``(1, …)`` from `_run` and proves nothing."""
        assert nq._outcome_for(1, None, want_present=True) == "UNKNOWN"
        assert nq._outcome_for(1, None, want_present=False) == "UNKNOWN"

    def test_an_uninterpretable_query_result_is_none_not_false(self, fw):
        fw(FakeFirewall(opaque={_in()}))
        assert nq._rule_present(_in()) is None

    def test_the_absence_answer_is_recognised_as_absence(self, fw):
        fw(FakeFirewall())
        assert nq._rule_present(_in()) is False

    def test_a_present_rule_is_recognised(self, fw):
        fw(FakeFirewall(rules={_in()}))
        assert nq._rule_present(_in()) is True

    def test_active_targets_returns_a_copy(self, fw):
        fw(FakeFirewall())
        _quarantine()
        snapshot = nq.active_targets()
        snapshot[TARGET]["effect"] = "TAMPERED"
        assert nq._active[TARGET]["effect"] != "TAMPERED"

    def test_pending_reconciliation_is_empty_for_a_clean_full_effect(self, fw):
        fw(FakeFirewall())
        _quarantine()
        assert nq.pending_reconciliation() == {}


# ── absent-control ──────────────────────────────────────────────────────────

class TestAbsentControls:
    def test_release_consumes_every_command_return_code(self):
        body = code_region("async def release(ip: str) -> dict:")
        assert "rc , out = await loop . run_in_executor ( None , _run , cmd )" in body, \
            "a command result is discarded"
        assert "command_rc = rc" in body
        assert "await loop . run_in_executor ( None , _run , c )" not in body, \
            "the old result-discarding call shape is back"

    def test_release_cannot_set_success_unconditionally(self):
        body = code_region("async def release(ip: str) -> dict:")
        assert 'res [ "released" ] = True' not in body, \
            "release assigns success without reference to evidence"
        assert 'res [ "released" ] = proven_absent' in body
        assert "proven_absent = all ( r . observed_present is False for r in rules )" \
            in body, "release no longer requires every rule to be PROVEN absent"

    def test_release_cannot_erase_tracking_before_a_verified_release(self):
        body = code_region("async def release(ip: str) -> dict:")
        pop_at = body.index('_active . pop ( ip , None )')
        guard_at = body.index("if proven_absent :")
        assert guard_at < pop_at, "the pop is not guarded by the proof"

    def test_a_partial_add_cannot_be_dropped_from_tracking(self):
        body = code_region("async def quarantine(ip: str, *, reason")
        assert "if effect is not ContainmentEffect . NO_EFFECT :" in body, \
            "tracking is gated on something other than 'any effect at all'"
        assert '_active [ ip ] = res' in body
        assert 'if ok : _active [ ip ] = res [ "ts" ]' not in body

    def test_every_effectful_path_writes_a_receipt(self):
        """Non-vacuity: the inventory is asserted non-empty first."""
        regions = {
            "quarantine": code_region("async def quarantine(ip: str, *, reason"),
            "release": code_region("async def release(ip: str) -> dict:"),
            "skipped": code_region("def _skipped(ip: str, reason: str"),
        }
        assert regions
        for name, body in regions.items():
            assert "_audit (" in body, f"{name} writes no audit receipt"

    def test_there_is_no_second_quarantine_entrypoint(self):
        """A bypass would make all of the above decorative."""
        add_sites = [line for line in _LINES
                     if '"add"' in line and "rule" in line]
        assert add_sites, "non-vacuity: no rule-adding site found"
        assert " ".join(_CODE_LINES).count('"add" , "rule"') == 1, \
            "more than one path creates firewall rules"
        assert " ".join(_CODE_LINES).count('"delete" , "rule"') == 1, \
            "more than one path deletes firewall rules"

    def test_the_effect_vocabulary_is_total_and_distinct(self):
        values = [e.value for e in ContainmentEffect]
        assert len(values) == len(set(values)) == 5
        assert set(values) == {
            "NO_EFFECT", "FULL_EFFECT", "PARTIAL_EFFECT", "UNKNOWN_EFFECT",
            "RECONCILIATION_REQUIRED"}

    def test_the_per_rule_outcome_uses_the_m65d_vocabulary(self):
        """§26: reuse ExternalOutcome, do not fork a parallel truth model."""
        from core.effect_journal import ExternalOutcome

        governed = {o.value for o in ExternalOutcome}
        assert {nq._PROVEN_COMMITTED, nq._PROVEN_NOT_EXECUTED,
                nq._UNKNOWN} == governed

    def test_the_receipt_does_not_grant_authority(self):
        """The clearance decorator is still the gate, not the receipt."""
        assert hasattr(nq.quarantine, "__wrapped__"), \
            "quarantine is no longer clearance-gated"
        assert hasattr(nq.release, "__wrapped__"), \
            "release is no longer clearance-gated"

    def test_no_test_in_this_module_can_reach_real_netsh(self):
        """Self-check on the suite's own safety claim."""
        import ast

        src = Path(__file__).read_text(encoding="utf-8")
        assert "FakeFirewall" in src
        assert 'monkeypatch.setattr(nq, "_run", firewall)' in src
        # `_run` is the ONLY thing in this module that reaches a process, and
        # every test replaces it. Parsed rather than grepped: a substring search
        # finds the forbidden name inside this very assertion (measured).
        imported: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported, "non-vacuity: no imports were parsed at all"
        for forbidden in ("subprocess", "os", "shutil", "socket"):
            assert forbidden not in imported, \
                f"this suite imports {forbidden}; it must not reach the host"
