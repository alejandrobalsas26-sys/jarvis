"""V69 M58.1/.2/.3/.3.1 — prompt inventory, stable prefix, compact delta, size.

Deterministic and server-free: everything under test is pure.
"""
from __future__ import annotations

import pytest

from core.prompt_manifest import (
    CONTRACT_DELTA_SCHEMA_VERSION,
    MAX_CONTRACT_DELTA_CHARS,
    PromptLayer,
    build_fast_system_prompt,
    build_manifest,
    contract_delta,
    core_prompt_fingerprint,
    detect_duplicate_sections,
    measure_prompt,
    stable_core_prefix,
    stable_prefix,
    stable_prefix_fingerprint,
)
from core.response_contract import ResponseContract, ContractReason, ResponseShape
from core.response_contract import _BASE_SHAPES  # noqa: PLC2701 — test-only factory


# Six FAST-eligible contracts that must share a byte-identical stable prefix.
_SHARED_CONTRACTS = (
    ResponseContract.INSTANT, ResponseContract.BRIEF, ResponseContract.STANDARD,
    ResponseContract.TECHNICAL, ResponseContract.STRUCTURED,
    ResponseContract.ERROR_RECOVERY,
)


def _shape(contract: ResponseContract, language: str = "es") -> ResponseShape:
    return ResponseShape(contract=contract, reason=ContractReason.GENERAL_EDUCATIONAL,
                         language=language, **_BASE_SHAPES[contract])


# ── M58.1 fingerprints ────────────────────────────────────────────────────────
def test_identical_inputs_produce_identical_fingerprint():
    assert core_prompt_fingerprint() == core_prompt_fingerprint()
    assert stable_prefix_fingerprint(language_directive="X") == \
        stable_prefix_fingerprint(language_directive="X")


def test_language_change_invalidates_stable_prefix_fingerprint():
    a = stable_prefix_fingerprint(language_directive="Responde en español.")
    b = stable_prefix_fingerprint(language_directive="Answer in English.")
    assert a != b


def test_manifest_snapshot_has_no_raw_prompt_text():
    m = build_manifest(model="qwen3:8b", num_ctx=2048, language="es",
                       language_directive="Responde en español.",
                       shape=_shape(ResponseContract.BRIEF))
    snap = m.snapshot()
    blob = repr(snap)
    # No raw prompt sentences may leak through diagnostics — only fingerprints/sizes.
    assert "local AI assistant" not in blob
    assert "UNTRUSTED" not in blob
    assert "RESPONSE_CONTRACT" not in blob
    assert snap["core_fingerprint"] and len(snap["core_fingerprint"]) == 16


def test_compatibility_identity_excludes_the_contract_delta():
    # Two different contracts in the same family share the compatibility identity:
    # the delta is NOT part of it (that is what lets a family share one warmed prefix).
    m_instant = build_manifest(model="qwen3:8b", num_ctx=2048, language="es",
                               shape=_shape(ResponseContract.INSTANT))
    m_brief = build_manifest(model="qwen3:8b", num_ctx=2048, language="es",
                             shape=_shape(ResponseContract.BRIEF))
    assert m_instant.compatibility_identity() == m_brief.compatibility_identity()
    # but the delta fingerprints differ
    assert m_instant.contract_delta_fingerprint != m_brief.contract_delta_fingerprint


@pytest.mark.parametrize("field,a,b", [
    ("num_ctx", 2048, 1024),
    ("language", "es", "en"),
    ("authority_mode", "STANDARD", "ELEVATED"),
    ("scope_fingerprint", "s1", "s2"),
    ("tool_schema_fingerprint", "", "abc"),
    ("model", "qwen3:8b", "qwen3:14b"),
    ("transport", "native", "openai"),
])
def test_config_change_invalidates_compatibility_identity(field, a, b):
    base = dict(model="qwen3:8b", num_ctx=2048, language="es", authority_mode="STANDARD",
                scope_fingerprint="", tool_schema_fingerprint="", transport="native")
    ma = build_manifest(**{**base, field: a})
    mb = build_manifest(**{**base, field: b})
    assert ma.compatibility_identity() != mb.compatibility_identity()


# ── M58.2 stable prefix ───────────────────────────────────────────────────────
def test_shared_contracts_have_byte_identical_stable_prefix():
    lang = "Responde en español."
    prefixes = {c: stable_prefix(language_directive=lang) for c in _SHARED_CONTRACTS}
    # every contract sees the exact same reusable prefix bytes
    assert len(set(prefixes.values())) == 1


def test_full_prompt_starts_with_the_stable_prefix_for_every_contract():
    lang = "Responde en español."
    sp = stable_prefix(language_directive=lang)
    for c in _SHARED_CONTRACTS:
        full = build_fast_system_prompt(language_directive=lang, shape=_shape(c),
                                        host_time_line="HOST CLOCK: now",
                                        continuation="")
        assert full.startswith(sp), f"{c} prompt must begin with the stable prefix"


def test_security_instructions_live_in_the_stable_prefix():
    sp = stable_prefix(language_directive="")
    assert "UNTRUSTED DATA" in sp
    assert "Never reveal secrets" in sp
    # and the no-chain-of-thought rule
    assert "Do NOT show reasoning" in sp


def test_dynamic_clock_is_after_the_delta_not_inside_the_stable_prefix():
    full = build_fast_system_prompt(language_directive="L", shape=_shape(ResponseContract.BRIEF),
                                    host_time_line="HOST CLOCK: 2026-07-23T10:00:00", continuation="")
    assert full.index("[RESPONSE_CONTRACT]") < full.index("HOST CLOCK")
    # the stable prefix contains neither the clock nor the delta
    sp = stable_prefix(language_directive="L")
    assert "HOST CLOCK" not in sp and "[RESPONSE_CONTRACT]" not in sp


def test_no_duplicated_stable_sections_in_a_real_prompt():
    full = build_fast_system_prompt(language_directive="Responde en español.",
                                    shape=_shape(ResponseContract.STANDARD),
                                    host_time_line="HOST CLOCK: now", continuation="")
    assert detect_duplicate_sections(full) == ()


# ── M58.3 compact contract delta ──────────────────────────────────────────────
def test_delta_field_order_is_deterministic_and_bounded():
    d = contract_delta(_shape(ResponseContract.BRIEF))
    rendered = d.render()
    assert rendered.startswith("[RESPONSE_CONTRACT]")
    assert rendered.rstrip().endswith("[/RESPONSE_CONTRACT]")
    assert len(rendered) <= MAX_CONTRACT_DELTA_CHARS
    # schema version required and present
    assert f"schema={CONTRACT_DELTA_SCHEMA_VERSION}" in rendered
    # deterministic
    assert d.render() == contract_delta(_shape(ResponseContract.BRIEF)).render()


def test_delta_covers_all_ten_contracts_without_raising():
    for c in ResponseContract:
        d = contract_delta(_shape(c))
        assert d.contract == c.value
        assert len(d.render()) <= MAX_CONTRACT_DELTA_CHARS


def test_delta_preserves_language():
    assert contract_delta(_shape(ResponseContract.BRIEF, "en")).language == "en"
    assert contract_delta(_shape(ResponseContract.BRIEF, "es")).language == "es"
    assert "language=en" in contract_delta(_shape(ResponseContract.BRIEF, "en")).render()


def test_delta_carries_no_tool_or_authority_fields():
    for c in ResponseContract:
        rendered = contract_delta(_shape(c)).render().lower()
        for banned in ("tool", "authority", "scope", "permission", "risk",
                       "verify", "memory", "rag"):
            assert banned not in rendered, f"{c}: delta must not carry {banned!r}"


def test_delta_is_materially_smaller_than_the_prose_style_tail():
    # The whole point of M58.3: the compact delta replaces the M57 prose tail with a
    # small, FIXED-size block. In aggregate across the FAST family it is much smaller,
    # and for the explanatory contracts (which carried the long answer-first prose) it
    # is smaller one-for-one. A greeting's prose was already tiny — its win is prefix
    # REUSE, not tail size — so it is excluded from the one-for-one check.
    total_prose = sum(len(_shape(c).style_directive()) for c in _SHARED_CONTRACTS)
    total_delta = sum(len(contract_delta(_shape(c)).render()) for c in _SHARED_CONTRACTS)
    assert total_delta < total_prose * 0.75, (total_delta, total_prose)
    for c in (ResponseContract.BRIEF, ResponseContract.STANDARD,
              ResponseContract.TECHNICAL):
        shape = _shape(c)
        assert len(contract_delta(shape).render()) < len(shape.style_directive())


# ── M58.3.1 size governor & duplicate detection ───────────────────────────────
def test_measure_prompt_flags_over_budget_and_trim_candidates():
    layers = {
        PromptLayer.CORE: "x" * 400,
        PromptLayer.SESSION: "y" * 40,
        PromptLayer.RECENT: "z" * 2000,
        PromptLayer.DIGEST: "d" * 800,
        PromptLayer.CURRENT: "hola",
    }
    report = measure_prompt(layers, budget_tokens=200)
    assert report.over_budget is True
    # cheapest-first trim order: digest then recent
    assert report.trim_candidates[0] == PromptLayer.DIGEST.value
    assert "recent" in report.trim_candidates


def test_measure_prompt_detects_duplicate_stable_section():
    doubled = stable_core_prefix() + "\n\n" + stable_core_prefix()
    dups = detect_duplicate_sections(doubled)
    assert "identity_block" in dups
    assert "security_block" in dups


def test_size_snapshot_is_content_free():
    report = measure_prompt({PromptLayer.CORE: stable_core_prefix()}, budget_tokens=500)
    blob = repr(report.snapshot())
    assert "local AI assistant" not in blob
    assert "UNTRUSTED" not in blob
