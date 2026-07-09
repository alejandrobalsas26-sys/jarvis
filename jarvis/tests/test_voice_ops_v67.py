"""tests/test_voice_ops_v67.py — V67 M33 typed operational voice control.

Proves the voice layer resolves speech ONLY to a fixed set of typed operational
intents and never turns voice into a world effect:
  * the documented utterances classify to the right intent + mode (READ-ONLY /
    DRY-RUN / REQUIRES_APPROVAL), including the "dry-run the recommended runbook" vs
    "execute the runbook" disambiguation;
  * READ-ONLY intents answer from the grounded M32 engine (honest empty state);
  * DRY-RUN plans the recommended runbook (status dry_run) and executes NOTHING;
  * a voice request to EXECUTE is refused and routed to HITL — the runbook engine's
    execute() is never called (a spy engine raises if it ever is);
  * process_for_voice_ops reuses the EXISTING tts.speak_async and never raises.

Pure: deterministic scenario spine + fakes; no STT/TTS hardware, no Ollama.
"""
from __future__ import annotations

import asyncio

import pytest

from core.ops_query import OperationalContext
from core.runbook_engine import RunbookEngine
from core.scenario_harness import SCENARIOS, ScenarioHarness
from core.situation_engine import SituationEngine
from core.voice_ops import (
    VoiceOpsIntent,
    VoiceOpsMode,
    classify_voice_ops,
    handle_voice_ops,
    process_for_voice_ops,
)


class _FakeTTS:
    def __init__(self):
        self.spoken: list[str] = []

    async def speak_async(self, text):
        self.spoken.append(text)


class _NoExecEngine:
    """A runbook engine that PLANS but raises if anyone tries to execute — proves
    voice never reaches a world effect."""
    def dry_run(self, name, params=None):
        return RunbookEngine().dry_run(name, params)

    async def execute(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("voice must never execute a runbook")


def _ctx(scenario_id="new_service_exposure"):
    out = ScenarioHarness().run(SCENARIOS[scenario_id])
    return OperationalContext(situation=out.situation, twin_snapshot=out.drift,
                              findings=out.findings, incidents=out.incidents, sensors={}), out


# ── classification ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("utterance,intent,mode", [
    ("Jarvis, system status.", VoiceOpsIntent.SYSTEM_STATUS, VoiceOpsMode.READ_ONLY),
    ("Jarvis, what changed?", VoiceOpsIntent.WHAT_CHANGED, VoiceOpsMode.READ_ONLY),
    ("Jarvis, summarize incidents.", VoiceOpsIntent.SUMMARIZE_INCIDENTS, VoiceOpsMode.READ_ONLY),
    ("Jarvis, what is uncertain?", VoiceOpsIntent.WHAT_UNCERTAIN, VoiceOpsMode.READ_ONLY),
    ("Jarvis, show unhealthy assets.", VoiceOpsIntent.UNHEALTHY_ASSETS, VoiceOpsMode.READ_ONLY),
    ("Jarvis, recommend a runbook.", VoiceOpsIntent.RECOMMEND_RUNBOOK, VoiceOpsMode.READ_ONLY),
    ("Jarvis, dry-run the recommended runbook.", VoiceOpsIntent.DRY_RUN_RUNBOOK,
     VoiceOpsMode.DRY_RUN),
    ("Jarvis, execute the runbook.", VoiceOpsIntent.EXECUTE_RUNBOOK,
     VoiceOpsMode.REQUIRES_APPROVAL),
    ("Jarvis, run the runbook.", VoiceOpsIntent.EXECUTE_RUNBOOK,
     VoiceOpsMode.REQUIRES_APPROVAL),
])
def test_classification(utterance, intent, mode):
    assert classify_voice_ops(utterance) == (intent, mode)


def test_wake_word_variants_stripped():
    for w in ("Jarvis, system status", "Hey Jarvis system status",
              "okay jarvis, system status", "system status"):
        assert classify_voice_ops(w)[0] is VoiceOpsIntent.SYSTEM_STATUS


def test_non_operational_utterance_falls_through():
    assert handle_voice_ops("Jarvis, tell me a joke.", context=OperationalContext()) is None


# ── read-only intents are grounded, never effect the world ────────────────────
class TestReadOnly:
    def test_status_is_grounded_no_world_effect(self):
        ctx, _ = _ctx()
        r = handle_voice_ops("Jarvis, system status.", context=ctx)
        assert r.mode is VoiceOpsMode.READ_ONLY
        assert r.executed_world_effect is False
        assert r.data["grounded"] is True
        assert r.spoken

    def test_empty_state_is_honest(self):
        ctx = OperationalContext(situation=SituationEngine().build())
        r = handle_voice_ops("Jarvis, summarize incidents.", context=ctx)
        assert "secure" not in r.spoken.lower()
        assert r.data["empty"] is True


# ── dry-run plans only ────────────────────────────────────────────────────────
class TestDryRun:
    def test_dry_run_plans_recommended_runbook_without_executing(self):
        ctx, _ = _ctx()
        spy = _NoExecEngine()
        r = handle_voice_ops("Jarvis, dry-run the recommended runbook.",
                             context=ctx, runbook_engine=spy)
        assert r.mode is VoiceOpsMode.DRY_RUN
        assert r.executed_world_effect is False
        assert r.data["status"] == "dry_run"
        assert r.runbook == "NEW_SERVICE_EXPOSURE_REVIEW"
        assert "nothing was executed" in r.spoken.lower()

    def test_dry_run_with_no_recommendation_is_graceful(self):
        ctx = OperationalContext(situation=SituationEngine().build())
        r = handle_voice_ops("Jarvis, dry-run the recommended runbook.",
                             context=ctx, runbook_engine=_NoExecEngine())
        assert r.mode is VoiceOpsMode.DRY_RUN
        assert "no recommended runbook" in r.spoken.lower()


# ── execute requests are refused, never run ───────────────────────────────────
class TestExecuteRefused:
    def test_execute_requires_approval_and_runs_nothing(self):
        ctx, _ = _ctx()
        spy = _NoExecEngine()   # .execute() raises if ever called
        r = handle_voice_ops("Jarvis, execute the runbook.", context=ctx,
                             runbook_engine=spy)
        assert r.mode is VoiceOpsMode.REQUIRES_APPROVAL
        assert r.requires_approval is True
        assert r.executed_world_effect is False
        assert "human-in-the-loop" in r.spoken.lower()

    def test_no_intent_ever_reports_a_world_effect(self):
        ctx, _ = _ctx()
        for utt in ("Jarvis, system status.", "Jarvis, dry-run the recommended runbook.",
                    "Jarvis, execute the runbook.", "Jarvis, recommend a runbook."):
            r = handle_voice_ops(utt, context=ctx, runbook_engine=_NoExecEngine())
            assert r.executed_world_effect is False


# ── pipeline entry reuses existing TTS, never raises ──────────────────────────
class TestPipelineEntry:
    def test_operational_utterance_is_spoken_and_handled(self):
        ctx, _ = _ctx()
        tts = _FakeTTS()
        handled = asyncio.run(process_for_voice_ops(
            "Jarvis, system status.", None, tts, context=ctx))
        assert handled is True
        assert len(tts.spoken) == 1 and tts.spoken[0]

    def test_non_operational_falls_through_without_speaking(self):
        tts = _FakeTTS()
        handled = asyncio.run(process_for_voice_ops(
            "Jarvis, tell me a joke.", None, tts, context=OperationalContext()))
        assert handled is False
        assert tts.spoken == []
