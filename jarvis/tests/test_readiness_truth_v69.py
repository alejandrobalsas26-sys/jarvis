"""
tests/test_readiness_truth_v69.py — V69 M54.7 semantic health folded into readiness.

Locks symptom #6: when jarvis_episodic is REINDEX_REQUIRED the ONE boot snapshot
must report DEGRADED, must NOT say "Episodic memory online", and must NOT say "All
systems nominal". When semantic memory is healthy the prior nominal path is intact.
"""
from __future__ import annotations

from core.boot_state import assemble_boot_state, OK, DEGRADED


def _report(results, failed=0, optional_missing=0):
    return {"results": results, "failed": failed, "optional_missing": optional_missing}


def _sub(id_, status, name=None, detail=""):
    return {"id": id_, "name": name or id_, "status": status, "detail": detail}


# A self-test where every core subsystem passed (chromadb OK etc.).
_HEALTHY_CORE = _report([
    _sub("ollama", OK), _sub("chromadb", OK), _sub("correlator", OK),
])

# The live semantic summary: jarvis_episodic needs a reindex; vault active.
_REINDEX_SUMMARY = {
    "overall": "DEGRADED",
    "collections": [
        {"logical_name": "jarvis_episodic", "status": "REINDEX_REQUIRED",
         "active_physical": "jarvis_episodic", "records": 1200},
        {"logical_name": "jarvis_knowledge", "status": "ACTIVE",
         "active_physical": "jarvis_knowledge__abc", "records": 0},
    ],
}
_HEALTHY_SUMMARY = {
    "overall": "OK",
    "collections": [
        {"logical_name": "jarvis_episodic", "status": "ACTIVE"},
        {"logical_name": "jarvis_knowledge", "status": "ACTIVE"},
    ],
}


# ── REINDEX_REQUIRED degrades readiness ───────────────────────────────────────

def test_reindex_required_makes_health_degraded_not_nominal():
    st = assemble_boot_state(_HEALTHY_CORE, semantic_summary=_REINDEX_SUMMARY)
    assert st.semantic_degraded is True
    assert st.episodic_reindex_required is True
    assert st.health() == DEGRADED
    assert st.all_systems_nominal() is False


def test_reindex_narration_does_not_claim_episodic_online_or_nominal():
    st = assemble_boot_state(_HEALTHY_CORE, semantic_summary=_REINDEX_SUMMARY)
    lines = dict(st.narration_lines())
    assert "Episodic memory online." != lines["memory"]
    assert "requires migration" in lines["memory"].lower()
    # The ready line must not assert "All systems nominal".
    all_text = " ".join(m for _, m in st.narration_lines())
    assert "All systems nominal" not in all_text
    assert "degraded semantic memory" in lines["ready"].lower()


def test_reindex_ready_line_matches_allowed_wording():
    st = assemble_boot_state(_HEALTHY_CORE, semantic_summary=_REINDEX_SUMMARY)
    ready = dict(st.narration_lines())["ready"]
    assert "ready with degraded semantic memory" in ready.lower()
    assert "Knowledge Vault is active" in ready
    assert "Episodic memory requires migration" in ready


def test_knowledge_vault_active_detected():
    st = assemble_boot_state(_HEALTHY_CORE, semantic_summary=_REINDEX_SUMMARY)
    assert st.knowledge_vault_active is True


# ── Healthy semantic memory preserves the nominal path ────────────────────────

def test_healthy_semantic_allows_nominal():
    st = assemble_boot_state(_HEALTHY_CORE, semantic_summary=_HEALTHY_SUMMARY)
    assert st.semantic_degraded is False
    assert st.episodic_reindex_required is False
    assert st.all_systems_nominal() is True
    assert st.health() == OK
    assert "All systems nominal" in dict(st.narration_lines())["ready"]


def test_no_semantic_summary_is_backward_compatible():
    # Omitting the summary must not fabricate degradation (old callers).
    st = assemble_boot_state(_HEALTHY_CORE)
    assert st.semantic_degraded is False
    assert st.all_systems_nominal() is True


def test_malformed_summary_degrades_safely():
    st = assemble_boot_state(_HEALTHY_CORE, semantic_summary={"overall": "DEGRADED"})
    # overall DEGRADED with no parseable collections still degrades, honestly.
    assert st.semantic_degraded is True
    assert st.all_systems_nominal() is False


def test_to_dict_exposes_semantic_flags():
    d = assemble_boot_state(_HEALTHY_CORE, semantic_summary=_REINDEX_SUMMARY).to_dict()
    assert d["semantic_degraded"] is True
    assert d["episodic_reindex_required"] is True
    assert d["knowledge_vault_active"] is True
    assert d["all_systems_nominal"] is False
