"""tests/test_cognitive_synthesis_v68.py — V68 M40 evidence-grounded synthesis.

Proves the grounding validator (deterministic, no LLM) and the fallback contract:
  * a faithful narrative built only from the facts passes grounding with real coverage;
  * invented specifics (an IP / incident id / CVE not in the facts) FAIL grounding -
    no invented facts survive;
  * absolute-safety language ("all secure") FAILS - unknown is not safe;
  * causal overreach ("proves", "caused by") FAILS unless hedged - correlation != proof,
    hypothesis != fact;
  * synthesize() returns the LLM text ONLY when it is grounded; an ungrounded model
    output is discarded and the deterministic grounded answer is returned, marked
    source=degraded (the model never overrides the evidence);
  * a model timeout / crash degrades to the deterministic answer, never an error.

The synthesizer is injected; the network is never touched.
"""
from __future__ import annotations

import asyncio

from core.cognitive_synthesis import (
    GroundedSynthesis,
    synthesize,
    validate_grounding,
)

FACTS = [
    "situation severity: high",
    "incident inc-42: brute force on host web-1",
    "finding f_ab12: rule ssh_bruteforce confidence 0.7",
    "asset web-1 port 22 exposure external",
]


def _run(coro):
    return asyncio.run(coro)


class _Bundle:
    """Minimal stand-in for an ops_query FactBundle (has to_grounding + answer)."""
    def __init__(self, facts, answer):
        self._facts = facts
        self.answer = answer

    def to_grounding(self):
        return {"instruction": "Answer using ONLY the facts.", "question": "what is happening?",
                "facts": self._facts, "sources": ["situation_engine"]}


# ── deterministic validator ───────────────────────────────────────────────────
class TestValidator:
    def test_faithful_answer_is_grounded(self):
        text = ("Situation severity is high. Incident inc-42 reports a brute force on host "
                "web-1, matching finding f_ab12 (rule ssh_bruteforce). Asset web-1 exposes "
                "port 22 externally.")
        rep = validate_grounding(text, FACTS)
        assert rep.grounded is True
        assert rep.coverage > 0.5

    def test_invented_ip_fails(self):
        rep = validate_grounding("The attacker at 203.0.113.9 hit host web-1.", FACTS)
        assert rep.grounded is False
        assert "203.0.113.9" in rep.invented_specifics

    def test_invented_incident_id_fails(self):
        rep = validate_grounding("See incident inc-999 for the full chain.", FACTS)
        assert rep.grounded is False
        assert any("inc-999" in s for s in rep.invented_specifics)

    def test_absolute_safety_fails(self):
        rep = validate_grounding("No threats found; everything is secure.", FACTS)
        assert rep.grounded is False
        assert rep.certainty_violations

    def test_causal_overreach_fails_unless_hedged(self):
        bad = validate_grounding("This proves the brute force caused by the attack.", FACTS)
        assert bad.grounded is False and bad.causal_overreach
        hedged = validate_grounding(
            "The brute force may be correlated with the finding; not confirmed.", FACTS)
        assert hedged.grounded is True

    def test_empty_facts_yields_zero_coverage(self):
        assert validate_grounding("anything", []).coverage == 0.0


# ── synthesis fallback contract ────────────────────────────────────────────────
class TestSynthesizeContract:
    def test_grounded_llm_output_is_used(self):
        async def good(_prompt):
            return "Severity is high; incident inc-42 on host web-1 (finding f_ab12)."
        b = _Bundle(FACTS, "DET-ANSWER")
        out = _run(synthesize(b, synthesizer=good))
        assert isinstance(out, GroundedSynthesis)
        assert out.grounded is True and out.source == "llm"

    def test_ungrounded_output_is_discarded_for_deterministic(self):
        async def hallucinate(_prompt):
            return "The host 10.9.9.9 is fully secure and the attack is proven."
        b = _Bundle(FACTS, "DET-ANSWER")
        out = _run(synthesize(b, synthesizer=hallucinate))
        assert out.grounded is False
        assert out.source == "degraded"
        assert out.text == "DET-ANSWER"      # evidence-backed answer wins, not the model

    def test_timeout_degrades_to_deterministic(self):
        async def hang(_prompt):
            await asyncio.sleep(5)
            return "never"
        b = _Bundle(FACTS, "DET-ANSWER")
        out = _run(synthesize(b, synthesizer=hang, timeout_s=0.05))
        assert out.source == "deterministic" and out.text == "DET-ANSWER"

    def test_crash_degrades_to_deterministic(self):
        async def boom(_prompt):
            raise RuntimeError("model down")
        b = _Bundle(FACTS, "DET-ANSWER")
        out = _run(synthesize(b, synthesizer=boom))
        assert out.source == "deterministic" and out.grounded is True

    def test_output_is_ascii_dict(self):
        async def good(_prompt):
            return "Severity high on host web-1 incident inc-42."
        out = _run(synthesize(_Bundle(FACTS, "DET"), synthesizer=good))
        assert str(out.to_dict()).isascii()
