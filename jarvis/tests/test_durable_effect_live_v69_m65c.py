"""
tests/test_durable_effect_live_v69_m65c.py — V69 M65C LIVE RUNTIME RECOVERY.

Every scenario here enters through ``LLM.chat_stream`` — the same generator
``main._run_turn`` drives on every operator turn — because a test that reaches
past the integration layer proves the journal works and says nothing about
whether JARVIS uses it.

The two proofs the milestone rests on are §60 and §61:

  * a committed effect whose RESPONSE was lost must come back as a recovered
    receipt, with no second effect;
  * an effect whose OUTCOME was lost must come back as "I cannot tell", with no
    second effect and no claim of success.

The second one is the harder claim and the more important one. It is easy to
build a system that recovers when it knows the answer; the milestone is about
what it does when it does not.

The wire-level fakes and the turn harness are IMPORTED from the M64.1 live suite
rather than copied — two harnesses meant to be identical that drifted would let
this file prove something about a turn the other file no longer runs.

Nothing here opens a socket, names a public target, or touches a holdout. The
external effect is a file in a temporary directory.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_mesh_live_turn_v69_m64_1 import LiveTurn  # noqa: E402

from core.effect_journal import (  # noqa: E402
    DurableEffectJournal,
    EffectDurabilityClass,
    EffectState,
    ExecutionDisposition,
    compute_effect_id,
    register_durability,
    unregister_durability,
)
from core.security_effects import CONTAINMENT, SCOPES  # noqa: E402
from tests.support import m65c_effect_world  # noqa: E402
from tests.support.m65c_effect_world import CRASH_EXIT, SyntheticWorld  # noqa: E402

WORKER = Path(m65c_effect_world.__file__).resolve()
JARVIS_ROOT = WORKER.parent.parent.parent
JOIN_S = 60.0

#: A test-owned effectful tool. Registered on the executor by the harness, so it
#: passes every real gate — preflight, guardrails, authority, the risk class, the
#: NATO challenge and the effect protocol — on the way to the handler.
TOOL = "code_execute"
ARGS = {"code": "deploy('synthetic')"}


#: The identity scope these scenarios run under.
#:
#: In production ``mesh_live.effect_epoch`` returns ``turn:<task_id>``, and
#: ``task_id`` is the DURABLE turn id when the turn has one — falling back to
#: ``id(task_decision)``, a memory address, when it does not. That fallback is
#: correct: two separate operator requests to deploy the same thing are two
#: effects, and fusing them would make the second one impossible to ever run.
#:
#: It also means durable dedupe reaches across a restart exactly as far as the
#: caller's scope does. These tests pin the scope, which is what a resumed task
#: with a durable id supplies in production; the limitation is stated plainly in
#: the milestone document rather than hidden behind the pin, and
#: ``test_two_turns_with_different_scopes_are_two_effects`` asserts the other
#: half of the behaviour.
PINNED_SCOPE = "turn:m65c-durable-intent"


@pytest.fixture
def live(monkeypatch, tmp_path):
    """The M64.1 live turn, with a journal this test owns."""
    CONTAINMENT.authorizations = []
    SCOPES.scopes = []
    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL_PATH", str(tmp_path / "effects.db"))
    # A short lease, set the way a deployment would set it. Without this the
    # live executor would apply the 900s production default and read a crashed
    # owner as merely slow — correct in production, useless in a test that has
    # to reach the stale-owner branch at all. The worker below uses the same
    # values, so both sides agree on what "gone" means.
    monkeypatch.setenv("JARVIS_EFFECT_LEASE_S", "1")
    monkeypatch.setenv("JARVIS_EFFECT_LEASE_GRACE_S", "0")
    monkeypatch.setattr("core.mesh_live.effect_epoch",
                        lambda turn, fallback: PINNED_SCOPE)
    turn = LiveTurn(monkeypatch)
    turn.journal_path = tmp_path / "effects.db"
    turn.world = SyntheticWorld(tmp_path / "world").prepare()
    return turn


def ask(live, message: str):
    return asyncio.run(live.ask(message))


def tool_messages(live) -> str:
    """Everything the model was told by a tool, across every generation.

    ``last_messages`` holds only the final ``create()`` call's transcript, and
    the tool result that matters may have been sent to an earlier one.
    """
    seen: list[str] = []
    for call in live.client.calls:
        for message in (call.get("messages") or []):
            if message.get("role") == "tool":
                seen.append(str(message.get("content", "")))
    return " ".join(seen)


def epoch_of(live) -> str:
    return PINNED_SCOPE


def run_worker(payload, result: "Path | None" = None, *,
               expect_crash: bool = False) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{JARVIS_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    proc = subprocess.run(
        [sys.executable, str(WORKER), json.dumps(payload),
         str(result) if result else ""],
        env=env, cwd=str(JARVIS_ROOT), capture_output=True, timeout=JOIN_S)
    expected = CRASH_EXIT if expect_crash else 0
    assert proc.returncode == expected, (
        f"worker exited {proc.returncode}: {proc.stderr.decode()[-800:]}")
    if result is not None and result.exists():
        return json.loads(result.read_text())
    return {}


def worker_spec(live, *, mode="non_replayable",
                cls=EffectDurabilityClass.NON_REPLAYABLE, crash_at="",
                scope=None, **kw) -> dict:
    payload = {
        "journal": str(live.journal_path), "world": str(live.world.root),
        "tool": TOOL, "scope": scope if scope is not None else epoch_of(live),
        "args": json.dumps(ARGS), "durability_class": cls.value, "mode": mode,
        "crash_at": crash_at, "lease_s": 1.0, "lease_grace_s": 0.0,
    }
    payload.update(kw)
    return payload


# ══════════════════════════════════════════════════════════════════════════════
#  LIVE_JARVIS_RECOVERY — §60
# ══════════════════════════════════════════════════════════════════════════════
def test_a_live_turn_journals_the_effect_it_executes(live):
    """The precondition for everything below: the operator path really does
    reach the durable journal, not just a unit-test executor."""
    live.add_effect_tool(TOOL, {"stdout": "deployed"})
    live.client.call_tool(TOOL, ARGS)
    live.client.say("Done.")
    r = ask(live, "run the synthetic deployment")

    assert r.effect_count(TOOL) == 1
    journal = DurableEffectJournal(live.journal_path)
    assert journal.status()["committed"] == 1


def test_a_lost_response_resolves_from_the_journal_with_no_second_effect(live):
    """§60 — a worker process commits the effect, the response never reaches the
    operator, a NEW runtime instance handles the same intent.

    The worker is a separate interpreter with its own runtime instance id, so
    the live executor genuinely inherits a stranger's committed row rather than
    reading back its own memory.
    """
    live.add_effect_tool(TOOL, {"stdout": "deployed"})
    # 1. Another process performs and commits the effect, then dies before the
    #    operator is told anything (C4: committed, response never delivered).
    run_worker(worker_spec(live, crash_at="C4"), expect_crash=True)
    assert live.world.effect_count == 1

    # 2. A new runtime instance takes the same operator intent.
    live.client.call_tool(TOOL, ARGS)
    live.client.say("Reporting.")
    r = ask(live, "run the synthetic deployment")

    assert r.effect_count(TOOL) == 0, "the live turn repeated a committed effect"
    assert live.world.effect_count == 1
    journal = DurableEffectJournal(live.journal_path)
    record = journal.get(compute_effect_id(
        surface="native", tool_id=TOOL, identity_scope=epoch_of(live),
        tool_input=ARGS))
    assert record.state is EffectState.COMMITTED


def test_the_recovered_turn_still_answers_the_operator(live):
    """A recovery that costs the operator their answer is not a recovery."""
    live.add_effect_tool(TOOL, {"stdout": "deployed"})
    run_worker(worker_spec(live, crash_at="C4"), expect_crash=True)
    live.client.call_tool(TOOL, ARGS)
    live.client.say("The deployment was already applied.")
    r = ask(live, "run the synthetic deployment")
    assert r.text.strip()


def test_the_recovered_tool_result_names_its_disposition(live):
    """The model is told what actually happened, not a fabricated success."""
    live.add_effect_tool(TOOL, {"stdout": "deployed"})
    run_worker(worker_spec(live, crash_at="C4"), expect_crash=True)
    live.client.call_tool(TOOL, ARGS)
    live.client.say("Understood.")
    ask(live, "run the synthetic deployment")

    blob = tool_messages(live)
    assert blob, "the turn produced no tool message"
    assert ExecutionDisposition.RECOVERED_COMMITTED.value in blob
    assert "not retained" in blob, (
        "the recovery envelope did not say the body is unavailable")


# ══════════════════════════════════════════════════════════════════════════════
#  LIVE_JARVIS_INDETERMINATE — §61
# ══════════════════════════════════════════════════════════════════════════════
def test_a_lost_outcome_is_not_replayed_and_not_claimed_as_success(live):
    """§61 — the milestone's hardest claim, on the live path.

    A NON_REPLAYABLE effect really happened and the process died before the
    commit. JARVIS must not replay it, must not say it succeeded, and must say
    the outcome needs reconciliation.
    """
    live.add_effect_tool(TOOL, {"stdout": "deployed"})
    run_worker(worker_spec(live, crash_at="C3"), expect_crash=True)
    time.sleep(1.2)   # the only wall-clock wait: a real lease must really expire
    assert live.world.effect_count == 1, "the synthetic effect did not happen"

    live.client.call_tool(TOOL, ARGS)
    live.client.say("Reporting.")
    r = ask(live, "run the synthetic deployment")

    assert r.effect_count(TOOL) == 0, "an INDETERMINATE effect was replayed"
    assert live.world.effect_count == 1, "the external effect was duplicated"

    blob = tool_messages(live)
    assert "indeterminate_effect" in blob, (
        "the model was not told the outcome is unknown")
    assert "reconcilia" in blob.lower(), (
        "the model was not told reconciliation is required")


def test_the_indeterminate_effect_is_visible_in_the_journal(live):
    live.add_effect_tool(TOOL, {"stdout": "deployed"})
    run_worker(worker_spec(live, crash_at="C3"), expect_crash=True)
    time.sleep(1.2)   # the only wall-clock wait: a real lease must really expire
    live.client.call_tool(TOOL, ARGS)
    live.client.say("Reporting.")
    ask(live, "run the synthetic deployment")

    journal = DurableEffectJournal(live.journal_path)
    status = journal.status()
    assert status["indeterminate"] == 1
    assert status["recovery_required"] is True


def test_asking_again_does_not_wear_down_the_refusal(live):
    """§12 — repetition is not evidence. A second and third request must not
    eventually be allowed through."""
    live.add_effect_tool(TOOL, {"stdout": "deployed"})
    run_worker(worker_spec(live, crash_at="C3"), expect_crash=True)
    time.sleep(1.2)   # the only wall-clock wait: a real lease must really expire
    for _ in range(3):
        live.client.call_tool(TOOL, ARGS)
        live.client.say("Reporting.")
        ask(live, "run the synthetic deployment")
    assert live.world.effect_count == 1, "persistence produced a duplicate effect"


# ══════════════════════════════════════════════════════════════════════════════
#  HITL_RECOVERY / SCOPE_RECOVERY — §43/§44
# ══════════════════════════════════════════════════════════════════════════════
def test_a_journal_row_does_not_grant_the_hitl_approval(live):
    """§43 — durable reservation is not permission. A committed row identifies
    an effect; it never resurrects the authority that let it happen."""
    live.add_effect_tool(TOOL, {"stdout": "deployed"})
    live.client.call_tool(TOOL, ARGS)
    live.client.say("Done.")
    first = ask(live, "run the synthetic deployment")
    assert first.effect_count(TOOL) == 1

    # A DIFFERENT effect identity, with the challenge now denying. The prior
    # commit must buy this one nothing.
    live.hitl_grants = False
    live.client.call_tool(TOOL, {"code": "deploy('second')"})
    live.client.say("Understood.")
    second = ask(live, "run the other synthetic deployment")

    assert second.effect_count(TOOL) == 1, (
        "a journalled effect granted a later one its approval")


def test_the_challenge_is_asked_again_for_a_new_identity(live):
    """Recovery of a proven receipt is not a new effect and asks nothing; a NEW
    effect is a new effect and must be challenged on its own."""
    live.add_effect_tool(TOOL, {"stdout": "deployed"})
    challenges: list = []
    real = live.executor._challenge

    async def _counting(tool_name, preview):
        challenges.append(tool_name)
        return await real(tool_name, preview)

    live.executor._challenge = _counting

    run_worker(worker_spec(live, crash_at="C4"), expect_crash=True)
    live.client.call_tool(TOOL, ARGS)
    live.client.say("Done.")
    ask(live, "run the synthetic deployment")
    assert challenges == [], (
        "a recovered receipt asked the operator to approve it a second time")

    live.client.call_tool(TOOL, {"code": "deploy('fresh')"})
    live.client.say("Done.")
    ask(live, "run a different deployment")
    assert challenges, "a genuinely new effect skipped its challenge"


def test_the_journal_binds_the_authority_but_never_returns_it(live):
    """§44 — the row carries a digest so a later attempt can be COMPARED
    against it. There is no field a scope can be read back out of."""
    live.add_effect_tool(TOOL, {"stdout": "deployed"})
    live.client.call_tool(TOOL, ARGS)
    live.client.say("Done.")
    ask(live, "run the synthetic deployment")

    journal = DurableEffectJournal(live.journal_path)
    record = journal.get(compute_effect_id(
        surface="native", tool_id=TOOL, identity_scope=epoch_of(live),
        tool_input=ARGS))
    rendered = json.dumps(record.to_dict())
    assert len(record.authority_digest) == 64
    assert "STANDARD" not in rendered and "scopes" not in rendered


# ══════════════════════════════════════════════════════════════════════════════
#  RUNTIME_DOCTOR — §48
# ══════════════════════════════════════════════════════════════════════════════
def test_the_doctor_reports_journal_health_without_an_llm(live, monkeypatch):
    from core.runtime_doctor import DoctorStatus, check_effect_journal

    findings = {f.check_id: f for f in check_effect_journal()}
    assert findings["effects.journal"].status is DoctorStatus.PASS
    assert findings["effects.recovery"].status is DoctorStatus.PASS
    assert "schema v" in findings["effects.journal"].evidence


def test_the_doctor_blocks_on_an_indeterminate_effect(live):
    from core.runtime_doctor import DoctorStatus, check_effect_journal

    run_worker(worker_spec(live, crash_at="C3"), expect_crash=True)
    time.sleep(1.2)   # the only wall-clock wait: a real lease must really expire
    journal = DurableEffectJournal(live.journal_path, lease_s=0, lease_grace_s=0)
    journal.startup_recovery()

    findings = {f.check_id: f for f in check_effect_journal()}
    assert findings["effects.recovery"].status is DoctorStatus.BLOCKED
    assert "reconcil" in findings["effects.recovery"].remediation.lower()


def test_the_doctor_reports_a_disabled_journal_as_degraded(live, monkeypatch):
    from core.runtime_doctor import DoctorStatus, check_effect_journal

    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL", "0")
    findings = {f.check_id: f for f in check_effect_journal()}
    assert findings["effects.journal"].status is DoctorStatus.DEGRADED
    assert "DISABLED" in findings["effects.journal"].evidence


def test_the_doctor_never_leaks_an_argument(live):
    from core.runtime_doctor import check_effect_journal

    secret = "sk-live-m65c-doctor-secret"
    live.add_effect_tool(TOOL, {"stdout": "ok"})
    live.client.call_tool(TOOL, {"code": secret})
    live.client.say("Done.")
    ask(live, "run it")

    blob = " ".join(f"{f.evidence} {f.remediation}"
                    for f in check_effect_journal())
    assert secret not in blob


def test_the_doctor_runs_the_journal_check_in_the_full_diagnostic(live):
    from core.runtime_doctor import run_diagnostics

    report = run_diagnostics(include_network=False)
    ids = {f.check_id for f in report.findings}
    assert "effects.journal" in ids and "effects.recovery" in ids


# ══════════════════════════════════════════════════════════════════════════════
#  THE SCOPE BOUNDARY — stated, not hidden
# ══════════════════════════════════════════════════════════════════════════════
def test_two_turns_with_different_scopes_are_two_effects(live, monkeypatch):
    """The other half of the identity rule, and it is CORRECT behaviour.

    Durable dedupe is scoped to the caller's declared identity. Two separate
    operator requests to deploy the same thing are two effects, and fusing them
    would make the second one impossible to ever run — a system that refused to
    ever repeat an action would be broken in a more annoying way than one that
    repeated it.

    The consequence, stated plainly: durable dedupe reaches across a restart
    exactly as far as the caller's scope does.
    """
    live.add_effect_tool(TOOL, {"stdout": "deployed"})
    scopes = iter(["turn:first", "turn:second"])
    monkeypatch.setattr("core.mesh_live.effect_epoch",
                        lambda turn, fallback: next(scopes))

    live.client.call_tool(TOOL, ARGS)
    live.client.say("Done.")
    ask(live, "run the synthetic deployment")
    live.client.call_tool(TOOL, ARGS)
    live.client.say("Done again.")
    r = ask(live, "run the synthetic deployment")

    assert r.effect_count(TOOL) == 2, (
        "two separate operator requests were fused into one effect")
    journal = DurableEffectJournal(live.journal_path)
    assert journal.status()["committed"] == 2


def test_the_same_scope_dedupes_across_a_new_executor(live):
    """And within one scope, a brand-new executor object — the in-process state
    a restart would lose — still resolves from the journal."""
    from tools.executor import ToolExecutor

    live.add_effect_tool(TOOL, {"stdout": "deployed"})
    live.client.call_tool(TOOL, ARGS)
    live.client.say("Done.")
    first = ask(live, "run the synthetic deployment")
    assert first.effect_count(TOOL) == 1

    fresh = ToolExecutor()
    fresh.begin_effect_epoch(PINNED_SCOPE)
    calls = {"n": 0}

    def _handler(**kwargs):
        calls["n"] += 1
        return {"stdout": "deployed"}

    fresh._tool_code_execute = _handler

    async def _granted(tool_name, preview):
        return True, "test:granted"

    fresh._challenge = _granted
    note: dict = {}
    result = asyncio.run(fresh.aexecute(TOOL, dict(ARGS), "r", effect_note=note))
    assert calls["n"] == 0, "a fresh executor repeated a committed effect"
    assert note["disposition"] == ExecutionDisposition.RECOVERED_COMMITTED.value
    assert result["status"] == "recovered"


# ══════════════════════════════════════════════════════════════════════════════
#  TEAM_RECOVERY — §45
# ══════════════════════════════════════════════════════════════════════════════
def test_the_team_inherits_durability_through_the_canonical_executor(tmp_path,
                                                                     monkeypatch):
    """§45 — no team-specific persistence logic exists, and that is the point.

    A grep of the team fabric for the journal must come back empty: the
    orchestrator reaches ToolExecutor.aexecute like everything else, so
    durability is a property of the path rather than a feature each caller has
    to remember to opt into.
    """
    import inspect

    from core import specialist_team

    source = inspect.getsource(specialist_team)
    for token in ("effect_journal", "DurableEffectJournal", "mark_executing",
                  "journal.reserve", "sqlite3"):
        assert token not in source, (
            f"the team fabric grew its own durability logic ({token})")


def test_two_specialists_converging_on_one_effect_share_the_durable_identity(
        tmp_path, monkeypatch):
    """Two specialists, one effect identity, one journal row, one effect.

    Driven through the real ToolExecutor rather than the scheduler, so nothing
    here can pass because a conflict scheduler happened to serialise it.
    """
    from tools.executor import ToolExecutor

    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL_PATH", str(tmp_path / "effects.db"))

    async def _no_broadcast(_payload):
        return None

    monkeypatch.setattr("tools.executor._aura_broadcast", _no_broadcast)
    executor = ToolExecutor()
    executor.begin_effect_epoch("task:team-shared")
    effects = {"n": 0}

    def _handler(**kwargs):
        effects["n"] += 1
        return {"stdout": "ok"}

    executor._tool_code_execute = _handler
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def _parked(tool_name, preview):
        entered.set()
        await asyncio.wait_for(gate.wait(), timeout=30)
        return True, "test:granted"

    executor._challenge = _parked
    notes = [{}, {}]

    async def scenario():
        a = asyncio.create_task(
            executor.aexecute(TOOL, dict(ARGS), "trace", effect_note=notes[0]))
        await asyncio.wait_for(entered.wait(), timeout=30)
        b = asyncio.create_task(
            executor.aexecute(TOOL, dict(ARGS), "oracle", effect_note=notes[1]))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(asyncio.gather(a, b), timeout=30)

    asyncio.run(scenario())
    assert effects["n"] == 1
    # Both specialists computed the SAME identity — that is why one of them was
    # suppressed rather than merely being slower.
    assert notes[0]["effect_key"] == notes[1]["effect_key"]
    assert notes[0]["disposition"] == ExecutionDisposition.EXECUTED_NOW.value
    assert notes[1]["disposition"] == \
        ExecutionDisposition.DEDUPLICATED_IN_PROCESS.value
    journal = DurableEffectJournal(tmp_path / "effects.db")
    assert journal.status()["total"] == 1, "one identity produced two rows"
    assert journal.status()["committed"] == 1
