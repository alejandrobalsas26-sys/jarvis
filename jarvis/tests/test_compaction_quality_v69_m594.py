"""V69 M59.4 — governor-integrated compaction & deterministic quality gates.

Proves the compaction proposer runs under the real residency governor at the dedicated
BACKGROUND_COMPACTION priority (below active FAST), and that the deterministic quality
gate rejects invented entities, secrets, code, quotation and duplicates while a model
can never mint EXPLICIT — never writing semantic memory.
"""
from __future__ import annotations

import asyncio

from core.compaction_quality import (
    CompactionQualityGate,
    QualityState,
    RejectReason,
)
from core.compaction_scheduler import CompactionConditions, CompactionScheduler
from core.conversation_digest import (
    ConversationDigest,
    DigestItem,
    Evidence,
    ItemKind,
    build_digest,
)
from core.residency_governor import Priority, ResidencyGovernor


def _run(coro):
    return asyncio.run(coro)


# ── governor priority ─────────────────────────────────────────────────────────
def test_background_compaction_priority_sits_between_background_and_prewarm():
    assert int(Priority.BACKGROUND) < int(Priority.BACKGROUND_COMPACTION)
    assert int(Priority.BACKGROUND_COMPACTION) < int(Priority.PREWARM)
    # Active FAST always outranks compaction.
    assert int(Priority.INTERACTIVE) < int(Priority.BACKGROUND_COMPACTION)


# ── source texts for a synthetic conversation ─────────────────────────────────
_SOURCE = [
    {"role": "user", "content": "quiero configurar kerberos y decidimos usar el "
                                "transporte nativo de ollama para el modo rapido"},
    {"role": "assistant", "content": "de acuerdo, kerberos con transporte nativo; "
                                     "queda pendiente revisar el contexto"},
]


def _gate():
    return CompactionQualityGate()


def _base():
    return build_digest(_SOURCE)


# ── quality gate: acceptance & rejection ──────────────────────────────────────
def test_gate_accepts_source_linked_items():
    items = [DigestItem(ItemKind.TOPIC, "kerberos transporte nativo", Evidence.OBSERVED),
             DigestItem(ItemKind.OPEN_QUESTION, "revisar el contexto", Evidence.OBSERVED)]
    accepted, m = _gate().evaluate(items, base_digest=_base(),
                                   source_texts=[s["content"] for s in _SOURCE])
    assert len(accepted) == 2
    assert m.quality_state == QualityState.PASS.value


def test_gate_rejects_model_minting_explicit():
    items = [DigestItem(ItemKind.DECISION, "kerberos nativo", Evidence.EXPLICIT)]
    accepted, m = _gate().evaluate(items, base_digest=_base(),
                                   source_texts=[s["content"] for s in _SOURCE])
    assert accepted == []
    assert m.rejection_reasons.get(RejectReason.MINTS_EXPLICIT.value) == 1


def test_gate_rejects_invented_entity():
    items = [DigestItem(ItemKind.TOPIC, "migracion a Kubernetes", Evidence.OBSERVED)]
    accepted, m = _gate().evaluate(items, base_digest=_base(),
                                   source_texts=[s["content"] for s in _SOURCE])
    assert accepted == []
    assert m.rejection_reasons.get(RejectReason.INVENTED_ENTITY.value) == 1


def test_gate_rejects_secret_like():
    items = [DigestItem(ItemKind.DECISION, "usar la clave sk-abcd1234efgh5678ijkl",
                        Evidence.OBSERVED)]
    accepted, m = _gate().evaluate(items, base_digest=_base(),
                                   source_texts=[s["content"] for s in _SOURCE])
    assert accepted == []
    assert m.rejection_reasons.get(RejectReason.SECRET_LIKE.value) == 1


def test_gate_rejects_code_block():
    items = [DigestItem(ItemKind.TOPIC, "def exploit(): return None", Evidence.OBSERVED)]
    accepted, m = _gate().evaluate(items, base_digest=_base(),
                                   source_texts=[s["content"] for s in _SOURCE])
    assert accepted == []
    assert m.rejection_reasons.get(RejectReason.RAW_CODE_BLOCK.value) == 1


def test_gate_rejects_bad_kind():
    items = [DigestItem(ItemKind.PREFERENCE, "kerberos nativo", Evidence.OBSERVED)]
    accepted, m = _gate().evaluate(items, base_digest=_base(),
                                   source_texts=[s["content"] for s in _SOURCE])
    assert accepted == []
    assert m.rejection_reasons.get(RejectReason.BAD_KIND.value) == 1


def test_gate_suppresses_duplicates():
    items = [DigestItem(ItemKind.TOPIC, "kerberos nativo", Evidence.OBSERVED),
             DigestItem(ItemKind.TOPIC, "kerberos nativo", Evidence.OBSERVED)]
    accepted, m = _gate().evaluate(items, base_digest=_base(),
                                   source_texts=[s["content"] for s in _SOURCE])
    assert len(accepted) == 1
    assert m.duplicate_suppressions == 1


def test_gate_rejects_too_long():
    items = [DigestItem(ItemKind.TOPIC, "kerberos " * 30, Evidence.OBSERVED)]
    accepted, m = _gate().evaluate(items, base_digest=_base(),
                                   source_texts=[s["content"] for s in _SOURCE])
    assert accepted == []
    assert m.rejection_reasons.get(RejectReason.TOO_LONG.value) == 1


def test_gate_metrics_are_content_free():
    items = [DigestItem(ItemKind.TOPIC, "kerberos nativo", Evidence.OBSERVED)]
    _, m = _gate().evaluate(items, base_digest=_base(),
                            source_texts=[s["content"] for s in _SOURCE])
    snap = m.snapshot()
    for value in snap.values():
        assert "kerberos" not in str(value).lower()


# ── scheduler under governor ──────────────────────────────────────────────────
def _long_history(n=20):
    h = list(_SOURCE)
    for i in range(n):
        h.append({"role": "user", "content": f"kerberos transporte nativo contexto {i} "
                                              "con bastante texto para pesar tokens"})
        h.append({"role": "assistant", "content": f"kerberos nativo detalle {i} " * 6})
    return h


def _eligible(**over):
    base = dict(completed_turns=12, active_user_turn=False, hitl_active=False,
                effectful_tool_active=False, answer_tts_active=False,
                high_priority_embedding=False, lifecycle_operational=True,
                power_allows_background=True, context_pressure=0.9,
                cooldown_expired=True)
    base.update(over)
    return CompactionConditions(**base)


async def _good_proposer(history, timeout):
    return [DigestItem(ItemKind.TOPIC, "kerberos transporte nativo", Evidence.OBSERVED),
            DigestItem(ItemKind.OPEN_QUESTION, "revisar el contexto", Evidence.OBSERVED)]


def test_scheduler_acquires_governor_slot():
    gov = ResidencyGovernor()
    sched = CompactionScheduler(proposer=_good_proposer, governor=gov,
                                quality_gate=_gate())
    st = _run(sched.maybe_run(_long_history(), _eligible()))
    assert st.value == "COMPLETED"
    snap = sched.snapshot()
    assert snap["governor_wait_ms"] is not None
    assert snap["accepted"] >= 1
    assert snap["quality_state"] in (QualityState.PASS.value, QualityState.DEGRADED.value)


def test_scheduler_rejects_invented_proposal_falls_back_to_base():
    async def bad_proposer(history, timeout):
        return [DigestItem(ItemKind.TOPIC, "planeta Marte colonizado", Evidence.OBSERVED)]

    gov = ResidencyGovernor()
    sched = CompactionScheduler(proposer=bad_proposer, governor=gov, quality_gate=_gate())
    st = _run(sched.maybe_run(_long_history(), _eligible()))
    # All invented → validation failed, but the extractive base digest survives.
    assert st.value == "VALIDATION_FAILED"
    assert sched.current_digest() is not None
    assert sched.snapshot()["rejected"] >= 1


def test_active_fast_holds_slot_and_compaction_waits():
    async def scenario():
        gov = ResidencyGovernor()
        # Occupy the single heavy slot with an active FAST turn.
        fast = gov.slot(role="fast", priority=Priority.INTERACTIVE)
        await fast.__aenter__()
        acquired = {"v": False}

        async def proposer(history, timeout):
            acquired["v"] = True
            return await _good_proposer(history, timeout)

        sched = CompactionScheduler(proposer=proposer, governor=gov,
                                    quality_gate=_gate(), slot_timeout_s=5.0)
        task = asyncio.ensure_future(sched.maybe_run(_long_history(), _eligible()))
        await asyncio.sleep(0.05)
        # FAST still holds the slot → the proposer has NOT run yet.
        assert acquired["v"] is False
        await fast.__aexit__(None, None, None)
        st = await task
        assert acquired["v"] is True
        assert st.value == "COMPLETED"

    _run(scenario())


def test_preempt_releases_and_preserves_previous_digest():
    async def scenario():
        gov = ResidencyGovernor()
        # First, produce a valid digest.
        s1 = CompactionScheduler(proposer=_good_proposer, governor=gov,
                                 quality_gate=_gate())
        await s1.maybe_run(_long_history(), _eligible())
        assert s1.current_digest() is not None

        started = asyncio.Event()

        async def slow(history, timeout):
            started.set()
            await asyncio.sleep(10.0)
            return []

        s1.proposer = slow
        task = asyncio.ensure_future(s1.maybe_run(_long_history(), _eligible(),))
        await started.wait()
        s1.preempt()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # The previous valid digest survives; nothing partial exposed; slot released.
        assert s1.preemptions == 1
        assert s1.current_digest() is not None
        assert gov.queue_depth == 0
        assert gov.active_roles == ()

    _run(scenario())


def test_quality_soak_mixed_adversarial_batch():
    # A single batch mixing legitimate items with every adversarial class: only the
    # source-linked, safe, non-duplicate items survive, and the batch stays bounded.
    src = [s["content"] for s in _SOURCE]
    batch = [
        DigestItem(ItemKind.TOPIC, "kerberos transporte nativo", Evidence.OBSERVED),   # ok
        DigestItem(ItemKind.TOPIC, "kerberos transporte nativo", Evidence.OBSERVED),   # dup
        DigestItem(ItemKind.DECISION, "usar sk-abcd1234efgh5678xy", Evidence.OBSERVED),  # secret
        DigestItem(ItemKind.TOPIC, "def hack(): pass", Evidence.OBSERVED),             # code
        DigestItem(ItemKind.TOPIC, "invadir Marte con Elon", Evidence.OBSERVED),       # invented
        DigestItem(ItemKind.DECISION, "kerberos nativo", Evidence.EXPLICIT),           # mint explicit
        DigestItem(ItemKind.OPEN_QUESTION, "revisar el contexto", Evidence.OBSERVED),  # ok
    ]
    accepted, m = _gate().evaluate(batch, base_digest=_base(), source_texts=src)
    accepted_texts = {a.text for a in accepted}
    assert accepted_texts == {"kerberos transporte nativo", "revisar el contexto"}
    assert m.duplicate_suppressions == 1
    assert m.quality_state == QualityState.DEGRADED.value
    # No accepted item carries EXPLICIT — a model can never mint it here.
    assert all(a.evidence is not Evidence.EXPLICIT for a in accepted)
    assert len(accepted) <= 6


def test_no_semantic_memory_write(monkeypatch):
    # The scheduler must never call into semantic memory. Guard by ensuring no digest
    # object exposes a persistence hook and the digest stays an in-memory object.
    gov = ResidencyGovernor()
    sched = CompactionScheduler(proposer=_good_proposer, governor=gov,
                                quality_gate=_gate())
    _run(sched.maybe_run(_long_history(), _eligible()))
    digest = sched.current_digest()
    assert isinstance(digest, ConversationDigest)
    assert not hasattr(digest, "persist")
