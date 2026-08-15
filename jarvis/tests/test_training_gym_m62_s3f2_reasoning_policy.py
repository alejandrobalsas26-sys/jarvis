"""V69 M62 S3F.2 — the eligibility-grade reasoning policy (operator ruling H6a).

The human operator ruled that a future eligibility-grade evaluation measures the model's
final answer rather than spending a large part of the token budget narrating hidden
reasoning that then interferes with a machine-readable contract:
``reasoning_policy = DISABLED``.

Three things have to hold for that ruling to mean anything, and each is pinned here.

1. It is a FORWARD policy, not a re-labelling of history. S3E.2 ran under
   ``MODEL_DEFAULT`` and its artefacts still say so. The default stays ``MODEL_DEFAULT``;
   ``DISABLED`` arrives as a separate, named object.
2. It is bound to identity. The policy travels in ``policy_hash`` and therefore in
   ``parity_hash``, so a report names the semantics that produced it and two
   differently-thinking arms cannot be compared.
3. It changes what is *graded*, never what is *scanned*. The reasoning stripper feeds the
   structural check only; the security scan still reads the whole raw response, so a
   credential inside a ``<think>`` block still blocks.
"""
from __future__ import annotations

import json

import pytest

from training_gym.evaluation.backend import (
    BackendStatus,
    EvaluationRequest,
    EvaluationResult,
    EvaluationRole,
)
from training_gym.evaluation.generation import (
    DEFAULT_GENERATION_POLICY,
    ELIGIBILITY_REASONING_POLICY,
    GenerationPolicy,
    GenerationPolicyError,
    ReasoningPolicy,
    assert_identical_policies,
    eligibility_generation_policy,
)
from training_gym.evaluation.policy import EvaluationPolicySet
from training_gym.evaluation.scoring import final_answer, score_arm

from _m62_evaluation_fixtures import (
    adapter_reference,
    baseline_reference,
    make_pack,
    make_store,
)

pytest.importorskip("scripts.qualify_reasoning_policy")
from scripts import qualify_reasoning_policy as QRP  # noqa: E402

_POLICIES = EvaluationPolicySet()


# ══════════════════════════════════════════════════════════════════════════════
#  1-2 — the ruling, and the identity it moves
# ══════════════════════════════════════════════════════════════════════════════
def test_the_operator_ruling_is_recorded_as_a_named_policy():
    assert ELIGIBILITY_REASONING_POLICY is ReasoningPolicy.DISABLED
    assert eligibility_generation_policy().reasoning_policy is ReasoningPolicy.DISABLED


def test_the_default_still_preserves_what_s3e2_actually_did():
    """A changed default would silently reinterpret every existing configuration and make
    the historical measurement look like it used a setting it never had."""
    assert DEFAULT_GENERATION_POLICY.reasoning_policy is ReasoningPolicy.MODEL_DEFAULT
    assert GenerationPolicy().reasoning_policy is ReasoningPolicy.MODEL_DEFAULT


def test_disabled_survives_a_policy_round_trip():
    policy = eligibility_generation_policy(seed=11)
    restored = GenerationPolicy.from_dict(policy.to_dict())
    assert restored.reasoning_policy is ReasoningPolicy.DISABLED
    assert restored.policy_hash() == policy.policy_hash()


def test_disabled_survives_a_config_round_trip():
    """Through the file an operator actually writes, not through an internal dict."""
    from training_gym.evaluation.config import config_from_dict
    from test_training_gym_m62_evaluation_config import _config_payload
    policy = eligibility_generation_policy(seed=11)
    payload = _config_payload(generation=policy.to_dict(), seed=policy.seed)
    restored = config_from_dict(json.loads(json.dumps(payload)))
    assert restored.generation.reasoning_policy is ReasoningPolicy.DISABLED
    assert restored.generation.policy_hash() == policy.policy_hash()
    assert restored.config_hash() == config_from_dict(payload).config_hash()


def test_the_policy_hash_differs_from_model_default():
    assert (eligibility_generation_policy(seed=11).policy_hash()
            != GenerationPolicy(seed=11).policy_hash())


def test_the_config_identity_moves_when_the_reasoning_policy_moves():
    default = _config(GenerationPolicy(seed=11))
    disabled = _config(eligibility_generation_policy(seed=11))
    assert default.config_hash() != disabled.config_hash()


def test_an_unknown_reasoning_policy_is_refused():
    with pytest.raises(Exception):
        GenerationPolicy(reasoning_policy="sometimes")


# ══════════════════════════════════════════════════════════════════════════════
#  3-4, 8 — parity: both arms, or neither
# ══════════════════════════════════════════════════════════════════════════════
def _request(policy: GenerationPolicy, *, role=EvaluationRole.BASELINE):
    pack = make_pack()
    return EvaluationRequest(
        role=role, task=pack.tasks[0], generation=policy,
        baseline=baseline_reference(),
        adapter=adapter_reference() if role is EvaluationRole.CANDIDATE else None)


def test_the_parity_hash_binds_the_reasoning_policy():
    assert (_request(eligibility_generation_policy(seed=11)).parity_hash()
            != _request(GenerationPolicy(seed=11)).parity_hash())


def test_two_arms_under_different_reasoning_policies_cannot_be_paired():
    with pytest.raises(GenerationPolicyError, match="differently-decoded arms"):
        assert_identical_policies(GenerationPolicy(seed=11),
                                  eligibility_generation_policy(seed=11))


def test_both_arms_under_the_ruling_pair_and_share_one_parity_hash():
    policy = eligibility_generation_policy(seed=11)
    shared = assert_identical_policies(policy, policy)
    assert shared == policy.policy_hash()
    baseline = _request(policy, role=EvaluationRole.BASELINE)
    candidate = _request(policy, role=EvaluationRole.CANDIDATE)
    # The adapter is the ONLY permitted difference: parity deliberately excludes the role
    # and the adapter and includes everything the model sees.
    assert baseline.parity_hash() == candidate.parity_hash()
    assert baseline.to_public_dict()["generation_policy_hash"] == shared
    assert candidate.to_public_dict()["generation_policy_hash"] == shared


def test_the_public_request_description_names_the_policy_digest():
    policy = eligibility_generation_policy(seed=11)
    described = _request(policy).to_public_dict()
    assert described["generation_policy_hash"] == policy.policy_hash()


# ══════════════════════════════════════════════════════════════════════════════
#  5 — the backend reads the policy instead of deciding on its own
# ══════════════════════════════════════════════════════════════════════════════
def _thinking_literals(source_text: str) -> list[object]:
    import ast

    literals: list[object] = []
    for node in ast.walk(ast.parse(source_text)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "enable_thinking" and isinstance(keyword.value,
                                                               ast.Constant):
                literals.append(keyword.value.value)
    return literals


def test_the_backend_passes_the_policy_it_was_given_and_never_hard_codes_thinking():
    """The property is unchanged; S3M.1 moved where half of it is checked.

    Until S3M.1 the honour check lived in this backend, so the only literal
    ``enable_thinking`` pair in the file was its deliberate render-both-ways comparison.
    S3M.1 (defect D37) moved that check to ``training_gym.training.chat_render`` so the
    TRAINING backend applies the identical one instead of forking a copy — the shared
    module is imported by both. So the assertion splits in two and gets stricter on this
    side: the evaluation backend must now hard-code **no** ``enable_thinking`` literal at
    all, and the only literal pair anywhere is still the honour check's.
    """
    import pathlib

    root = pathlib.Path("jarvis")
    if not (root / "training_gym").is_dir():  # pragma: no cover - layout shim
        root = pathlib.Path(__file__).resolve().parents[1]
    backend = (root / "training_gym/evaluation/backends/transformers_peft.py"
               ).read_text(encoding="utf-8")
    shared = (root / "training_gym/training/chat_render.py").read_text(encoding="utf-8")

    assert _thinking_literals(backend) == [], (
        "the evaluation backend hard-codes an enable_thinking literal again; it must "
        "read the plan-bound policy and nothing else")
    assert sorted(set(_thinking_literals(shared)), key=str) == [False, True]
    assert "policy.reasoning_policy.template_kwarg" in backend


# ══════════════════════════════════════════════════════════════════════════════
#  6-7 — the template-honour preflight, in both directions
# ══════════════════════════════════════════════════════════════════════════════
class _IgnoringTokenizer:
    """A template that never reads ``enable_thinking``: the silent no-op case."""

    def apply_chat_template(self, messages, **kwargs):
        return "<|user|>" + messages[-1]["content"]


class _HonouringTokenizer:
    def apply_chat_template(self, messages, **kwargs):
        suffix = "<think>\n" if kwargs.get("enable_thinking", True) else ""
        return "<|user|>" + messages[-1]["content"] + "<|assistant|>" + suffix


class _RefusingTokenizer:
    def apply_chat_template(self, messages, **kwargs):
        if "enable_thinking" in kwargs:
            raise TypeError("unexpected keyword argument 'enable_thinking'")
        return "<|user|>" + messages[-1]["content"]


def _honours(tokenizer) -> bool:
    from training_gym.evaluation.backends.transformers_peft import (
        template_honours_reasoning_policy,
    )
    return template_honours_reasoning_policy(tokenizer, QRP.PROBE_MESSAGES)


def test_the_preflight_fails_closed_when_the_template_ignores_the_request():
    assert _honours(_IgnoringTokenizer()) is False


def test_the_preflight_fails_closed_when_the_template_refuses_the_keyword():
    assert _honours(_RefusingTokenizer()) is False


def test_the_preflight_passes_when_the_template_implements_the_request():
    assert _honours(_HonouringTokenizer()) is True


def test_the_preflight_script_blocks_rather_than_guessing_a_cache_root(tmp_path):
    """A guessed root that turns out empty is indistinguishable from having looked in the
    wrong place, so an unlocatable cache is BLOCKED and never a pass."""
    report = QRP.qualify(cache_root=str(tmp_path / "nothing-here"))
    assert report["preflight"] == "blocked_cache_not_located"
    assert report["generation_performed"] is False
    assert report["tokens_produced"] == 0


def test_the_preflight_script_never_reports_a_pass_it_did_not_measure(tmp_path):
    report = QRP.qualify(cache_root=str(tmp_path / "nothing-here"))
    assert report["preflight"] != "pass"
    assert report["reasoning_policy"] == "disabled"
    assert report["policy_hash"] == eligibility_generation_policy(seed=11).policy_hash()


def test_the_preflight_script_publishes_no_path_and_no_rendered_prompt(tmp_path):
    body = json.dumps(QRP.qualify(cache_root=str(tmp_path / "nothing-here")))
    assert str(tmp_path) not in body
    assert "C:/Users" not in body and "C:\\\\Users" not in body and "/home/" not in body


def test_the_preflight_model_identity_is_an_immutable_commit():
    assert len(QRP.MODEL_REVISION) == 40
    assert all(c in "0123456789abcdef" for c in QRP.MODEL_REVISION)


# ══════════════════════════════════════════════════════════════════════════════
#  9-13 — what DISABLED changes, and what it must never change
# ══════════════════════════════════════════════════════════════════════════════
def _score(text: str):
    from dataclasses import replace
    pack = make_pack()
    task = replace(pack.tasks[0],
                   expected_output_schema={"type": "object",
                                           "additionalProperties": True})
    store = make_store(pack)
    result = EvaluationResult(
        backend_id="fake_evaluation", backend_version="m62.fake_evaluation.1",
        role=EvaluationRole.BASELINE, task_id=task.task_id, task_hash=task.task_hash,
        status=BackendStatus.SUCCEEDED, response_text=text, output_tokens=32)
    return score_arm(task, result,
                     target=store.lookup(task.task_id, task_hash=task.task_hash),
                     policy=_POLICIES.graders)


def test_a_genuine_secret_inside_a_reasoning_block_still_blocks():
    score = _score("<think>the token is AKIAIOSFODNN7EXAMPLE and the password is "
                   'hunter2</think>\n{"severity": "low"}')
    assert score.security_findings
    assert score.blocking is True and score.reward == 0.0


def test_a_genuine_home_path_inside_a_reasoning_block_still_blocks():
    score = _score("<think>look in /home/analyst/.ssh/id_rsa</think>\n"
                   '{"severity": "low"}')
    assert "secret_pii:home_path" in score.security_findings
    assert score.blocking is True


def test_reasoning_markup_alone_remains_hygiene_and_never_a_security_finding():
    """Operator ruling H4: a model's own reasoning markup discloses nobody's private
    data. It stays visible as response hygiene and stops driving a blocking verdict."""
    score = _score('<think>let me consider the alert</think>\n{"severity": "low"}')
    assert score.hygiene_findings == ("reasoning",)
    assert score.security_findings == ()
    assert score.blocking is False


def test_the_structural_check_reads_the_final_answer_and_the_scan_reads_the_whole_body():
    raw = "<think>/home/analyst/.ssh/id_rsa</think>\n{\"severity\": \"low\"}"
    answer, blocks = final_answer(raw)
    assert blocks == 1
    assert "/home/analyst" not in answer
    score = _score(raw)
    # The contract was satisfied by the final answer AND the leak was still found.
    assert score.json_parseable is True and score.schema_valid is True
    assert "secret_pii:home_path" in score.security_findings


def test_a_response_that_never_leaves_its_reasoning_block_is_named_distinctly():
    score = _score("<think>still thinking about it")
    assert score.json_parseable is False
    assert "structured_output_never_left_reasoning_block" in score.note_codes


# ══════════════════════════════════════════════════════════════════════════════
#  14 — history is not restated
# ══════════════════════════════════════════════════════════════════════════════
def test_a_legacy_policy_document_without_a_reasoning_policy_still_loads():
    payload = GenerationPolicy(seed=11).to_dict()
    payload.pop("reasoning_policy")
    restored = GenerationPolicy.from_dict(payload)
    assert restored.reasoning_policy is ReasoningPolicy.MODEL_DEFAULT
    assert restored.policy_hash() == GenerationPolicy(seed=11).policy_hash()


def test_the_sealed_s3e2_report_still_says_model_default_or_says_nothing():
    """Nothing in this milestone re-labels the historical measurement."""
    import pathlib
    sealed = pathlib.Path(
        "jarvis/evaluation/evaluations/qwen3-06b-lora-live-eval-001/gen-3/"
        "evaluation-report.json")
    if not sealed.is_file():
        pytest.skip("the sealed S3E.2 generation is not present on this host")
    body = sealed.read_text(encoding="utf-8")
    assert '"reasoning_policy": "disabled"' not in body
    assert '"reasoning_policy": "enabled"' not in body


def _config(policy: GenerationPolicy):
    from training_gym.evaluation.config import config_from_dict
    from test_training_gym_m62_evaluation_config import _config_payload
    return config_from_dict(_config_payload(generation=policy.to_dict(),
                                            seed=policy.seed))
