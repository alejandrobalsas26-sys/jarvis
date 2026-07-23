"""tests/test_generation_budget_v69_m572.py — V69 M57.2/M57.8.1: generation budgets.

Proves the contract -> native-Ollama option mapping:

  * every contract produces a BOUNDED num_predict inside its own window and inside
    the operator ceiling — there is no unlimited generation anywhere;
  * the live FAST num_ctx equals the PREWARM num_ctx (the M56.4 invariant), which
    ``core.llm._adaptive_ctx`` was silently breaking;
  * measured throughput moves the budget conservatively and only with enough
    samples; one abnormal run cannot distort policy;
  * a battery profile reduces nonessential generation but never below the floor;
  * hitting the token cap is reported TRUTHFULLY, never as a completed answer.

Pure — no model, no network.
"""
from __future__ import annotations

from core.config import Settings
from core.generation_budget import (
    GenerationBudget,
    ThroughputTracker,
    budget_for_shape,
    hit_generation_cap,
    resolve_live_fast_context,
    truncation_note,
)
from core.model_router import ModelDecision, ModelRole
from core.response_contract import HARD_MAX_OUTPUT_TOKENS, select_contract
from core.runtime_profile import RuntimeProfile, policy_for
from core.turn_policy import classify_request


def _md() -> ModelDecision:
    return ModelDecision(role=ModelRole.FAST, provider="ollama", model="m",
                         complexity=0.1, reason="t", requires_verification=False)


def _shape(msg: str, **kw):
    return select_contract(msg, turn_policy=classify_request(msg),
                           model_decision=_md(), **kw)


def _settings(**over) -> Settings:
    base = dict(fast_context=2048, fast_max_tokens=256, fast_keep_alive="10m",
                response_max_output_tokens=512)
    base.update(over)
    return Settings(**base)


# ── bounded token ranges ──────────────────────────────────────────────────────
def test_instant_budget_is_tiny():
    b = budget_for_shape(_shape("hola"), settings=_settings())
    assert 24 <= b.num_predict <= 64
    assert b.contract == "INSTANT"


def test_brief_budget_matches_the_measured_latency_target():
    b = budget_for_shape(_shape("como saco la raiz cuadrada de algo"),
                         settings=_settings())
    assert 64 <= b.num_predict <= 128


def test_technical_budget_is_larger_but_still_bounded():
    b = budget_for_shape(_shape("explica Kerberos con mas detalle"),
                         settings=_settings())
    assert 160 <= b.num_predict <= 384


def test_no_contract_can_exceed_the_hard_ceiling():
    for msg in ("hola", "explica Kerberos con mas detalle",
                "escribeme una funcion en python", "cuales son los tipos de datos",
                "segun mi PDF que dice", "system status"):
        b = budget_for_shape(_shape(msg), settings=_settings())
        assert 0 < b.num_predict <= HARD_MAX_OUTPUT_TOKENS
        assert b.num_predict <= 512


def test_operator_ceiling_clamps_every_contract():
    b = budget_for_shape(_shape("explica Kerberos con mas detalle"),
                         settings=_settings(response_max_output_tokens=64))
    assert b.num_predict <= 64


def test_operator_ceiling_cannot_be_set_unbounded():
    s = _settings(response_max_output_tokens=999999)
    assert s.response_max_output_tokens == 1024
    b = budget_for_shape(_shape("hola"), settings=s)
    assert b.num_predict <= 64


def test_explicit_detail_increases_the_budget_within_the_ceiling():
    plain = budget_for_shape(_shape("explicame Kerberos"), settings=_settings())
    detailed = budget_for_shape(_shape("explica Kerberos con mas detalle"),
                                settings=_settings())
    assert detailed.num_predict > plain.num_predict
    assert detailed.num_predict <= 512


# ── the M56.4 num_ctx invariant ───────────────────────────────────────────────
def test_live_fast_context_equals_prewarm_context():
    from core.fast_prewarm import resolve_fast_context
    assert resolve_live_fast_context() == resolve_fast_context()


def test_budget_num_ctx_is_the_configured_fast_context_not_a_shrunken_one():
    s = _settings(fast_context=2048)
    for msg in ("hola", "explica Kerberos con mas detalle"):
        b = budget_for_shape(_shape(msg), settings=s)
        assert b.num_ctx == 2048, "a short turn must NOT shrink the warmed runner ctx"


def test_budget_num_ctx_follows_an_operator_context_change():
    b = budget_for_shape(_shape("hola"), settings=_settings(fast_context=4096))
    assert b.num_ctx == 4096


# ── throughput adaptation (M57.8.1) ───────────────────────────────────────────
def test_no_adaptation_without_enough_samples():
    t = ThroughputTracker()
    t.record(tokens_per_second=6.0)
    b = budget_for_shape(_shape("como saco la raiz cuadrada"), settings=_settings(),
                         throughput=t)
    assert b.adjustment_reason == "contract_base"
    assert b.num_predict == b.base_num_predict


def test_slow_host_reduces_the_budget():
    t = ThroughputTracker()
    for _ in range(5):
        t.record(tokens_per_second=3.0)
    shape = _shape("como saco la raiz cuadrada")
    b = budget_for_shape(shape, settings=_settings(), throughput=t)
    assert b.throughput_basis == 3.0
    assert b.num_predict <= shape.base_output_tokens
    assert b.num_predict >= shape.min_output_tokens


def test_fast_host_raises_the_budget_but_only_within_the_window():
    t = ThroughputTracker()
    for _ in range(5):
        t.record(tokens_per_second=40.0)
    shape = _shape("como saco la raiz cuadrada")
    b = budget_for_shape(shape, settings=_settings(), throughput=t)
    assert b.num_predict <= shape.max_output_tokens
    assert b.num_predict <= int(shape.base_output_tokens * 1.5)


def test_one_abnormal_run_cannot_distort_policy():
    t = ThroughputTracker()
    for _ in range(6):
        t.record(tokens_per_second=6.0)
    t.record(tokens_per_second=59.0)          # a single freak measurement
    assert t.estimate_tok_s() == 6.0          # median is unmoved


def test_implausible_samples_are_rejected_not_clamped():
    t = ThroughputTracker()
    t.record(tokens_per_second=0.0)
    t.record(tokens_per_second=5000.0)
    t.record(tokens_per_second=None)
    assert t.samples == 0
    assert t.rejected == 2
    assert t.estimate_tok_s() is None


def test_tracker_ring_is_bounded():
    t = ThroughputTracker(maxlen=5)
    for i in range(50):
        t.record(tokens_per_second=5.0 + (i % 3))
    assert t.samples == 5
    assert t.snapshot()["samples"] == 5


def test_remaining_turn_time_can_only_shrink_the_budget():
    t = ThroughputTracker()
    for _ in range(5):
        t.record(tokens_per_second=6.0)
    shape = _shape("explica Kerberos con mas detalle")
    generous = budget_for_shape(shape, settings=_settings(), throughput=t,
                                remaining_s=300.0)
    tight = budget_for_shape(shape, settings=_settings(), throughput=t,
                             remaining_s=12.0)
    assert tight.num_predict < generous.num_predict
    assert tight.adjustment_reason == "remaining_turn_time"
    assert tight.num_predict >= shape.min_output_tokens


# ── battery ───────────────────────────────────────────────────────────────────
def test_battery_profile_reduces_generation():
    ac = budget_for_shape(
        _shape("explica Kerberos con mas detalle",
               power_policy=policy_for(RuntimeProfile.AC_PERFORMANCE)),
        settings=_settings())
    bat = budget_for_shape(
        _shape("explica Kerberos con mas detalle",
               power_policy=policy_for(RuntimeProfile.BATTERY_SAVER)),
        settings=_settings())
    assert bat.num_predict < ac.num_predict


# ── sampling posture ──────────────────────────────────────────────────────────
def test_sampling_options_are_bounded_and_code_has_the_lowest_repeat_penalty():
    code = budget_for_shape(_shape("escribeme una funcion en python"),
                            settings=_settings())
    tech = budget_for_shape(_shape("explica Kerberos con mas detalle"),
                            settings=_settings())
    assert code.repeat_penalty < tech.repeat_penalty
    for b in (code, tech):
        assert 0.0 <= b.temperature <= 1.0
        assert 0.5 <= b.top_p <= 1.0
        assert 1.0 <= b.repeat_penalty <= 1.5
        assert set(b.options()) == {"top_p", "repeat_penalty"}


def test_grounded_and_operational_contracts_use_low_temperature():
    for msg in ("segun mi PDF que dice del capitulo 3", "system status"):
        b = budget_for_shape(_shape(msg), settings=_settings())
        assert b.temperature <= 0.25


# ── truncation truth ──────────────────────────────────────────────────────────
def test_generation_cap_detected_from_done_reason():
    assert hit_generation_cap("length", None, 96) is True
    assert hit_generation_cap("stop", 40, 96) is False


def test_generation_cap_detected_from_eval_count():
    assert hit_generation_cap(None, 96, 96) is True
    assert hit_generation_cap(None, 95, 96) is False
    assert hit_generation_cap(None, None, 96) is False


def test_truncation_note_is_localized_and_invites_continuation():
    assert "continúa" in truncation_note("es")
    assert "continue" in truncation_note("en")
    assert truncation_note(None) == truncation_note("es")


# ── telemetry ─────────────────────────────────────────────────────────────────
def test_budget_telemetry_is_bounded_and_content_free():
    b = budget_for_shape(_shape("segun mi PDF, el procedimiento secreto"),
                         settings=_settings())
    tel = b.telemetry()
    blob = " ".join(str(v) for v in tel.values()).lower()
    assert "pdf" not in blob and "secreto" not in blob
    assert tel["token_budget"] == b.num_predict
    assert tel["context_budget"] == b.num_ctx
    assert isinstance(b, GenerationBudget)


def test_deterministic_bypass_path_spends_no_generation():
    # The bypass answers BEFORE any budget is built; assert the contract system
    # cannot be the thing that spends tokens on a time question.
    from core.deterministic_bypass import maybe_bypass
    answer = maybe_bypass("que hora es", language="es")
    assert answer, "the time question must still be answered without a model"
    assert isinstance(answer, str)
