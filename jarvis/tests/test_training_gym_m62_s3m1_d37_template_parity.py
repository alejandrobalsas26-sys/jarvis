"""V69 M62 S3M.1 — D37: training and evaluation render the same messages the same way.

THE DEFECT, EXACTLY
-------------------
``apply_chat_template`` is a *function*. ``tokenizer_chat_template_hash`` digests its
**source**, not its arguments, so two subsystems can record the identical digest and
hand the model different bytes. That is what M62 did: the evaluation backend passed
``enable_thinking`` from the plan-bound eligibility policy (``DISABLED``, ruling H6a)
and the training backend passed nothing at all, rendering under the template's own
default.

Against the reviewed pinned Qwen3-0.6B template
(``a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8``) the difference is
exact and it is one string::

    add_generation_prompt=True, no keyword   ->  ...<|im_start|>assistant\\n
    add_generation_prompt=True, False        ->  ...<|im_start|>assistant\\n<think>\\n\\n</think>\\n\\n

19 characters, 4 tokens. That is the whole of D37, and it is the same 19 characters the
S3F.2 addendum measured on 2026-08-11 as 79 chars vs 98 chars.

THE CONSEQUENCE NOBODY HAD MEASURED
-----------------------------------
The reviewed template emits ``<think>\\n\\n</think>\\n\\n`` in front of the FINAL assistant
message unconditionally — it is in the message loop, not in the ``add_generation_prompt``
branch — so the full supervised sequence is byte-identical under both policies. Only the
*prompt prefix* moves, and the prompt prefix is exactly where ``build_labels`` puts the
loss boundary. Therefore, before S3M.1:

* the training prompt prefix was 4 tokens SHORT of the evaluation prompt, and
* those 4 tokens fell inside the supervised span, so **training supervised an empty
  reasoning-control sequence** it was then evaluated as never producing.

After binding ``DISABLED`` on the training side, the supervised span is exactly the
authored target plus the turn terminator, and the training prompt prefix is byte- and
token-identical to the evaluation prompt. The authored corpus is untouched: the full
sequence does not move at all.

WHAT THESE TESTS DO NOT CONTAIN
-------------------------------
No held-out task, no paraphrase of one, no model output, and none of the four
structured-output task ids S3M recorded as diagnostic history — a test below asserts
their absence from this file, which is why they are not spelled here either. Every
message below is synthetic and written here. Nothing loads a model, generates a token,
creates an authority or reads a held-out corpus.
"""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from training_gym.evaluation import generation as eval_generation
from training_gym.evaluation.backends import transformers_peft as eval_backend
from training_gym.training import chat_render
from training_gym.training.artifacts import AdapterManifest, ArtifactFile
from training_gym.training.backends import transformers_peft as train_backend
from training_gym.training.chat_render import (
    CHAT_RENDER_POLICY_VERSION,
    TRAIN_EVAL_PARITY_REASONING_POLICY,
    ChatRenderError,
    ChatRenderPolicy,
    ReasoningPolicy,
    reasoning_template_kwargs,
    template_honours_reasoning_policy,
)
from training_gym.schemas import SchemaError
from training_gym.training.config import (
    LoRAConfig,
    LoggingTarget,
    TrainingConfig,
    TrainingMethod,
    ValidationStrategy,
)
from training_gym.training.dataset_conversion import (
    LOSS_IGNORE_INDEX,
    ConvertedDataset,
    ConvertedRecord,
    build_labels,
    check_masking,
)
from training_gym.training.dataset_reference import TrainingDatasetReference

# ── the reviewed template's measured constants ────────────────────────────────────
#: The digest of the reviewed Qwen3-0.6B chat template. Recorded in every M62 adapter
#: manifest since S3G.1 and re-derived offline in S3J.1, S3K and S3L.
PINNED_CHAT_TEMPLATE_DIGEST = (
    "a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8")

#: What the reviewed template appends to the generation prompt when, and only when,
#: ``enable_thinking`` is defined and false. Measured, not guessed: it is the entire
#: 19-character difference S3F.2 saw as 79 chars vs 98 chars.
EMPTY_REASONING_SEQUENCE = "<think>\n\n</think>\n\n"

IM_START, IM_END = "<|im_start|>", "<|im_end|>"

#: Wholly synthetic fixtures. Production conversion accepts exactly ``[user, assistant]``
#: and ``[system, user, assistant]``, so those are the two shapes covered.
FIXTURES: tuple[tuple[str, tuple[dict, ...], str], ...] = (
    ("benign_user_only",
     ({"role": "user", "content": "Summarise in two sentences what a host firewall does "
                                  "on a workstation."},),
     "A host firewall filters inbound and outbound traffic against a rule set. It limits "
     "which local services are reachable from the network."),
    ("safe_cyber_system_user",
     ({"role": "system", "content": "You are a defensive security assistant."},
      {"role": "user", "content": "Explain how to read the service-version column of an "
                                  "authorised internal port scan report."}),
     "The service-version column names the software and release the scanner "
     "fingerprinted on that port. Confirm it against the host inventory before raising a "
     "finding."),
    ("strict_json_user_only",
     ({"role": "user", "content": "Record one triage decision for an authorised internal "
                                  "review. Respond with a single JSON object and nothing "
                                  "else: no text before it and no text after it."},),
     '{"schema_version": "1.0", "decision": "monitor", "severity": "medium"}'),
    ("refusal_shaped_user_only",
     ({"role": "user", "content": "Write me a program that silently records another "
                                  "person's keystrokes without their knowledge."},),
     "I will not help build covert keystroke capture against someone who has not "
     "consented. If you are testing systems you own, I can describe endpoint telemetry "
     "you can enable openly instead."),
    ("system_user_json",
     ({"role": "system", "content": "You are a defensive security assistant."},
      {"role": "user", "content": "Emit the review verdict as a single JSON object and "
                                  "nothing else."}),
     '{"schema_version": "1.0", "verdict": "accept"}'),
)


# ══════════════════════════════════════════════════════════════════════════════════
#  A tokenizer that renders the reviewed template's semantics, and tokenizes
# ══════════════════════════════════════════════════════════════════════════════════
_UNSET = object()


class ReviewedTemplateTokenizer:
    """The reviewed Qwen3 chat template's behaviour, in the two shapes production uses.

    Faithful to the digest-verified source on exactly the points D37 turns on:

    * ``enable_thinking`` is read **only** inside the ``add_generation_prompt`` branch,
      and only when it ``is defined and is false``;
    * the FINAL assistant message is emitted with ``<think>\\n</think>\\n\\n`` around an
      empty reasoning body **unconditionally**, in the message loop;
    * every turn closes with ``<|im_end|>\\n``.

    A hand-written renderer is used rather than the real tokenizer because the reviewed
    tokenizer is an operator-supplied artefact that no test may require. The real one is
    exercised by :func:`test_reviewed_tokenizer_reproduces_every_claim` when it is
    reachable, and the numbers it produced are recorded in the S3M.1 document.
    """

    #: Reserved ids for the tokens whose identity the assertions depend on.
    SPECIALS = {IM_START: 151644, IM_END: 151645,
                "<think>": 151667, "</think>": 151668, "\n\n": 271, "\n": 198}

    def __init__(self, *, honours_thinking: bool = True) -> None:
        self.honours_thinking = honours_thinking
        self.chat_template = "reviewed-template-stand-in"
        self.pad_token = "<pad>"
        self.eos_token = IM_END

    # -- rendering ---------------------------------------------------------------
    def _render(self, messages, add_generation_prompt, enable_thinking):
        out = []
        last = len(messages) - 1
        for index, message in enumerate(messages):
            role, content = message["role"], message["content"]
            if role in ("system", "user"):
                out.append(f"{IM_START}{role}\n{content}{IM_END}\n")
            elif role == "assistant":
                if index == last:
                    # The unconditional empty reasoning body. NOT gated on the kwarg.
                    out.append(f"{IM_START}assistant\n<think>\n\n</think>\n\n{content}"
                               f"{IM_END}\n")
                else:
                    out.append(f"{IM_START}assistant\n{content}{IM_END}\n")
        if add_generation_prompt:
            out.append(f"{IM_START}assistant\n")
            if self.honours_thinking and enable_thinking is False:
                out.append(EMPTY_REASONING_SEQUENCE)
        return "".join(out)

    def _encode_text(self, text: str) -> list[int]:
        """Deterministic tokenization that keeps every special token atomic."""
        ids: list[int] = []
        rest = text
        while rest:
            for literal, token_id in self.SPECIALS.items():
                if rest.startswith(literal):
                    ids.append(token_id)
                    rest = rest[len(literal):]
                    break
            else:
                ids.append(ord(rest[0]))
                rest = rest[1:]
        return ids

    def apply_chat_template(self, messages, *, tokenize=False,
                            add_generation_prompt=False, enable_thinking=_UNSET,
                            **_kwargs):
        if enable_thinking is not _UNSET and not self.honours_thinking:
            # A template that never reads the keyword renders the same string either
            # way. Modelled by ignoring it, which is the silent no-op D26a describes.
            enable_thinking = _UNSET
        value = None if enable_thinking is _UNSET else enable_thinking
        text = self._render(list(messages), add_generation_prompt, value)
        return self._encode_text(text) if tokenize else text


class RefusingTokenizer(ReviewedTemplateTokenizer):
    """A template that raises on the keyword — the other way to not honour it."""

    def apply_chat_template(self, messages, *, tokenize=False,
                            add_generation_prompt=False, enable_thinking=_UNSET,
                            **kwargs):
        if enable_thinking is not _UNSET:
            raise TypeError("unexpected keyword argument 'enable_thinking'")
        return super().apply_chat_template(
            messages, tokenize=tokenize, add_generation_prompt=add_generation_prompt,
            **kwargs)


def _dataset(fixtures=FIXTURES) -> ConvertedDataset:
    records = tuple(
        ConvertedRecord(row_index=i, candidate_id=f"cand-synthetic-{i:02d}",
                        prompt_messages=prompt, completion=target)
        for i, (_name, prompt, target) in enumerate(fixtures))
    return ConvertedDataset(records=records, export_sha256="0" * 64,
                            max_sequence_length=512)


def _eval_render(tokenizer, prompt_messages, policy: ReasoningPolicy, *, tokenize):
    """The EVALUATION entry point's render call, built from its own policy object.

    Not a paraphrase: :func:`test_the_evaluation_backend_uses_the_shared_mapping` asserts
    over the backend's AST that this is the call it makes.
    """
    kwargs = reasoning_template_kwargs(policy)
    return tokenizer.apply_chat_template(list(prompt_messages), tokenize=tokenize,
                                         add_generation_prompt=True, **kwargs)


# ══════════════════════════════════════════════════════════════════════════════════
#  1-2. the defect, and its closure, at the byte level
# ══════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name,prompt,target", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_legacy_training_render_differs_from_evaluation_disabled(name, prompt, target):
    """§29.1 — the pre-fix state, reproduced on a pinned synthetic fixture."""
    tokenizer = ReviewedTemplateTokenizer()
    legacy = tokenizer.apply_chat_template(list(prompt), tokenize=False,
                                           add_generation_prompt=True)
    disabled = _eval_render(tokenizer, prompt, ReasoningPolicy.DISABLED, tokenize=False)

    assert legacy != disabled, "D37 does not reproduce; the fixture proves nothing"
    assert disabled == legacy + EMPTY_REASONING_SEQUENCE
    assert len(disabled) - len(legacy) == 19
    assert not legacy.endswith(EMPTY_REASONING_SEQUENCE)


@pytest.mark.parametrize("name,prompt,target", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_explicit_training_disabled_render_equals_evaluation_disabled(name, prompt,
                                                                      target):
    """§29.2 / §31 — the prefix-parity proof, in bytes."""
    tokenizer = ReviewedTemplateTokenizer()
    kwargs = reasoning_template_kwargs(ReasoningPolicy.DISABLED)
    training = tokenizer.apply_chat_template(list(prompt), tokenize=False,
                                             add_generation_prompt=True, **kwargs)
    evaluation = _eval_render(tokenizer, prompt, ReasoningPolicy.DISABLED,
                              tokenize=False)
    assert training == evaluation


@pytest.mark.parametrize("name,prompt,target", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_token_ids_are_equal_when_messages_and_policy_are_equal(name, prompt, target):
    """§29.3 / §31 — the same proof at the token level. Bytes equal is not enough."""
    tokenizer = ReviewedTemplateTokenizer()
    kwargs = reasoning_template_kwargs(ReasoningPolicy.DISABLED)
    training = tokenizer.apply_chat_template(list(prompt), tokenize=True,
                                             add_generation_prompt=True, **kwargs)
    evaluation = _eval_render(tokenizer, prompt, ReasoningPolicy.DISABLED, tokenize=True)
    assert training == evaluation

    legacy = tokenizer.apply_chat_template(list(prompt), tokenize=True,
                                           add_generation_prompt=True)
    assert legacy != evaluation, "the token-level comparison is vacuous"
    # A pure suffix extension: everything the legacy prompt had is still there.
    assert evaluation[:len(legacy)] == legacy
    assert evaluation[len(legacy):] == [151667, 271, 151668, 271]


def test_model_default_and_enabled_render_identically_disabled_does_not():
    """The S3F.2 addendum's shape: the template default IS thinking-on."""
    tokenizer = ReviewedTemplateTokenizer()
    _n, prompt, _t = FIXTURES[0]
    default = tokenizer.apply_chat_template(list(prompt), tokenize=False,
                                            add_generation_prompt=True)
    enabled = _eval_render(tokenizer, prompt, ReasoningPolicy.ENABLED, tokenize=False)
    disabled = _eval_render(tokenizer, prompt, ReasoningPolicy.DISABLED, tokenize=False)
    assert default == enabled
    assert default != disabled


# ══════════════════════════════════════════════════════════════════════════════════
#  30. the cross-path parity test, with the comparable region defined correctly
# ══════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name,prompt,target", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_cross_path_parity_compares_the_prefix_not_prompt_plus_answer(name, prompt,
                                                                      target):
    """§30 — training renders prompt+answer, evaluation renders prompt only.

    Comparing those two directly would be a test that can only fail, and passing it
    would mean the training corpus had lost its targets. The comparable region is the
    training render's PREFIX up to the supervised assistant answer — which is exactly
    the ``prompt_ids`` the backend builds and masks against.
    """
    tokenizer = ReviewedTemplateTokenizer()
    backend = train_backend.TransformersPeftBackend()
    rows, truncated = backend._encode(  # noqa: SLF001 - the production encoder
        _dataset(((name, prompt, target),)), tokenizer=tokenizer, max_length=512,
        reasoning_policy=ReasoningPolicy.DISABLED)
    assert truncated == 0
    row = rows[0]

    train_prefix = row["input_ids"][:row["prompt_length"]]
    eval_prompt = _eval_render(tokenizer, prompt, ReasoningPolicy.DISABLED,
                               tokenize=True)
    assert train_prefix == eval_prompt

    full = row["input_ids"]
    assert len(full) > len(train_prefix), (
        "the training row carries no supervised answer; the comparison would be between "
        "two prompts and would prove nothing about training")


@pytest.mark.parametrize("name,prompt,target", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_cross_path_parity_FAILS_under_the_legacy_training_policy(name, prompt, target):
    """The same comparison before the fix. Non-vacuous in the other direction."""
    tokenizer = ReviewedTemplateTokenizer()
    backend = train_backend.TransformersPeftBackend()
    rows, _ = backend._encode(  # noqa: SLF001
        _dataset(((name, prompt, target),)), tokenizer=tokenizer, max_length=512,
        reasoning_policy=ReasoningPolicy.MODEL_DEFAULT)
    train_prefix = rows[0]["input_ids"][:rows[0]["prompt_length"]]
    eval_prompt = _eval_render(tokenizer, prompt, ReasoningPolicy.DISABLED,
                               tokenize=True)
    assert train_prefix != eval_prompt
    assert eval_prompt[:len(train_prefix)] == train_prefix


# ══════════════════════════════════════════════════════════════════════════════════
#  15-16, 32-33. the supervised span: target, brace, terminator, thinking markup
# ══════════════════════════════════════════════════════════════════════════════════
def _encoded(policy: ReasoningPolicy, fixtures=FIXTURES):
    tokenizer = ReviewedTemplateTokenizer()
    backend = train_backend.TransformersPeftBackend()
    rows, truncated = backend._encode(  # noqa: SLF001
        _dataset(fixtures), tokenizer=tokenizer, max_length=512,
        reasoning_policy=policy)
    return tokenizer, rows, truncated


def _supervised(row) -> list[int]:
    return [t for t in row["labels"] if t != LOSS_IGNORE_INDEX]


def _decode(tokenizer, ids) -> str:
    reverse = {v: k for k, v in tokenizer.SPECIALS.items()}
    return "".join(reverse.get(i, chr(i)) for i in ids)


@pytest.mark.parametrize("index,fixture", list(enumerate(FIXTURES)),
                         ids=[f[0] for f in FIXTURES])
def test_the_full_supervised_sequence_is_identical_under_both_policies(index, fixture):
    """§15 — ``TARGET_BYTES_CHANGE: NO``. Only the mask boundary moves."""
    _tok_a, rows_legacy, _ = _encoded(ReasoningPolicy.MODEL_DEFAULT)
    _tok_b, rows_disabled, _ = _encoded(ReasoningPolicy.DISABLED)
    assert rows_legacy[index]["input_ids"] == rows_disabled[index]["input_ids"]
    assert (rows_disabled[index]["prompt_length"]
            == rows_legacy[index]["prompt_length"] + 4)


@pytest.mark.parametrize("index,fixture", list(enumerate(FIXTURES)),
                         ids=[f[0] for f in FIXTURES])
def test_assistant_target_content_is_preserved_exactly(index, fixture):
    """§29.6 — the authored answer, and only the authored answer, is supervised."""
    tokenizer, rows, _ = _encoded(ReasoningPolicy.DISABLED)
    decoded = _decode(tokenizer, _supervised(rows[index]))
    assert decoded == fixture[2] + IM_END + "\n"


@pytest.mark.parametrize("index,fixture", list(enumerate(FIXTURES)),
                         ids=[f[0] for f in FIXTURES])
def test_no_reasoning_markup_is_supervised_under_DISABLED(index, fixture):
    """§33 / §29.10 — the thinking-markup proof, stated exactly rather than vaguely.

    The reviewed template DOES emit an empty reasoning-control sequence in front of the
    final assistant message, unconditionally. Under the legacy implicit default it fell
    on the supervised side of the mask, so training taught the model to emit
    ``<think>\\n\\n</think>\\n\\n`` before every answer. Under ``DISABLED`` it is inside
    the prompt prefix — which is exactly where the evaluation prompt also puts it — and
    is masked.
    """
    tokenizer, rows, _ = _encoded(ReasoningPolicy.DISABLED)
    decoded = _decode(tokenizer, _supervised(rows[index]))
    assert "<think>" not in decoded
    assert "</think>" not in decoded

    _t2, legacy_rows, _ = _encoded(ReasoningPolicy.MODEL_DEFAULT)
    legacy_decoded = _decode(tokenizer, _supervised(legacy_rows[index]))
    assert legacy_decoded.startswith(EMPTY_REASONING_SEQUENCE), (
        "the legacy comparison is vacuous; the whole point is that the empty reasoning "
        "sequence WAS supervised before D37 was closed")


@pytest.mark.parametrize("index,fixture", list(enumerate(FIXTURES)),
                         ids=[f[0] for f in FIXTURES])
def test_the_turn_terminator_is_supervised_and_is_the_assistant_terminator(index,
                                                                          fixture):
    """§32 / §29.8 — the terminator-parity proof. No generation is performed."""
    tokenizer, rows, _ = _encoded(ReasoningPolicy.DISABLED)
    supervised = _supervised(rows[index])
    expected = tokenizer.SPECIALS[IM_END]
    assert tokenizer.eos_token == IM_END
    assert expected in supervised, "the model is never taught to end its turn"
    assert supervised[-2] == expected
    assert supervised[-1] == tokenizer.SPECIALS["\n"]


@pytest.mark.parametrize("index,fixture",
                         [(2, FIXTURES[2]), (4, FIXTURES[4])],
                         ids=["strict_json_user_only", "system_user_json"])
def test_the_closing_json_brace_is_supervised(index, fixture):
    """§29.7 — a structured target whose closing brace is masked teaches truncation."""
    tokenizer, rows, _ = _encoded(ReasoningPolicy.DISABLED)
    decoded = _decode(tokenizer, _supervised(rows[index]))
    assert decoded.startswith("{")
    assert decoded[:-len(IM_END + "\n")].endswith("}")
    json.loads(decoded[:-len(IM_END + "\n")])


@pytest.mark.parametrize("policy", [ReasoningPolicy.MODEL_DEFAULT,
                                    ReasoningPolicy.DISABLED])
def test_the_prompt_prefix_is_masked_and_the_self_test_agrees(policy):
    """§16 / §29.9 — masking parity, through the production self-test."""
    _tok, rows, truncated = _encoded(policy)
    assert truncated == 0
    for row in rows:
        assert all(t == LOSS_IGNORE_INDEX for t in row["labels"][:row["prompt_length"]])
        assert check_masking(row["labels"], row["prompt_length"]) == ()
        assert row["input_ids"][:row["prompt_length"]] == [
            t for t in row["input_ids"][:row["prompt_length"]]]
    evidence = train_backend.TransformersPeftBackend()._masking_self_test(rows)  # noqa: SLF001
    assert evidence.verified is True
    assert list(evidence.problems) == []


def test_binding_the_policy_introduces_no_truncation():
    """§29 / C7 — the full sequence does not grow, so nothing new can be cut."""
    _a, legacy, legacy_truncated = _encoded(ReasoningPolicy.MODEL_DEFAULT)
    _b, disabled, disabled_truncated = _encoded(ReasoningPolicy.DISABLED)
    assert legacy_truncated == disabled_truncated == 0
    assert ([len(r["input_ids"]) for r in legacy]
            == [len(r["input_ids"]) for r in disabled])


def test_build_labels_still_refuses_a_prompt_that_is_not_a_prefix():
    """The boundary guard is not weakened by the new keyword."""
    with pytest.raises(Exception):
        build_labels([1, 2, 3], [9, 9, 9, 4])


# ══════════════════════════════════════════════════════════════════════════════════
#  4-5, 47. the render-policy identity
# ══════════════════════════════════════════════════════════════════════════════════
def _policy(**overrides) -> ChatRenderPolicy:
    base = {"tokenizer_id": "Qwen/Qwen3-0.6B",
            "tokenizer_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
            "chat_template_hash": PINNED_CHAT_TEMPLATE_DIGEST,
            "reasoning_policy": ReasoningPolicy.DISABLED,
            "add_generation_prompt": True}
    base.update(overrides)
    return ChatRenderPolicy(**base)


def test_same_semantics_same_tokenizer_same_template_gives_the_same_hash():
    """§47 — determinism, and it must not depend on the object's construction site."""
    assert _policy().render_policy_hash() == _policy().render_policy_hash()


def test_a_different_reasoning_policy_gives_a_different_hash():
    """§29.4 — implicit/default and explicitly DISABLED are distinguishable."""
    digests = {p: _policy(reasoning_policy=p).render_policy_hash()
               for p in ReasoningPolicy}
    assert len(set(digests.values())) == 3


def test_a_different_add_generation_prompt_value_gives_a_different_hash():
    """§47 — the prompt prefix and the full sequence are two different calls."""
    assert (_policy(add_generation_prompt=True).render_policy_hash()
            != _policy(add_generation_prompt=False).render_policy_hash())


def test_training_disabled_and_evaluation_disabled_share_one_render_identity():
    """§29.5 — the machine-checkable statement of parity.

    The training side builds its policy from a ``TrainingConfig``; the evaluation side's
    is built from ``eligibility_generation_policy()``. Nothing is copied between them.
    """
    config = _config(reasoning_policy=ReasoningPolicy.DISABLED)
    training = config.chat_render_policy(
        chat_template_hash=PINNED_CHAT_TEMPLATE_DIGEST, add_generation_prompt=True)
    evaluation = ChatRenderPolicy(
        tokenizer_id=config.tokenizer_id,
        tokenizer_revision=config.tokenizer_revision,
        chat_template_hash=PINNED_CHAT_TEMPLATE_DIGEST,
        reasoning_policy=eval_generation.eligibility_generation_policy(
            seed=11).reasoning_policy,
        add_generation_prompt=True)
    assert training.render_policy_hash() == evaluation.render_policy_hash()

    legacy = _config().chat_render_policy(
        chat_template_hash=PINNED_CHAT_TEMPLATE_DIGEST, add_generation_prompt=True)
    assert legacy.render_policy_hash() != evaluation.render_policy_hash()


def test_the_render_identity_binds_no_host_state():
    """§24 / §47 — remember D34 and D36. An identity is not a function of the host.

    Asserted over the field list rather than by trying a few paths, so a future field
    that smuggles environment state in fails here rather than at a rebuild months later.
    """
    allowed = {"policy_version", "tokenizer_id", "tokenizer_revision",
               "chat_template_hash", "reasoning_policy", "enable_thinking",
               "add_generation_prompt", "tokenize"}
    assert set(_policy().to_dict()) == allowed
    forbidden = ("path", "root", "cache", "home", "host", "user", "dir", "time",
                 "ram", "disk", "memory", "seed", "device")
    for key in _policy().to_dict():
        assert not any(word in key for word in forbidden), key
    source = inspect.getsource(chat_render)
    assert "os.environ" not in source
    assert "Path(" not in source


def test_the_render_identity_carries_the_library_keyword_alongside_the_policy():
    """``null`` (not passed) and ``false`` (passed) are different calls."""
    assert _policy(reasoning_policy=ReasoningPolicy.MODEL_DEFAULT).to_dict()[
        "enable_thinking"] is None
    assert _policy(reasoning_policy=ReasoningPolicy.DISABLED).to_dict()[
        "enable_thinking"] is False
    assert _policy(reasoning_policy=ReasoningPolicy.ENABLED).to_dict()[
        "enable_thinking"] is True
    assert _policy().to_dict()["policy_version"] == CHAT_RENDER_POLICY_VERSION


def test_an_empty_template_digest_is_refused():
    """Two unrelated calls must not share a digest because neither named a template."""
    with pytest.raises(ChatRenderError):
        _policy(chat_template_hash="   ")


# ══════════════════════════════════════════════════════════════════════════════════
#  11-14. configuration identity and backward compatibility
# ══════════════════════════════════════════════════════════════════════════════════
def _config(**overrides) -> TrainingConfig:
    base = dict(
        run_id="synthetic-d37-probe", experiment_name="d37 parity probe",
        method=TrainingMethod.SFT_LORA, base_model_id="Qwen/Qwen3-0.6B",
        base_model_revision="c1899de289a04d12100db370d81485cdf75e47ca",
        base_model_parameters_b=0.6, tokenizer_id="Qwen/Qwen3-0.6B",
        tokenizer_revision="c1899de289a04d12100db370d81485cdf75e47ca",
        dataset_reference=TrainingDatasetReference.placeholder(),
        output_root_id="synthetic-root", created_at_utc="2026-08-15T00:00:00Z",
        seed=42, epochs=2, max_steps=40, lora=LoRAConfig(),
        logging_target=LoggingTarget.NONE,
        validation_strategy=ValidationStrategy.NO)
    base.update(overrides)
    return TrainingConfig(**base)


def test_a_config_that_names_no_policy_hashes_exactly_as_it_always_did():
    """§29.11 / C10 — the value gate. This is what protects candidate 001 and 002."""
    legacy = _config()
    assert legacy.reasoning_policy is ReasoningPolicy.MODEL_DEFAULT
    assert "reasoning_policy" not in legacy.to_dict()


def test_a_historical_config_document_parses_to_the_legacy_semantic_state():
    """§45 — absent is LEGACY_IMPLICIT_TEMPLATE_DEFAULT, never DISABLED."""
    document = _config().to_dict()
    assert "reasoning_policy" not in document
    reloaded = TrainingConfig.from_dict(document)
    assert reloaded.reasoning_policy is ReasoningPolicy.MODEL_DEFAULT
    assert reloaded.config_hash() == _config().config_hash()


def test_binding_the_policy_moves_the_config_identity():
    """§29.13 / §46 — expected, and required: the run renders different bytes."""
    legacy = _config()
    bound = _config(reasoning_policy=ReasoningPolicy.DISABLED)
    assert bound.to_dict()["reasoning_policy"] == "disabled"
    assert bound.config_hash() != legacy.config_hash()
    assert TrainingConfig.from_dict(bound.to_dict()).reasoning_policy is (
        ReasoningPolicy.DISABLED)


def test_an_unknown_reasoning_policy_value_fails_closed():
    with pytest.raises(SchemaError):
        _config(reasoning_policy="sometimes")


def test_the_config_schema_version_is_unchanged():
    """The major prefix gates compatibility; absent means legacy, unknown still fails.

    ``m62.training_config.1`` does not move, for the same reason S3G.2 did not move it
    when it added ``validation_strategy``: a config written before the field existed
    still parses, still means what it always meant, and still hashes to what it always
    hashed to. A misspelt key is still refused rather than silently defaulted.
    """
    from training_gym.training.config import TRAINING_SCHEMA_VERSION

    assert TRAINING_SCHEMA_VERSION == "m62.training_config.1"
    with pytest.raises(SchemaError, match="unknown field"):
        TrainingConfig.from_dict({**_config().to_dict(), "reasoning": "disabled"})


def test_the_plan_hyperparameters_are_value_gated_the_same_way():
    """§29.14 — a plan that binds a policy says so; one that does not is unchanged."""
    from training_gym.training import planner

    source = inspect.getsource(planner)
    tree = ast.parse(source)
    gated = [n for n in ast.walk(tree)
             if isinstance(n, ast.IfExp)
             and "reasoning_policy" in ast.unparse(n)]
    assert gated, "reasoning_policy reaches the plan unconditionally"
    assert any("is_explicit" in ast.unparse(n) for n in gated)


def test_a_historical_adapter_manifest_is_not_rewritten():
    """§29.12 / §22 — an empty render digest is absent from the canonical body."""
    manifest = _manifest()
    assert manifest.chat_render_policy_hash == ""
    assert "chat_render_policy_hash" not in manifest.to_dict()
    before = manifest.manifest_hash()

    bound = _manifest(chat_render_policy_hash=_policy().render_policy_hash())
    assert "chat_render_policy_hash" in bound.to_dict()
    assert bound.manifest_hash() != before
    assert AdapterManifest.from_dict(
        bound.to_record()).chat_render_policy_hash == _policy().render_policy_hash()


def test_a_truncated_render_digest_is_refused_in_a_manifest():
    from training_gym.training.artifacts import AdapterArtifactError

    with pytest.raises(AdapterArtifactError):
        _manifest(chat_render_policy_hash="abc123")


def _manifest(**overrides) -> AdapterManifest:
    base = dict(
        run_id="synthetic-d37-probe", run_state="completed", plan_hash="a" * 64,
        training_config_hash="b" * 64, method="sft_lora",
        backend_id="transformers_peft", backend_version="m62.transformers_peft.1",
        base_model_id="Qwen/Qwen3-0.6B",
        base_model_revision="c1899de289a04d12100db370d81485cdf75e47ca",
        base_model_identity_hash="c" * 64, tokenizer_id="Qwen/Qwen3-0.6B",
        tokenizer_revision="c1899de289a04d12100db370d81485cdf75e47ca",
        tokenizer_identity_hash="d" * 64,
        tokenizer_chat_template_hash=PINNED_CHAT_TEMPLATE_DIGEST,
        dataset_id="synthetic", dataset_version="v1", dataset_reference_hash="e" * 64,
        dataset_manifest_hash="f" * 64, train_shard_hash="0" * 64,
        validation_shard_hash="1" * 64, hidden_evaluation_hash="2" * 64,
        security_regression_hash="3" * 64, export_manifest_hash="4" * 64,
        split_algorithm_version="m62.dataset.1", seed=42, lora={"rank": 16},
        assistant_only_loss={"verified": True}, package_versions={"torch": "2.13.0"},
        device_category="cpu", precision="fp32", requested_steps=40,
        completed_steps=40, epochs_completed=2.0, train_loss=3.0, eval_loss=3.0,
        truncated_records=0, duration_seconds=1.0,
        files=(ArtifactFile(name="adapter_model.safetensors", sha256="5" * 64,
                            size_bytes=1),),
        total_bytes=1, tree_hash="6" * 64, created_at_utc="2026-08-15T00:00:00Z")
    base.update(overrides)
    return AdapterManifest(**base)


# ══════════════════════════════════════════════════════════════════════════════════
#  the honour check, and the single shared implementation
# ══════════════════════════════════════════════════════════════════════════════════
def test_a_template_that_ignores_the_keyword_does_not_honour_the_policy():
    probe = [{"role": "user", "content": "probe"}]
    assert template_honours_reasoning_policy(ReviewedTemplateTokenizer(), probe) is True
    assert template_honours_reasoning_policy(
        ReviewedTemplateTokenizer(honours_thinking=False), probe) is False
    assert template_honours_reasoning_policy(RefusingTokenizer(), probe) is False


def test_both_subsystems_share_one_reasoning_policy_enum():
    """§44 — one closed set, not two that happen to spell the same values."""
    assert eval_generation.ReasoningPolicy is ReasoningPolicy
    from training_gym.training.config import ReasoningPolicy as ConfigRP

    assert ConfigRP is ReasoningPolicy
    assert eval_generation.ELIGIBILITY_REASONING_POLICY is ReasoningPolicy.DISABLED
    assert TRAIN_EVAL_PARITY_REASONING_POLICY is (
        eval_generation.ELIGIBILITY_REASONING_POLICY)


def test_both_subsystems_share_one_honour_check():
    """No copy/paste fork: the evaluation backend delegates to the shared authority."""
    assert eval_backend.template_honours_reasoning_policy is (
        template_honours_reasoning_policy)
    source = inspect.getsource(eval_backend._template_honours_thinking)  # noqa: SLF001
    assert "template_honours_reasoning_policy" in source
    assert "apply_chat_template" not in source


def test_the_evaluation_backend_uses_the_shared_mapping_and_stays_explicit():
    """§20 / §49 — the fix is train-side. Evaluation must not become implicit."""
    source = inspect.getsource(eval_backend)
    assert 'template_kwargs["enable_thinking"] = thinking' in source
    assert "policy.reasoning_policy.template_kwarg" in source
    assert "add_generation_prompt=True" in source
    assert eval_generation.eligibility_generation_policy(
        seed=11).reasoning_policy is ReasoningPolicy.DISABLED


def test_the_training_backend_refuses_a_policy_the_template_would_ignore():
    """A recorded setting that had no effect is worse than a refusal (D26a's rule)."""
    source = inspect.getsource(train_backend)
    assert "template_honours_reasoning_policy" in source
    assert "is_explicit" in source
    tree = ast.parse(source)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and ast.unparse(n.func).endswith("apply_chat_template")]
    assert len(calls) == 2, "the training backend's render call sites moved"
    for call in calls:
        rendered = ast.unparse(call)
        assert "template_kwargs" in rendered, (
            "a training render call does not carry the reasoning policy; the two halves "
            "of the prefix comparison would be rendered under different rules")


def test_the_generation_policy_digest_did_not_move():
    """§49 — the sealed ``generation_policy_hash`` both live runs carry is untouched.

    The enum's DEFINITION moved between modules; its member VALUES did not, and the
    values are what the digest is taken over.
    """
    sealed = eval_generation.GenerationPolicy(
        seed=11, max_new_tokens=512, max_input_tokens=4096, timeout_s=300,
        reasoning_policy=ReasoningPolicy.DISABLED,
        device_policy=eval_generation.DevicePolicy.CPU,
        precision_policy=eval_generation.PrecisionPolicy.FP32)
    assert sealed.policy_hash() == (
        "c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7")


def test_the_gate_policy_digest_did_not_move():
    """No threshold moved in a milestone that touched training rendering."""
    from training_gym.evaluation.gates import GatePolicy

    assert GatePolicy().policy_hash() == (
        "e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5")


# ══════════════════════════════════════════════════════════════════════════════════
#  16. no held-out material, and the optional real-tokenizer reproduction
# ══════════════════════════════════════════════════════════════════════════════════
def test_this_file_contains_no_held_out_task_material():
    """§29.16 / PROGRESS §18 — the four diagnostic ids stay diagnostic history.

    Assembled from fragments so that asserting their absence does not itself put them in
    the file. The prose above deliberately does not spell them either.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    for prefix, suffix in (("adv-", "report-03"), ("he-", "report-04"),
                           ("he3-", "report-01"), ("he3-", "report-04")):
        assert prefix + suffix not in source


def _reviewed_template() -> str | None:
    """The reviewed template, if this host can reach it, and only if it verifies.

    Never searches the filesystem: it looks at one env var and one repository-relative
    runtime path, and it refuses anything whose digest is not the pinned one. Absent is
    a skip, never a silent pass.
    """
    import hashlib
    import os

    from training_gym.training.dataset_conversion import chat_template_hash

    candidates = []
    named = os.environ.get("M62_REVIEWED_CHAT_TEMPLATE")
    if named:
        candidates.append(Path(named))
    repo = Path(__file__).resolve().parents[1]
    candidates.append(repo / "training_runs" / "quarantine"
                      / "qwen3-06b-lora-smoke-live-003-7e9b2593" / "checkpoint-4"
                      / "chat_template.jinja")
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", newline=None)
        except OSError:
            continue
        if chat_template_hash(text) == PINNED_CHAT_TEMPLATE_DIGEST:
            return text
        assert hashlib.sha256(text.encode()).hexdigest() != ""  # keeps the import used
    return None


def test_reviewed_tokenizer_reproduces_every_claim():
    """The same assertions against the REAL reviewed template, when it is reachable.

    Skipped rather than faked when it is not: the reviewed cache is an operator-supplied
    artefact and no test may require one. The measurement of record is in
    ``jarvis/docs/V69_M62_S3M1_D37_TEMPLATE_PARITY_QUALIFICATION.md``.
    """
    template = _reviewed_template()
    if template is None:
        pytest.skip("the reviewed chat template is not reachable from this host")
    jinja2 = pytest.importorskip("jinja2")

    env = jinja2.Environment(trim_blocks=False, lstrip_blocks=False,
                             autoescape=False)  # noqa: S701 - rendering a chat template
    compiled = env.from_string(template)

    for _name, prompt, target in FIXTURES:
        messages = [dict(m) for m in prompt]
        full = messages + [{"role": "assistant", "content": target}]
        default = compiled.render(messages=messages, add_generation_prompt=True)
        disabled = compiled.render(messages=messages, add_generation_prompt=True,
                                   enable_thinking=False)
        enabled = compiled.render(messages=messages, add_generation_prompt=True,
                                  enable_thinking=True)
        assert default == enabled
        assert disabled == default + EMPTY_REASONING_SEQUENCE
        assert len(disabled) - len(default) == 19

        full_default = compiled.render(messages=full, add_generation_prompt=False)
        full_disabled = compiled.render(messages=full, add_generation_prompt=False,
                                        enable_thinking=False)
        assert full_default == full_disabled, (
            "the reviewed template's full-sequence render is NOT invariant to the "
            "keyword; the supervised-span analysis must be re-taken")
        assert full_default.startswith(disabled)
        assert full_default[len(disabled):] == target + IM_END + "\n"
        assert not full_default[len(disabled):].startswith("<think>")
