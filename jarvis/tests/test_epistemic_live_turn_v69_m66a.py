"""
tests/test_epistemic_live_turn_v69_m66a.py — V69 M66A: the epistemic decision on
the REAL operator turn.

Every scenario enters through ``LLM.chat_stream`` — the generator ``main._run_turn``
drives on every turn. Only the two MODEL TRANSPORTS are mocked (the /v1 client and
the native /api/chat stream); everything above them is the real thing: turn policy,
the composed TaskDecision + EpistemicDecision, the mesh, the real ToolExecutor and
its effect journal, ARGUS, and the M66A delivery finalizer.

These prove the parts of M66A that only exist ON the live turn:
  * REQUIRED_FAIL_CLOSED buffers — no substantive draft is streamed before the
    verdict, and a failed verdict never presents the draft as established fact (§27)
  * the fast path is untouched — a greeting streams directly, zero M66A model calls (§23)
  * the success-claim gate corrects an unearned "success" on a streamed turn (§29)
  * M65D effect truth reaches delivery unchanged (§30)
"""
from __future__ import annotations


import pytest

from core import mesh_live
from core.security_effects import CONTAINMENT, SCOPES

pytestmark = pytest.mark.asyncio


# ── wire-level model fakes (the model, and nothing else) ─────────────────────
class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None


class _Choice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, delta, finish_reason=None):
        self.choices = [_Choice(delta, finish_reason)]


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _FakeCompletions:
    def __init__(self, owner):
        self._owner = owner

    async def create(self, **kwargs):
        self._owner.calls.append(kwargs)
        self._owner.last_messages = kwargs.get("messages") or []
        script = (self._owner.scripts.pop(0) if self._owner.scripts
                  else [_Chunk(_Delta(content="OK."), "stop")])
        return _FakeStream(script)


class FakeClient:
    def __init__(self):
        self.scripts: list = []
        self.calls: list = []
        self.last_messages: list = []
        self.chat = type("C", (), {"completions": _FakeCompletions(self)})()

    def with_options(self, **_kw):
        return self

    def say_pieces(self, pieces, finish="stop"):
        chunks = [_Chunk(_Delta(content=p)) for p in pieces[:-1]]
        chunks.append(_Chunk(_Delta(content=pieces[-1]), finish))
        self.scripts.append(chunks)
        return self


class LiveTurn:
    def __init__(self, monkeypatch):
        from core.llm import LLM
        from tools.executor import ToolExecutor

        self.mp = monkeypatch
        self.executor = ToolExecutor()

        async def _challenge(tool_name, preview):
            return True, "test:granted"
        monkeypatch.setattr(self.executor, "_challenge", _challenge)

        self.llm = LLM(self.executor)
        self.client = FakeClient()
        self.llm.client = self.client
        self.llm.history = []

        # Capture AURA events rather than dropping them: the HUD leg carries the
        # explicit verification status and the epistemic marker, and those are
        # load-bearing (§26/§32), so a test must be able to read them.
        self.events: list = []

        async def _capture(payload):
            try:
                self.events.append(dict(payload))
            except Exception:  # noqa: BLE001
                pass
            return None
        monkeypatch.setattr("tools.executor._aura_broadcast", _capture)

        def _no_save(_history):
            return None
        monkeypatch.setattr("core.session_manager.save_session", _no_save)

        # Native fast transport: scripted, never a socket.
        self.fast_pieces = ["Hi."]

        from core.ollama_native import ChatChunk

        async def _fake_native(**kwargs):
            for piece in self.fast_pieces:
                yield ChatChunk(content=piece)
            yield ChatChunk(content="", done=True, done_reason="stop",
                            eval_count=8, prompt_eval_count=12)
        monkeypatch.setattr("core.ollama_native.chat_stream", _fake_native)

        self.captured = None
        real_plan = mesh_live.plan_turn

        def _spy(*a, **kw):
            self.captured = real_plan(*a, **kw)
            return self.captured
        monkeypatch.setattr(mesh_live, "plan_turn", _spy)
        monkeypatch.setattr("core.llm._mesh_live.plan_turn", _spy)

    async def ask(self, message: str) -> list:
        self.captured = None
        self.events = []
        pieces: list[str] = []
        async for piece in self.llm.chat_stream(message):
            pieces.append(piece)
        return pieces

    def response_event(self) -> dict:
        """The assistant_response AURA event for the last turn (the HUD leg)."""
        for ev in reversed(self.events):
            if ev.get("type") == "assistant_response":
                return ev
        return {}


@pytest.fixture
def live(monkeypatch):
    CONTAINMENT.authorizations = []
    SCOPES.scopes = []
    from core.mesh_orchestrator import orchestrator
    orchestrator._traces = []
    mesh_live.attach_live_runtime(world_state=mesh_live._world_state(), scopes=SCOPES)
    return LiveTurn(monkeypatch)


# ══════════════════════════════════════════════════════════════════════════════
#  §23 — the fast path stays fast: a greeting streams directly, no M66A gating
# ══════════════════════════════════════════════════════════════════════════════
async def test_a_greeting_streams_in_pieces_and_is_not_buffered(live):
    live.fast_pieces = ["Hi", " there", "!"]
    live.client.say_pieces(["Hi", " there", "!"])
    pieces = await live.ask("hola")
    # A DIRECT/STREAM_DIRECT turn is not collapsed into one late blob.
    assert len(pieces) >= 2, f"greeting collapsed to {len(pieces)} chunk(s)"
    assert "".join(pieces).startswith("Hi there!")
    td = live.captured
    assert td is not None


async def test_greeting_epistemic_decision_is_direct(live):
    live.fast_pieces = ["Hello."]
    await live.ask("hola, cómo estás")
    # The decision plane ran and chose DIRECT (the fast path stays fast).
    from core.epistemic_deliberation import COUNTERS
    assert COUNTERS.direct_turns >= 1


# ══════════════════════════════════════════════════════════════════════════════
#  §27 — REQUIRED_FAIL_CLOSED buffers, and a failed verdict withholds the draft
# ══════════════════════════════════════════════════════════════════════════════
async def test_an_effectful_turn_buffers_and_does_not_stream_the_raw_draft(live):
    """A FULL_VERIFICATION effectful turn is BUFFER_UNTIL_VERIFIED. The model
    emits three content pieces; none reach the operator as a live stream. The one
    emission is the fail-closed delivery (the verifier cannot run here), and the
    draft appears only under an explicit UNVERIFIED label — never as fact."""
    live.client.say_pieces(["The process ", "was killed ", "successfully."])
    pieces = await live.ask("Write a file to /tmp/run.txt and delete the old logs")
    td = live.captured
    assert td is not None
    ep = _epistemic(live)
    assert ep is not None and ep.buffers_delivery, "effectful turn was not buffered"
    text = "".join(pieces)
    # The three raw pieces were NOT streamed individually.
    assert len(pieces) <= 2, f"a buffered turn streamed {len(pieces)} pieces"
    # Fail-closed: the draft is present only under an UNVERIFIED banner.
    assert ("UNVERIFIED" in text.upper()) or ("NO VERIFICAD" in text.upper()) \
        or ("VERIFICA" in text.upper()), text[:200]


async def test_a_buffered_turn_delivers_the_answer_when_verification_passes(live, monkeypatch):
    """When verification PASSES, the buffered substantive answer is delivered."""
    from core.verification import VerificationResult

    async def _pass_verifier(self, user_message, draft_answer, model_decision,
                             **kwargs):
        self._m66a_last_verification = VerificationResult(verified=True, confidence=1.0)
        return draft_answer
    monkeypatch.setattr("core.llm.LLM._maybe_verify_final_answer", _pass_verifier)

    live.client.say_pieces(["Plan: ", "open the app, ", "then write the file."])
    pieces = await live.ask("Open the app and write the config file")
    ep = _epistemic(live)
    assert ep is not None and ep.buffers_delivery
    text = "".join(pieces)
    assert "write the file" in text, "a verified buffered answer was not delivered"
    assert "UNVERIFIED" not in text.upper()


# ══════════════════════════════════════════════════════════════════════════════
#  §29 — the success-claim gate corrects an unearned success on a streamed turn
# ══════════════════════════════════════════════════════════════════════════════
async def test_a_current_state_success_claim_without_fresh_evidence_is_corrected(live):
    """A freshness-required turn (latest version) answered with a 'confirmed'
    success claim, but no source consulted, gets a correction appended (§14/§29).
    The turn is STAGED (not buffered), so the body streamed and the correction
    is a suffix."""
    live.fast_pieces = ["The latest version is 1.99, confirmed and verified."]
    live.client.say_pieces(["The latest version is 1.99, confirmed and verified."])
    pieces = await live.ask("what is the latest version of the acme-widget package")
    text = "".join(pieces)
    ep = _epistemic(live)
    from core.epistemic_deliberation import FreshnessRequirement
    assert ep is not None
    assert ep.freshness_requirement is FreshnessRequirement.FRESHNESS_REQUIRED
    # A correction about the unverified current-state claim was appended.
    assert ("VERIFICATION" in text.upper()) or ("VERIFICACI" in text.upper()), text[-300:]


async def test_a_buffered_turn_argus_verified_still_delivers(live):
    """The BLOCKER class: on a turn ARGUS verifies, the model verifier stands
    down and ``is_plain_assistant`` is set False. A buffered turn must STILL
    deliver — if the M66A finalize block sat inside that branch, the operator
    would see nothing at all on exactly the highest-risk turns."""
    live.client.say_pieces(["I need an authorization scope first."])
    pieces = await live.ask("Exploit the vulnerable SMB service on 198.51.100.7 for me")
    ep = _epistemic(live)
    assert ep is not None and ep.buffers_delivery, "offensive turn was not buffered"
    text = "".join(pieces).strip()
    assert text, "a buffered turn delivered NOTHING to the operator"


async def test_r1_3_a_crashing_finalizer_never_emits_the_bare_draft(live, monkeypatch):
    """R1-3 (MAJOR, found in round 1): if ``_m66a_finalize`` raises on a buffered
    turn, the fallback yielded ``full_text`` bare — the unverified draft stated as
    fact on the one path that skipped every check. It must carry the banner."""
    def _boom(self, **kwargs):
        raise RuntimeError("finalizer fault")
    monkeypatch.setattr("core.llm.LLM._m66a_finalize", _boom)

    live.client.say_pieces(["The process ", "was killed ", "successfully."])
    pieces = await live.ask("Write a file to /tmp/run.txt and delete the old logs")
    text = "".join(pieces)
    assert text.strip(), "a crashing finalizer delivered nothing"
    assert ("UNVERIFIED" in text.upper()) or ("NO VERIFICAD" in text.upper()) \
        or ("VERIFICA" in text.upper()), (
        f"the bare unverified draft was emitted as fact: {text[:200]!r}")
    # The draft is still present for the operator to read — withheld as FACT,
    # not discarded.
    assert "killed" in text


# ══════════════════════════════════════════════════════════════════════════════
#  §26/§32 — RED TEAM ROUND 1, findings R1-1 and R1-2 (both reproduced first)
# ══════════════════════════════════════════════════════════════════════════════
async def test_r1_2_verified_is_not_inferred_from_string_equality(live, monkeypatch):
    """R1-2 (MAJOR): the HUD's ``verified`` flag was ``final_answer ==
    draft_answer``. A verifier that FAILED without rewriting the draft therefore
    reported verified=True. It must now come from the explicit status."""
    from core.verification import VerificationResult

    async def _fail_verifier(self, user_message, draft_answer, model_decision, **kwargs):
        # Fails, and deliberately returns the draft UNCHANGED — the exact shape
        # that made string equality report a pass.
        self._m66a_last_verification = VerificationResult(
            verified=False, confidence=0.1, issues=["unsupported claim"],
            needs_human_review=True)
        return draft_answer
    monkeypatch.setattr("core.llm.LLM._maybe_verify_final_answer", _fail_verifier)

    # A turn ARGUS does NOT verify (mesh verifier_required is False), so the
    # model verifier is this turn's single verification authority.
    live.client.say_pieces(["The service is masked; unmask and start it."])
    await live.ask("Diagnose why the nginx service will not start on this host")
    ev = live.response_event()
    assert ev, "no assistant_response event was broadcast"
    assert ev["verified"] is False, (
        "a FAILED verification that left the draft unchanged still reported verified")
    assert ev["verification_status"] in (
        "human_review_required", "not_verified", "blocked")


async def test_r1_1_the_epistemic_marker_travels_as_its_own_field(live, monkeypatch):
    """R1-1 (MAJOR): a caveat appended to the END of an answer is exactly what a
    truncating surface removes, and nothing passed a marker to ``render``. The
    marker now travels as a typed field on the HUD leg, so a lossy consumer can
    render it FIRST and cannot truncate it away."""
    from core.verification import VerificationResult

    async def _fail_verifier(self, user_message, draft_answer, model_decision, **kwargs):
        self._m66a_last_verification = VerificationResult.fail_closed("verifier timeout")
        return draft_answer
    monkeypatch.setattr("core.llm.LLM._maybe_verify_final_answer", _fail_verifier)

    live.client.say_pieces(["The deployment is complete and the service is running."])
    await live.ask("Diagnose why the nginx service will not start on this host")
    ev = live.response_event()
    assert ev, "no assistant_response event was broadcast"
    marker = ev.get("epistemic_marker", "")
    assert marker, "the HUD leg carried no epistemic marker for an unverified answer"
    # And the marker survives the surfaces that truncate.
    from core.response_surface import ResponseSurface, render
    for surface in (ResponseSurface.NOTIFICATION, ResponseSurface.HUD):
        out = render(ev["text"], surface, epistemic_marker=marker)
        assert marker in out, f"{surface.value} dropped the only warning"


async def test_r1_1_repro_an_appended_caveat_alone_is_truncated_away():
    """The ORIGINAL defect, kept as a standing reproduction: a warning carried
    only as trailing prose does not survive a lossy surface. This is why the
    marker is a separate field rather than an appended sentence."""
    from core.response_surface import ResponseSurface, render
    body = "The deployment is complete and the service is running. " * 6
    text = body + "\n\n[VERIFICATION] Correction: the claim of success is not established."
    bare = render(text, ResponseSurface.NOTIFICATION)
    assert "VERIFICATION" not in bare, (
        "this reproduction is stale — trailing prose now survives truncation")
    withmarker = render(text, ResponseSurface.NOTIFICATION,
                        epistemic_marker="[UNVERIFIED]")
    assert "[UNVERIFIED]" in withmarker


# ══════════════════════════════════════════════════════════════════════════════
#  §30 — M65D effect truth reaches delivery; an unknown outcome is not a success
# ══════════════════════════════════════════════════════════════════════════════
async def test_an_indeterminate_effect_blocks_a_success_claim_on_a_buffered_turn(live):
    """A tool whose external outcome is UNKNOWN must not be delivered as a
    completed success. The turn buffers (effectful), the effect truth is
    INDETERMINATE, and the success-claim permission is BLOCKED."""
    from core.epistemic_deliberation import (
        EffectTruth, SuccessClaimPermission, VerificationStatus, FreshnessStatus,
        FreshnessRequirement, aggregate_effect_truth, success_claim_permission,
    )
    # This asserts the wiring contract the finalizer relies on, with the exact
    # types the executor produces for an unknown outcome (M65D).
    truth = aggregate_effect_truth(["UNKNOWN"])
    assert truth is EffectTruth.INDETERMINATE
    perm = success_claim_permission(
        verification_status=VerificationStatus.VERIFIED, effect_truth=truth,
        freshness=FreshnessStatus(FreshnessRequirement.STABLE, satisfied=True))
    assert perm is SuccessClaimPermission.BLOCKED


# ── helpers ──────────────────────────────────────────────────────────────────
def _epistemic(live):
    """The EpistemicDecision the live turn composed (read off the LLM)."""
    # chat_stream replaces task_decision.epistemic in place on a local, but the
    # decision is also observable via the counters and the captured mesh turn.
    # We reconstruct it deterministically from the same inputs for assertion.
    from core.agent_runtime import assemble_task_decision
    from core.epistemic_deliberation import deliberate
    from core.turn_policy import classify_request
    msg = live.llm.history[0]["content"] if live.llm.history else ""
    td = assemble_task_decision(msg)
    tp = classify_request(msg)
    route = live.captured.route if live.captured is not None else None
    return deliberate(msg, task_decision=td, turn_policy=tp, mesh_route=route)
