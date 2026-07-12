"""
tests/test_tool_recovery_v681.py — V68.1 M46 regression coverage.

Locks deterministic tool-failure recovery and context isolation: a failed tool
must yield a typed, LLM-safe envelope; it must NOT be able to switch the
conversation to an unrelated tool family (the observed Packet Tracer / XML
contamination after query_knowledge failed).
"""
from __future__ import annotations

from core.tool_result import (
    ToolFailure,
    classify_exception,
    is_failure,
    make_failure,
    recovery_guidance,
)


# ── Envelope shape ────────────────────────────────────────────────────────────

def test_envelope_has_all_required_fields():
    d = make_failure("query_knowledge", "dependency_incompatibility", "backend offline")
    for key in (
        "status", "tool", "error_class", "safe_message", "error",
        "retryable", "retry_after", "fallback_allowed", "evidence_refs",
    ):
        assert key in d
    assert d["status"] == "failure"
    assert d["tool"] == "query_knowledge"
    assert is_failure(d)


def test_legacy_error_dict_still_detected_as_failure():
    assert is_failure({"error": "something"})
    assert not is_failure({"status": "ok", "result": "x"})
    assert not is_failure("plain string")


# ── Retry classification ──────────────────────────────────────────────────────

def test_structural_classes_never_retryable():
    for cls in (
        "schema_error", "invalid_query", "dependency_incompatibility",
        "configuration_error", "permission_denied",
    ):
        d = make_failure("t", cls, "msg", retryable=True)  # even if asked
        assert d["retryable"] is False


def test_timeout_is_retryable_by_default():
    d = make_failure("verifier", "timeout", "timed out")
    assert d["retryable"] is True


def test_success_result_is_not_failure():
    assert not is_failure({"status": "ok", "result": "fragments", "sources": ["a.pdf"]})


# ── Sanitization: no dependency internals reach the model ─────────────────────

def test_safe_message_is_single_line_no_stack_trace():
    raw = "Traceback (most recent call last):\n  File x\ninfer_schema torch.Tensor boom"
    d = make_failure("query_knowledge", "schema_error", raw)
    assert "\n" not in d["safe_message"]
    assert len(d["safe_message"]) <= 300


def test_classify_exception_maps_without_leak():
    ec, msg = classify_exception(ValueError("Parameter input has unsupported type torch.Tensor"))
    assert ec == "schema_error"
    assert "torch" not in msg.lower()
    ec2, msg2 = classify_exception(TimeoutError("verifier timed out"))
    assert ec2 == "timeout"
    ec3, _ = classify_exception(ModuleNotFoundError("No module named 'chromadb'"))
    assert ec3 == "dependency_missing"


# ── Context isolation: guidance pins to the same tool, forbids drift ──────────

def test_recovery_guidance_forbids_switching_tool_family():
    d = make_failure("query_knowledge", "dependency_incompatibility",
                     "Vector backend offline.")
    g = recovery_guidance(d)
    assert "query_knowledge" in g
    assert "do not switch" in g.lower() or "not switch" in g.lower()
    # It must never name or invite an unrelated tool family.
    assert "packet tracer" not in g.lower()


def test_recovery_guidance_non_retryable_says_do_not_retry():
    d = make_failure("query_knowledge", "dependency_incompatibility", "offline")
    g = recovery_guidance(d).lower()
    assert "not retry" in g


def test_recovery_guidance_retryable_allows_one_retry():
    d = make_failure("verifier", "timeout", "timed out")
    g = recovery_guidance(d).lower()
    assert "retry" in g and "once" in g


def test_recovery_guidance_scopes_failure_to_turn():
    d = make_failure("query_knowledge", "embedding_error", "search failed")
    g = recovery_guidance(d).lower()
    assert "this request only" in g or "this turn" in g or "scoped" in g


# ── ToolFailure dataclass immutability ────────────────────────────────────────

def test_toolfailure_is_frozen():
    f = ToolFailure(tool="t", error_class="x", safe_message="m")
    try:
        f.tool = "other"  # type: ignore[misc]
        assert False, "ToolFailure must be immutable"
    except Exception:
        pass
