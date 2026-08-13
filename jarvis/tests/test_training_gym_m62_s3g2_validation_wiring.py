"""V69 M62 S3G.2 — D31: the VALIDATION split reached the trainer as nothing at all.

THE DEFECT
----------
``m62-defensive-quality-train v1`` holds nine deterministically-assigned VALIDATION rows.
They were promoted, bound into the training plan by digest
(``validation_split_manifest_hash`` / ``validation_shard_hash``) and length-audited
against the real tokenizer in S3G.1 — and then read by nothing:

  * ``training_gym.datasets.export`` could only write the TRAIN split, so no file
    containing the validation rows existed in a shape a trainer could read;
  * ``training_gym.training.execution`` constructed every ``ExecutionRequest`` with
    ``validation_file=None``, hard-coded, although the field had existed since S3B;
  * ``TransformersPeftBackend._train`` built ``Trainer(..., train_dataset=rows)`` with no
    ``eval_dataset``.

So a run reported ``eval_loss`` of exactly ``0.0`` — the dataclass default — and the split
contributed nothing to any measurement.

WHAT THESE TESTS PIN
--------------------
That the rows now reach ``eval_dataset``; that TRAIN and VALIDATION stay disjoint in both
directions; that the four held-out splits reach the trainer through neither arm; that the
eval rows are encoded by the SAME production encoder, tokenizer and cap as the training
rows; that no eval loss curve is allowed to decide when a run stops or which weights are
saved; and that turning validation on MOVES the config and plan identity while turning it
off reproduces the pre-S3G.2 identity byte for byte.

The last pair is the point of the value-gated canonical form, and a one-sided test would
pass on a fix that either froze the identity (making two different runs share one) or
moved it unconditionally (re-identifying every configuration ever written).

NO MODEL IS LOADED HERE. The framework surface ``_train`` touches is faked, deliberately:
the properties under test are properties of the WIRING — which object receives which rows,
under which arguments — and a real 0.6B forward pass would establish none of them while
costing minutes per test.
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_training_gym_m62_training_dataset_binding import (  # noqa: E402
    DATASET_ID,
    VERSION,
    Corpus,
)

from training_gym.datasets.candidate import DatasetSplit  # noqa: E402
from training_gym.datasets.export import (  # noqa: E402
    EXPORTABLE_SPLITS,
    SFT_FILENAME,
    SFT_VALIDATION_FILENAME,
    SFT_VALIDATION_MANIFEST_FILENAME,
    ExportError,
    SFTExportManifest,
    export_sft_validation,
    verify_sft_validation_export,
)
from training_gym.datasets.manifests import (  # noqa: E402
    RevocationSnapshot,
    read_shard,
    shard_filename,
)
from training_gym.schemas import SchemaError  # noqa: E402
from training_gym.training.backend import ExecutionRequest  # noqa: E402
from training_gym.training.backends.transformers_peft import (  # noqa: E402
    TransformersPeftBackend,
    _finite,
)
from training_gym.training.config import (  # noqa: E402
    SUPPORTED_VALIDATION_STRATEGIES,
    CheckpointStrategy,
    TrainingConfig,
    ValidationStrategy,
)
from training_gym.training.dataset_conversion import convert_sft_export  # noqa: E402

NOW = "2026-08-13T00:00:00Z"


def _executable_source(module: str) -> str:
    """The module's code with every docstring and comment removed.

    Needed because these files EXPLAIN what they must never do — "held-out eligibility
    material is ``m62-defensive-eval``", "no ``EarlyStoppingCallback``" — so a plain
    substring search over the source finds the prohibition and reads it as the violation.
    ``ast.unparse`` drops comments; the docstrings are stripped explicitly.
    """
    import ast

    tree = ast.parse(Path(module).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
    return ast.unparse(ast.fix_missing_locations(tree))


# ══════════════════════════════════════════════════════════════════════════════
#  A corpus with BOTH exports
# ══════════════════════════════════════════════════════════════════════════════
class ValidatedCorpus(Corpus):
    """The binding fixture's corpus, plus the validation export S3G.2 adds."""

    def __init__(self, root, export_root) -> None:
        super().__init__(root, export_root)
        self.validation_export = export_sft_validation(
            root=root, dataset_id=DATASET_ID, dataset_version=VERSION,
            revocation=RevocationSnapshot(), created_at_utc=NOW, out_root=export_root)

    @property
    def train_file(self) -> Path:
        return self.export_directory / SFT_FILENAME

    @property
    def validation_file(self) -> Path:
        return self.export_directory / SFT_VALIDATION_FILENAME

    def ids_in(self, split: DatasetSplit) -> set[str]:
        return {c.candidate_id
                for c in read_shard(self.version_directory / shard_filename(split))}

    @property
    def held_out_ids(self) -> set[str]:
        return (self.ids_in(DatasetSplit.HIDDEN_EVALUATION)
                | self.ids_in(DatasetSplit.SECURITY_REGRESSION))


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> ValidatedCorpus:
    base = tmp_path_factory.mktemp("s3g2")
    return ValidatedCorpus(base / "store", base / "out")


@pytest.fixture()
def scratch(tmp_path) -> ValidatedCorpus:
    """A private corpus for the tests that write or tamper."""
    return ValidatedCorpus(tmp_path / "store", tmp_path / "out")


def _config(corpus: ValidatedCorpus, **overrides) -> TrainingConfig:
    """The quality candidate's shape: validation per epoch, no checkpoints, fp32 CPU."""
    base = {"validation_strategy": ValidationStrategy.EPOCH,
            "gradient_checkpointing": False,
            "checkpoint_strategy": CheckpointStrategy.NO}
    base.update(overrides)
    return corpus.config(**base)


# ══════════════════════════════════════════════════════════════════════════════
#  A faked framework surface — see the module docstring on why
# ══════════════════════════════════════════════════════════════════════════════
class _Tokenizer:
    """Renders a message list to ids such that the prompt IS a prefix of the whole.

    Not a stand-in for Qwen3's template: it is the minimum that satisfies
    ``build_labels``, which is what makes the masking self-test meaningful rather than
    vacuous. One token per character keeps every length assertion arithmetic.
    """

    chat_template = "{{ messages }}"
    pad_token = None
    eos_token = "<eos>"

    #: Role markers and a turn terminator, so that the generation prompt a rendered
    #: PROMPT ends with is the same marker the assistant turn of the FULL rendering
    #: opens with. That is the property real chat templates have and the property
    #: ``build_labels`` requires; a fake without it fails for the wrong reason.
    _ROLE = {"system": 1, "user": 2, "assistant": 3}
    _END = 4

    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
        assert tokenize is True
        ids: list[int] = []
        for message in messages:
            ids.append(self._ROLE[message["role"]])
            ids.extend(ord(c) % 251 + 5 for c in message["content"])
            ids.append(self._END)
        if add_generation_prompt:
            ids.append(self._ROLE["assistant"])
        return ids


class _Parameter:
    def __init__(self, count: int, *, trainable: bool) -> None:
        self._count, self.requires_grad = count, trainable

    def numel(self) -> int:
        return self._count


class _ModelConfig:
    use_cache = True


class _Model:
    def __init__(self) -> None:
        self.saved_with: dict = {}
        self.config = _ModelConfig()
        self.gradient_checkpointing = False

    def gradient_checkpointing_enable(self) -> None:
        self.gradient_checkpointing = True

    def parameters(self):
        return [_Parameter(1_000_000, trainable=False),
                _Parameter(10_000, trainable=True)]

    def save_pretrained(self, path, **kwargs) -> None:
        self.saved_with = {"path": str(path), **kwargs}
        Path(path).mkdir(parents=True, exist_ok=True)
        (Path(path) / "adapter_config.json").write_text("{}", encoding="utf-8")


class _State:
    def __init__(self, log_history) -> None:
        self.global_step, self.epoch = 3, 3.0
        self.log_history = log_history


class _CallbackHandler:
    def __init__(self) -> None:
        self.callbacks: list = []


class _Trainer:
    """Captures what it was constructed with. Trains nothing."""

    last: "_Trainer | None" = None
    log_history: list = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.model = kwargs["model"]
        self.state = _State(list(type(self).log_history))
        self.callback_handler = _CallbackHandler()
        type(self).last = self

    #: What the closing ``trainer.evaluate()`` returns. Distinct from the last periodic
    #: value on purpose, so a test can tell which number the record kept where.
    closing_metrics: dict = {"eval_loss": 1.2, "epoch": 2.99, "eval_runtime": 0.4}

    def train(self):
        class _Output:
            metrics = {"train_loss": 1.5, "train_runtime": 4.0}

        return _Output()

    def evaluate(self, *_a, **_k):
        metrics = dict(type(self).closing_metrics)
        self.state.log_history.append(dict(metrics))
        self.evaluated = True
        return metrics


class _Arguments:
    last: "_Arguments | None" = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        type(self).last = self


class _Transformers:
    TrainingArguments = _Arguments
    Trainer = _Trainer

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(*_a, **_k):
            return _Tokenizer()

    class AutoModelForCausalLM:
        @staticmethod
        def from_pretrained(*_a, **_k):
            return _Model()

    @staticmethod
    def DataCollatorForSeq2Seq(*_a, **_k):  # noqa: N802 — mirrors the real name
        return object()

    @staticmethod
    def set_seed(_seed):
        return None


class _Peft:
    @staticmethod
    def LoraConfig(**kwargs):  # noqa: N802 — mirrors the real name
        return kwargs

    @staticmethod
    def get_peft_model(model, _config):
        return model


class _Torch:
    float32 = "float32"
    bfloat16 = "bfloat16"
    float16 = "float16"


@pytest.fixture()
def faked_runtime(monkeypatch):
    """Replace ``_runtime`` only. Every line of ``_train`` under test still runs."""
    import training_gym.training.backends.transformers_peft as production

    _Trainer.last = None
    _Arguments.last = None
    _Trainer.closing_metrics = {"eval_loss": 1.2, "epoch": 2.99, "eval_runtime": 0.4}
    _Trainer.log_history = [
        {"loss": 2.0, "epoch": 1.0, "step": 1},
        {"eval_loss": 1.9, "epoch": 1.0, "step": 1, "eval_runtime": 0.5},
        {"loss": 1.6, "epoch": 2.0, "step": 2},
        {"eval_loss": 1.4, "epoch": 2.0, "step": 2, "eval_runtime": 0.5},
        {"loss": 1.3, "epoch": 3.0, "step": 3},
        {"eval_loss": 1.25, "epoch": 3.0, "step": 3, "eval_runtime": 0.5},
        {"train_runtime": 4.0, "epoch": 3.0, "step": 3},
    ]
    monkeypatch.setattr(
        production, "_runtime",
        lambda: production._Runtime(torch=_Torch, transformers=_Transformers,
                                    peft=_Peft, versions={"torch": "2.3.0"}))
    return production


def _run_backend(corpus: ValidatedCorpus, tmp_path: Path, config: TrainingConfig,
                 *, validation_file=...):
    from training_gym.training.backend import CancellationToken

    directory = tmp_path / "run"
    directory.mkdir(parents=True, exist_ok=True)
    planning = corpus_planning(corpus, config, tmp_path)
    if validation_file is ...:
        validation_file = (corpus.validation_file
                           if config.train_time_validation_enabled else None)
    request = ExecutionRequest(
        config=config, plan=planning.plan, run_directory=directory,
        train_file=corpus.train_file, validation_file=validation_file,
        device="cpu", precision="fp32")
    return TransformersPeftBackend().execute(request,
                                            cancellation=CancellationToken()), request


def corpus_planning(corpus: ValidatedCorpus, config: TrainingConfig, tmp_path: Path):
    from training_gym.training.planner import plan_training

    return plan_training(config, dataset_root=corpus.root,
                         output_root=tmp_path / "plans",
                         export_root=corpus.export_root)


# ══════════════════════════════════════════════════════════════════════════════
#  1-4 — the two arms carry the two splits, and only those
# ══════════════════════════════════════════════════════════════════════════════
def test_the_train_rows_reach_train_dataset(corpus, tmp_path, faked_runtime):
    result, _ = _run_backend(corpus, tmp_path, _config(corpus))
    assert result.ok, result.error_message
    trainer = _Trainer.last
    expected = convert_sft_export(corpus.train_file, max_sequence_length=512)
    assert len(trainer.kwargs["train_dataset"]) == len(expected.records)


def test_the_validation_rows_reach_eval_dataset(corpus, tmp_path, faked_runtime):
    """The defect, stated as the property it violated."""
    result, _ = _run_backend(corpus, tmp_path, _config(corpus))
    assert result.ok, result.error_message
    trainer = _Trainer.last
    expected = convert_sft_export(corpus.validation_file, max_sequence_length=512)
    assert trainer.kwargs["eval_dataset"] is not None
    assert len(trainer.kwargs["eval_dataset"]) == len(expected.records)
    assert len(expected.records) == len(corpus.ids_in(DatasetSplit.VALIDATION))


def test_no_validation_row_appears_in_the_training_dataset(corpus):
    train = {r.candidate_id
             for r in convert_sft_export(corpus.train_file,
                                         max_sequence_length=512).records}
    validation = corpus.ids_in(DatasetSplit.VALIDATION)
    assert validation
    assert not (train & validation)


def test_no_training_row_appears_in_the_validation_dataset(corpus):
    validation = {r.candidate_id
                  for r in convert_sft_export(corpus.validation_file,
                                              max_sequence_length=512).records}
    train = corpus.ids_in(DatasetSplit.TRAIN)
    assert train
    assert not (train & validation)
    assert validation == corpus.ids_in(DatasetSplit.VALIDATION)


def test_the_two_arms_share_no_row_inside_the_trainer(corpus, tmp_path, faked_runtime):
    """Disjoint on disk is necessary; disjoint in the objects handed over is the claim."""
    result, _ = _run_backend(corpus, tmp_path, _config(corpus))
    assert result.ok
    trainer = _Trainer.last
    train = {tuple(row["input_ids"]) for row in trainer.kwargs["train_dataset"]}
    evaluation = {tuple(row["input_ids"]) for row in trainer.kwargs["eval_dataset"]}
    assert train and evaluation
    assert not (train & evaluation)


# ══════════════════════════════════════════════════════════════════════════════
#  5-7 — held-out material reaches the trainer through neither arm
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("split", [DatasetSplit.HIDDEN_EVALUATION,
                                   DatasetSplit.SECURITY_REGRESSION])
def test_an_internal_held_out_split_is_not_exportable_at_all(scratch, split):
    assert split not in EXPORTABLE_SPLITS
    from training_gym.datasets.export import _export_split

    with pytest.raises(ExportError, match="not an exportable split"):
        _export_split(split=split, root=scratch.root, dataset_id=DATASET_ID,
                      dataset_version=VERSION, revocation=RevocationSnapshot(),
                      created_at_utc=NOW, out_root=scratch.export_root)


@pytest.mark.parametrize("split", [DatasetSplit.HIDDEN_EVALUATION,
                                   DatasetSplit.SECURITY_REGRESSION])
def test_no_internal_held_out_row_reaches_either_trainer_arm(corpus, tmp_path,
                                                             faked_runtime, split):
    held = corpus.ids_in(split)
    assert held
    for path in (corpus.train_file, corpus.validation_file):
        present = {r.candidate_id
                   for r in convert_sft_export(path, max_sequence_length=512).records}
        assert not (present & held), (path.name, sorted(present & held))
    # And the conversion authority refuses them by id even if one ever got in.
    for path in (corpus.train_file, corpus.validation_file):
        convert_sft_export(path, max_sequence_length=512,
                           held_out_candidate_ids=frozenset(held))


def test_the_conversion_authority_refuses_a_held_out_id_in_the_validation_corpus(corpus):
    """Non-vacuous: the same guard that protects TRAIN protects the eval arm."""
    validation = sorted(corpus.ids_in(DatasetSplit.VALIDATION))
    with pytest.raises(SchemaError, match="held-out material"):
        convert_sft_export(corpus.validation_file, max_sequence_length=512,
                           held_out_candidate_ids=frozenset({validation[0]}))


def test_the_evaluation_only_corpus_is_named_by_no_training_path():
    """``m62-defensive-eval`` v1/v2 appear nowhere on the train-time validation path."""
    for module in ("training_gym/datasets/export.py",
                   "training_gym/training/execution.py",
                   "training_gym/training/backends/transformers_peft.py",
                   "scripts/train_experiment.py"):
        assert "m62-defensive-eval" not in _executable_source(module), module


@pytest.mark.parametrize("flags,expected", [
    # evaluation_only implies not dataset_eligible: the candidate authority refuses a
    # record claiming both, because that would be trainable held-out material.
    ({"evaluation_only": True, "dataset_eligible": False}, "evaluation_only"),
    ({"dataset_eligible": False}, "not_dataset_eligible"),
])
def test_an_ineligible_record_is_excluded_from_the_validation_export(corpus, flags,
                                                                    expected):
    """The five redundant filters apply to the eval arm too, not only to TRAIN.

    The flags are set on a real promoted candidate rather than found in the fixture: the
    point is that the *validation* export refuses the same conditions the training export
    refuses, and a fixture that happens to contain no such row would prove nothing.
    """
    from training_gym.datasets.export import _exclusion_reason

    # A PROMOTED candidate: `_exclusion_reason` checks the state first, so an
    # unpromoted one would be excluded for the wrong reason and prove nothing.
    candidate = replace(corpus.result.promoted[0], **flags)
    assert _exclusion_reason(candidate, DatasetSplit.VALIDATION, RevocationSnapshot(),
                             source_split=DatasetSplit.VALIDATION) == expected
    # Same verdict on the training side, from the same code.
    assert _exclusion_reason(candidate, DatasetSplit.TRAIN,
                             RevocationSnapshot()) == expected


# ══════════════════════════════════════════════════════════════════════════════
#  8-10 — the eval arm is encoded by the production encoder, under the same cap
# ══════════════════════════════════════════════════════════════════════════════
def test_validation_uses_the_same_encoder_and_tokenizer_as_training(corpus, tmp_path,
                                                                    faked_runtime):
    """One tokenizer object, one ``_encode``, one template — asserted, not assumed."""
    code = _executable_source(
        "training_gym/training/backends/transformers_peft.py")
    # The eval rows are produced by the same call, with the same tokenizer variable and
    # the same cap. A second encoder would be a second rendering policy.
    assert code.count("self._encode(") == 2
    assert "tokenizer=tokenizer" in code
    assert code.count("max_length=config.max_sequence_length") == 2

    result, _ = _run_backend(corpus, tmp_path, _config(corpus))
    assert result.ok
    rows = _Trainer.last.kwargs["eval_dataset"]
    tokenizer = _Tokenizer()
    for row, record in zip(rows, convert_sft_export(
            corpus.validation_file, max_sequence_length=512).records, strict=True):
        assert row["input_ids"] == tokenizer.apply_chat_template(
            list(record.messages), tokenize=True, add_generation_prompt=False)


def _lengths(path: Path) -> tuple[list[int], list[int]]:
    tokenizer = _Tokenizer()
    records = convert_sft_export(path, max_sequence_length=512).records
    return ([len(tokenizer.apply_chat_template(list(r.prompt_messages), tokenize=True,
                                              add_generation_prompt=True))
             for r in records],
            [len(tokenizer.apply_chat_template(list(r.messages), tokenize=True,
                                               add_generation_prompt=False))
             for r in records])


def _biting_cap(corpus: ValidatedCorpus) -> int:
    """A cap that truncates at least one validation row but strands no supervised token.

    Derived rather than guessed, and derived over BOTH arms: ``max_sequence_length`` is
    one number for the whole run, so a cap chosen to bite the validation rows must still
    leave every training row a supervised token. Below a row's prompt length, truncation
    removes the entire supervised span and ``_encode`` correctly refuses the row — real
    behaviour, pinned separately, and not what these two tests are about.
    """
    train_prompts, _ = _lengths(corpus.train_file)
    eval_prompts, eval_fulls = _lengths(corpus.validation_file)
    cap = max(*train_prompts, *eval_prompts, 15) + 1
    assert cap < max(eval_fulls), "the fixture has no validation row long enough to cut"
    return cap


def test_validation_respects_max_sequence_length(corpus, tmp_path, faked_runtime):
    capped = _config(corpus, max_sequence_length=_biting_cap(corpus))
    result, _ = _run_backend(corpus, tmp_path, capped)
    assert result.ok, result.error_message
    for row in _Trainer.last.kwargs["eval_dataset"]:
        assert len(row["input_ids"]) <= capped.max_sequence_length
        assert len(row["labels"]) == len(row["input_ids"])


def test_the_validation_truncation_count_is_reported_separately(corpus, tmp_path,
                                                                faked_runtime):
    """A truncated eval arm must be visible, not folded into the training count."""
    capped = _config(corpus, max_sequence_length=_biting_cap(corpus))
    result, _ = _run_backend(corpus, tmp_path, capped)
    assert result.ok, result.error_message
    evidence = result.evidence["train_time_validation"]
    assert evidence["validation_rows_truncated"] >= 1
    # The two counters are independent, and the top-level one is the TRAINING arm's:
    # re-derived here from the training rows alone, so a fix that summed the two arms
    # into one number would fail.
    _, train_fulls = _lengths(corpus.train_file)
    expected_train = sum(1 for n in train_fulls if n > capped.max_sequence_length)
    assert result.truncated_records == expected_train
    _, eval_fulls = _lengths(corpus.validation_file)
    assert evidence["validation_rows_truncated"] == sum(
        1 for n in eval_fulls if n > capped.max_sequence_length)
    # And with a cap nothing exceeds, the validation count is zero.
    result, _ = _run_backend(corpus, tmp_path / "wide", _config(corpus))
    assert result.ok, result.error_message
    assert result.evidence["train_time_validation"]["validation_rows_truncated"] == 0


def test_a_cap_that_strands_every_supervised_token_is_refused(corpus, tmp_path,
                                                              faked_runtime):
    """Non-vacuous: the eval arm is refused by the same rule the training arm is.

    16 is the schema's floor for ``max_sequence_length`` and is below every prompt in the
    fixture, so truncation leaves no supervised token anywhere.
    """
    result, _ = _run_backend(corpus, tmp_path, _config(corpus, max_sequence_length=16))
    assert not result.ok
    assert "contribute nothing" in result.error_message


def test_no_validation_row_truncates_at_512_in_the_promoted_quality_corpus():
    """The S3G.1 512 qualification, re-checked over the rows the eval arm now reads.

    Bounded to the validation split and to a length count: the wiring does not change
    the rendering semantics S3G.1 measured, so re-auditing all 128 rows would re-measure
    a number nothing moved. Character length is a lower bound on nothing, so this asserts
    what it can without a tokenizer — the token measurement is in the S3G.2 document,
    taken through the production encoder against the pinned tokenizer.
    """
    export = (Path("training_gym_datasets/exports") / "m62-defensive-quality-train"
              / "v1" / SFT_VALIDATION_FILENAME)
    if not export.is_file():  # the promoted corpus is a gitignored runtime artefact
        pytest.skip("the promoted quality corpus is not present in this checkout")
    dataset = convert_sft_export(export, max_sequence_length=512)
    assert len(dataset.records) == 9
    for record in dataset.records:
        rendered = "".join(m["content"] for m in record.messages)
        assert len(rendered) <= 512 * 3, record.candidate_id


# ══════════════════════════════════════════════════════════════════════════════
#  11 — the eval arm contributes no gradient and no rows to training
# ══════════════════════════════════════════════════════════════════════════════
def test_validation_adds_no_row_and_no_label_to_the_training_dataset(corpus, tmp_path,
                                                                     faked_runtime):
    with_validation, _ = _run_backend(corpus, tmp_path, _config(corpus))
    without, _ = _run_backend(
        corpus, tmp_path / "b",
        _config(corpus, validation_strategy=ValidationStrategy.NO))
    assert with_validation.ok and without.ok
    assert with_validation.converted_records == without.converted_records
    assert with_validation.truncated_records == without.truncated_records
    assert with_validation.evidence["train_time_validation"]["contributes_gradients"] \
        is False


def test_the_backend_never_generates_during_validation():
    code = _executable_source(
        "training_gym/training/backends/transformers_peft.py")
    for forbidden in ("model.generate", ".generate(", "predict_with_generate",
                      "compute_metrics", "GenerationConfig"):
        assert forbidden not in code, forbidden


# ══════════════════════════════════════════════════════════════════════════════
#  12-14 — no early stopping, no checkpoints, no best-model selection
# ══════════════════════════════════════════════════════════════════════════════
def test_early_stopping_remains_disabled(corpus, tmp_path, faked_runtime):
    result, _ = _run_backend(corpus, tmp_path, _config(corpus))
    assert result.evidence["train_time_validation"]["early_stopping"] is False
    assert not any("EarlyStopping" in type(cb).__name__
                   for cb in _Trainer.last.callback_handler.callbacks)
    code = _executable_source(
        "training_gym/training/backends/transformers_peft.py")
    assert "EarlyStoppingCallback" not in code
    assert "early_stopping_patience" not in code
    # The only mention is the evidence field that records it as off.
    assert code.count("early_stopping") == 1
    for key in ("metric_for_best_model", "greater_is_better", "early_stopping_patience"):
        assert key not in _Arguments.last.kwargs


def test_checkpoint_saving_remains_disabled(corpus, tmp_path, faked_runtime):
    result, _ = _run_backend(corpus, tmp_path, _config(corpus))
    assert result.ok
    assert _Arguments.last.kwargs["save_strategy"] == CheckpointStrategy.NO.value
    assert _Arguments.last.kwargs["eval_strategy"] == ValidationStrategy.EPOCH.value


def test_load_best_model_at_end_remains_false(corpus, tmp_path, faked_runtime):
    """Stated explicitly rather than left to a default that could move under us."""
    _run_backend(corpus, tmp_path, _config(corpus))
    assert _Arguments.last.kwargs["load_best_model_at_end"] is False
    code = _executable_source(
        "training_gym/training/backends/transformers_peft.py")
    assert "load_best_model_at_end=True" not in code
    assert "load_best_model_at_end=False" in code


@pytest.mark.parametrize("strategy", [CheckpointStrategy.EPOCH,
                                     CheckpointStrategy.STEPS])
@pytest.mark.parametrize("validation", [ValidationStrategy.NO,
                                        ValidationStrategy.EPOCH])
def test_checkpoint_writing_stays_refused_whatever_the_validation_cadence(
        corpus, strategy, validation):
    """Enabling evaluation must not become a route back to pickle-shaped state (D16)."""
    with pytest.raises(SchemaError, match="checkpoint_strategy"):
        _config(corpus, checkpoint_strategy=strategy, validation_strategy=validation)


def test_the_config_refuses_validation_alongside_checkpointing_independently():
    """A second, explicit guard, so widening CheckpointStrategy cannot open the door.

    Unreachable through the constructor today — ``checkpoint_strategy`` is already
    restricted to ``no`` — so the guard is asserted at the source level rather than by
    building a config the schema will not build.
    """
    code = _executable_source("training_gym/training/config.py")
    assert "validation_strategy never authorises checkpoint writing" not in code
    assert ("self.validation_strategy is not ValidationStrategy.NO and "
            "self.checkpoint_strategy is not CheckpointStrategy.NO") in code


def test_a_per_step_cadence_is_refused_with_a_reason(corpus):
    assert ValidationStrategy.STEPS not in SUPPORTED_VALIDATION_STRATEGIES
    with pytest.raises(SchemaError, match="not supported"):
        _config(corpus, validation_strategy=ValidationStrategy.STEPS)


def test_the_adapter_directory_gains_no_checkpoint_artifact(corpus, tmp_path,
                                                            faked_runtime):
    result, request = _run_backend(corpus, tmp_path, _config(corpus))
    assert result.ok
    written = sorted(p.name for p in Path(request.run_directory).iterdir())
    assert not any(name.startswith("checkpoint") for name in written)
    for name in written:
        assert Path(name).suffix not in (".bin", ".pt", ".pth", ".pkl", ".pickle"), name


# ══════════════════════════════════════════════════════════════════════════════
#  15-16 — the behaviour is identity-bound, in both directions
# ══════════════════════════════════════════════════════════════════════════════
def test_enabling_validation_moves_the_config_identity(corpus):
    on = _config(corpus)
    off = replace(on, validation_strategy=ValidationStrategy.NO)
    assert on.config_hash() != off.config_hash()
    assert on.train_time_validation_enabled
    assert not off.train_time_validation_enabled


def test_disabling_validation_reproduces_the_pre_s3g2_identity(corpus):
    """The other half, and the reason the canonical key is value-gated.

    A fix that moved the hash unconditionally would re-identify every configuration
    written before this field existed — including the one the S3G.1 plan was built from.
    """
    off = replace(_config(corpus), validation_strategy=ValidationStrategy.NO)
    assert "validation_strategy" not in off.to_dict()
    legacy = TrainingConfig.from_dict(
        {k: v for k, v in off.to_dict().items()})
    assert legacy.config_hash() == off.config_hash()
    assert legacy.validation_strategy is ValidationStrategy.NO


def test_enabling_validation_moves_the_plan_identity(corpus, tmp_path):
    on = _config(corpus)
    off = replace(on, validation_strategy=ValidationStrategy.NO)
    plan_on = corpus_planning(corpus, on, tmp_path).plan
    plan_off = corpus_planning(corpus, off, tmp_path).plan
    assert plan_on.plan_hash() != plan_off.plan_hash()
    assert plan_on.training_config_hash != plan_off.training_config_hash
    # The dataset the two bind is the same one; only the run's behaviour differs.
    assert plan_on.dataset_manifest_hash == plan_off.dataset_manifest_hash
    assert plan_on.validation_split_hash == plan_off.validation_split_hash


def test_the_plan_records_the_cadence_it_authorises(corpus, tmp_path):
    plan_on = corpus_planning(corpus, _config(corpus), tmp_path).plan
    assert plan_on.hyperparameters["validation_strategy"] == "epoch"
    assert plan_on.hyperparameters["validation_split"] == "validation"
    off = replace(_config(corpus), validation_strategy=ValidationStrategy.NO)
    plan_off = corpus_planning(corpus, off, tmp_path).plan
    assert "validation_strategy" not in plan_off.hyperparameters


def test_the_dataset_identity_is_unchanged_by_the_validation_export(corpus):
    """Adding an export must not touch the promoted version it was derived from."""
    from training_gym.datasets.manifests import load_manifest

    reloaded = load_manifest(root=corpus.root, dataset_id=DATASET_ID,
                            dataset_version=VERSION)
    assert reloaded.manifest_hash() == corpus.manifest.manifest_hash()
    # The reference still binds the TRAINING export. Binding the validation export here
    # instead -- or as well -- would move `reference_hash`, and with it the identity the
    # S3G.1 plan was built against, for a file the reference never described.
    reference = corpus.reference()
    assert reference.export_manifest_hash == corpus.export.manifest.export_hash()
    assert reference.export_manifest_hash !=         corpus.validation_export.manifest.export_hash()
    # What binds the validation ROWS is the shard digest, which was already there.
    assert reference.validation_shard_hash
    assert reference.validation_split_manifest_hash


# ══════════════════════════════════════════════════════════════════════════════
#  17-18 — legacy readability, and the pre-S3G.2 run shape
# ══════════════════════════════════════════════════════════════════════════════
def test_a_config_document_that_never_heard_of_validation_still_loads(corpus):
    document = replace(_config(corpus),
                       validation_strategy=ValidationStrategy.NO).to_dict()
    assert "validation_strategy" not in document
    loaded = TrainingConfig.from_dict(document)
    assert loaded.validation_strategy is ValidationStrategy.NO
    assert not loaded.train_time_validation_enabled


def test_a_document_naming_the_field_round_trips(corpus):
    document = _config(corpus).to_dict()
    assert document["validation_strategy"] == "epoch"
    assert TrainingConfig.from_dict(document).config_hash() == \
        _config(corpus).config_hash()


def test_an_unknown_validation_field_name_still_fails_closed(corpus):
    document = _config(corpus).to_dict()
    document["validation_strategyy"] = "epoch"
    with pytest.raises(SchemaError, match="unknown field"):
        TrainingConfig.from_dict(document)


def test_the_pre_s3g2_run_shape_is_unchanged(corpus, tmp_path, faked_runtime):
    """With validation off, ``Trainer`` receives exactly what it always received."""
    off = replace(_config(corpus), validation_strategy=ValidationStrategy.NO)
    result, _ = _run_backend(corpus, tmp_path, off)
    assert result.ok, result.error_message
    assert _Trainer.last.kwargs["eval_dataset"] is None
    assert _Arguments.last.kwargs["eval_strategy"] == "no"
    assert result.eval_loss == 0.0
    assert result.evidence["train_time_validation"] == {
        "enabled": False, "strategy": "no", "validation_rows": 0, "evaluations": 0,
        "note": "the reviewed config asks for no train-time validation"}


# ══════════════════════════════════════════════════════════════════════════════
#  The readiness boundary — both directions, before anything is spent
# ══════════════════════════════════════════════════════════════════════════════
def test_validation_enabled_with_no_corpus_is_refused_before_the_plan_is_spent(
        corpus, tmp_path):
    planning = corpus_planning(corpus, _config(corpus), tmp_path)
    request = ExecutionRequest(
        config=_config(corpus), plan=planning.plan, run_directory=tmp_path,
        train_file=corpus.train_file, validation_file=None, device="cpu",
        precision="fp32")
    problems = TransformersPeftBackend().readiness(request)
    assert any("no validation corpus was supplied" in p for p in problems), problems


def test_validation_enabled_with_an_unreadable_corpus_is_refused(corpus, tmp_path):
    planning = corpus_planning(corpus, _config(corpus), tmp_path)
    request = ExecutionRequest(
        config=_config(corpus), plan=planning.plan, run_directory=tmp_path,
        train_file=corpus.train_file, validation_file=tmp_path / "absent.jsonl",
        device="cpu", precision="fp32")
    problems = TransformersPeftBackend().readiness(request)
    assert any("validation corpus is not a readable file" in p for p in problems)


def test_a_corpus_supplied_against_a_config_that_did_not_ask_is_refused(corpus,
                                                                        tmp_path):
    """The config is the authority. A file arriving alongside 'no' is a disagreement."""
    off = replace(_config(corpus), validation_strategy=ValidationStrategy.NO)
    planning = corpus_planning(corpus, off, tmp_path)
    request = ExecutionRequest(
        config=off, plan=planning.plan, run_directory=tmp_path,
        train_file=corpus.train_file, validation_file=corpus.validation_file,
        device="cpu", precision="fp32")
    problems = TransformersPeftBackend().readiness(request)
    assert any("validation_strategy is 'no'" in p for p in problems), problems


def test_the_execution_stage_no_longer_hard_codes_the_field_to_none():
    """The exact line D31 was. A regression here is silent, so it is pinned literally."""
    code = Path("training_gym/training/execution.py").read_text(encoding="utf-8")
    assert "validation_file=validation_file" in code
    assert "validation_file=None," not in code


# ══════════════════════════════════════════════════════════════════════════════
#  Observability — the numbers a future S3H report has to be able to print
# ══════════════════════════════════════════════════════════════════════════════
def test_train_and_validation_loss_are_both_observable(corpus, tmp_path, faked_runtime):
    result, _ = _run_backend(corpus, tmp_path, _config(corpus))
    assert result.train_loss == pytest.approx(1.5)
    evidence = result.evidence["train_time_validation"]
    assert evidence["enabled"] is True
    assert evidence["strategy"] == "epoch"
    assert evidence["validation_rows"] == len(corpus.ids_in(DatasetSplit.VALIDATION))
    assert evidence["evaluations"] == 3
    assert [e["eval_loss"] for e in evidence["eval_loss_by_evaluation"]] == \
        [1.9, 1.4, 1.25]
    assert [e["epoch"] for e in evidence["eval_loss_by_evaluation"]] == [1.0, 2.0, 3.0]
    assert [e["step"] for e in evidence["eval_loss_by_evaluation"]] == [1, 2, 3]
    assert all("eval_runtime" in e for e in evidence["eval_loss_by_evaluation"])
    assert [e["loss"] for e in evidence["train_loss_by_logging_step"]] == [2.0, 1.6, 1.3]

    # The closing measurement is its own record, and it is the one reported as final.
    closing = evidence["final_evaluation"]
    assert closing["at_end_of_training"] is True
    assert closing["eval_loss"] == pytest.approx(1.2)
    assert closing["epoch"] == pytest.approx(2.99)
    assert evidence["final_eval_loss"] == pytest.approx(1.2)
    assert result.eval_loss == pytest.approx(1.2)
    # ... and it is NOT the last periodic value, which max_steps can leave mid-epoch.
    assert evidence["eval_loss_by_evaluation"][-1]["eval_loss"] == pytest.approx(1.25)


def test_a_closing_evaluation_runs_only_when_validation_is_enabled(corpus, tmp_path,
                                                                   faked_runtime):
    """``max_steps`` can cut the final epoch short, so the end-of-run number is taken."""
    _run_backend(corpus, tmp_path, _config(corpus))
    assert getattr(_Trainer.last, "evaluated", False) is True
    off = replace(_config(corpus), validation_strategy=ValidationStrategy.NO)
    _run_backend(corpus, tmp_path / "off", off)
    assert getattr(_Trainer.last, "evaluated", False) is False


def test_the_closing_evaluation_is_loss_only(corpus, tmp_path, faked_runtime):
    """It is ``evaluate()``, never ``predict()`` and never generation."""
    code = _executable_source(
        "training_gym/training/backends/transformers_peft.py")
    assert "trainer.evaluate()" in code
    assert "trainer.predict" not in code
    result, _ = _run_backend(corpus, tmp_path, _config(corpus))
    assert result.evidence["train_time_validation"]["generation_performed"] is False


def test_the_validation_evidence_is_json_serialisable_and_body_free(corpus, tmp_path,
                                                                     faked_runtime):
    """It is persisted in ``backend_result.json``; it must carry numbers only."""
    result, _ = _run_backend(corpus, tmp_path, _config(corpus))
    evidence = result.evidence["train_time_validation"]
    serialised = json.dumps(evidence, sort_keys=True)
    for record in convert_sft_export(corpus.validation_file,
                                     max_sequence_length=512).records:
        assert record.completion[:40] not in serialised
        for message in record.prompt_messages:
            assert message["content"][:40] not in serialised

    def _leaves(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from _leaves(item)
        elif isinstance(value, list):
            for item in value:
                yield from _leaves(item)
        else:
            yield value

    for leaf in _leaves(evidence):
        assert isinstance(leaf, (int, float, bool)) or (
            isinstance(leaf, str) and leaf in {
                "epoch", "no", "validation",
                "the reviewed config asks for no train-time validation",
                "manual_label_masking(-100)"}), repr(leaf)


def test_the_closing_evaluation_is_absent_when_validation_is_off(corpus, tmp_path,
                                                                faked_runtime):
    off = replace(_config(corpus), validation_strategy=ValidationStrategy.NO)
    result, _ = _run_backend(corpus, tmp_path, off)
    assert "final_evaluation" not in result.evidence["train_time_validation"]


def test_the_validation_evidence_states_what_it_is_not(corpus, tmp_path, faked_runtime):
    """A diagnostic number that travels without its caveats becomes a quality score."""
    result, _ = _run_backend(corpus, tmp_path, _config(corpus))
    evidence = result.evidence["train_time_validation"]
    assert evidence["is_held_out_eligibility_evidence"] is False
    assert evidence["generation_performed"] is False
    assert evidence["contributes_gradients"] is False
    assert evidence["load_best_model_at_end"] is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"),
                                 None, "not a number", object()])
def test_a_non_finite_measurement_never_enters_the_curve(bad):
    assert _finite(bad) == 0.0


def test_a_diverged_eval_loss_does_not_become_a_reported_number(corpus, tmp_path,
                                                                faked_runtime):
    _Trainer.log_history = [{"eval_loss": float("nan"), "epoch": 1.0, "step": 1}]
    _Trainer.closing_metrics = {"eval_loss": float("inf"), "epoch": 3.0}
    result, _ = _run_backend(corpus, tmp_path, _config(corpus))
    assert result.ok
    evidence = result.evidence["train_time_validation"]
    assert evidence["eval_loss_by_evaluation"] == [
        {"eval_loss": 0.0, "epoch": 1.0, "step": 1}]
    assert evidence["final_evaluation"]["eval_loss"] == 0.0
    assert result.eval_loss == 0.0


def test_assistant_only_loss_is_proven_for_the_eval_arm_too(corpus, tmp_path,
                                                            faked_runtime):
    result, _ = _run_backend(corpus, tmp_path, _config(corpus))
    masking = result.evidence["train_time_validation"]["assistant_only_loss_evidence"]
    assert masking["verified"] is True
    assert masking["probe_prompt_tokens"] > 0
    assert masking["probe_completion_tokens"] > 0
    for row in _Trainer.last.kwargs["eval_dataset"]:
        prompt = row["prompt_length"]
        assert set(row["labels"][:prompt]) == {-100}
        assert any(token != -100 for token in row["labels"][prompt:])


# ══════════════════════════════════════════════════════════════════════════════
#  The export authority itself
# ══════════════════════════════════════════════════════════════════════════════
def test_the_validation_export_reads_the_validation_shard_and_nothing_else(corpus):
    manifest = corpus.validation_export.manifest
    assert manifest.source_split == DatasetSplit.VALIDATION.value
    assert manifest.filename == SFT_VALIDATION_FILENAME
    assert manifest.record_count == len(corpus.ids_in(DatasetSplit.VALIDATION))
    assert verify_sft_validation_export(
        out_root=corpus.export_root, dataset_id=DATASET_ID,
        dataset_version=VERSION).ok


def test_the_two_exports_are_separate_files_with_separate_manifests(corpus):
    assert corpus.train_file.is_file() and corpus.validation_file.is_file()
    assert corpus.train_file != corpus.validation_file
    assert (corpus.export_directory / SFT_VALIDATION_MANIFEST_FILENAME).is_file()
    assert corpus.validation_export.manifest.export_hash() != \
        corpus.export.manifest.export_hash()


def test_a_manifest_may_not_claim_one_split_under_the_other_split_s_filename():
    """The pair is bound. Either field alone would accept the mismatch."""
    digest = "a" * 64
    fields = dict(dataset_id="d", dataset_version="v1", dataset_manifest_hash=digest,
                  source_manifest_hash=digest, sha256_file=digest, size_bytes=10,
                  record_count=1, created_at_utc=NOW, row_hashes_hash=digest)
    with pytest.raises(SchemaError, match="not the one legal export name"):
        SFTExportManifest(filename=SFT_FILENAME, source_split="validation", **fields)
    with pytest.raises(SchemaError, match="not the one legal export name"):
        SFTExportManifest(filename=SFT_VALIDATION_FILENAME, source_split="train",
                          **fields)
    # Both correct pairings are accepted.
    for name, split in ((SFT_FILENAME, "train"),
                        (SFT_VALIDATION_FILENAME, "validation")):
        assert SFTExportManifest(filename=name, source_split=split,
                                 **fields).source_split == split


@pytest.mark.parametrize("split", ["hidden_evaluation", "security_regression",
                                   "adversarial", "quarantine", ""])
def test_a_manifest_naming_a_non_exportable_split_is_refused(split):
    digest = "a" * 64
    with pytest.raises(SchemaError, match="train-side split"):
        SFTExportManifest(
            dataset_id="d", dataset_version="v1", dataset_manifest_hash=digest,
            source_manifest_hash=digest, sha256_file=digest, size_bytes=10,
            record_count=1, created_at_utc=NOW, row_hashes_hash=digest,
            filename=SFT_FILENAME, source_split=split)


def test_the_train_export_manifest_is_byte_identical_to_the_pre_s3g2_shape(corpus):
    """Whatever the validation export added, the train export's record must not move.

    ``excluded_counts`` feeds ``export_hash``, and the exclusion reason for a row in the
    wrong split is built from the split's own name — so ``not_train_split`` has to keep
    exactly the spelling it had, or every existing train export re-hashes.
    """
    from training_gym.datasets.export import _exclusion_reason

    candidate = next(c for c in corpus.candidates if not c.evaluation_only)
    assert _exclusion_reason(candidate, DatasetSplit.VALIDATION,
                             RevocationSnapshot()) == "not_train_split"
    assert _exclusion_reason(candidate, DatasetSplit.TRAIN, RevocationSnapshot(),
                             source_split=DatasetSplit.VALIDATION) == \
        "not_validation_split"


def test_an_export_is_as_immutable_as_the_version_it_came_from(scratch):
    with pytest.raises(ExportError, match="already exists"):
        export_sft_validation(root=scratch.root, dataset_id=DATASET_ID,
                              dataset_version=VERSION,
                              revocation=RevocationSnapshot(), created_at_utc=NOW,
                              out_root=scratch.export_root)


def test_verification_refuses_a_manifest_that_describes_the_other_split(scratch):
    """Reports a problem rather than answering about the wrong corpus."""
    target = scratch.export_directory / SFT_VALIDATION_MANIFEST_FILENAME
    train_manifest = json.loads(
        (scratch.export_directory / "sft_train.manifest.json").read_text(
            encoding="utf-8"))
    train_manifest["source_split"] = "train"
    train_manifest["filename"] = SFT_FILENAME
    target.write_text(json.dumps(train_manifest), encoding="utf-8")
    result = verify_sft_validation_export(
        out_root=scratch.export_root, dataset_id=DATASET_ID, dataset_version=VERSION)
    assert not result.ok
    assert any("describes the train split" in p for p in result.problems), \
        result.problems


# ══════════════════════════════════════════════════════════════════════════════
#  Nothing new reaches the network, and no framework is imported by planning
# ══════════════════════════════════════════════════════════════════════════════
def test_the_validation_export_authority_imports_no_framework():
    before = {name for name in sys.modules
              if name.split(".")[0] in {"torch", "transformers", "peft", "trl",
                                        "accelerate", "datasets"}}
    import importlib

    importlib.reload(importlib.import_module("training_gym.datasets.export"))
    after = {name for name in sys.modules
             if name.split(".")[0] in {"torch", "transformers", "peft", "trl",
                                       "accelerate", "datasets"}}
    assert after == before


def test_planning_a_validation_enabled_config_still_creates_nothing(corpus, tmp_path):
    root = tmp_path / "plans"
    plan = corpus_planning(corpus, _config(corpus), tmp_path).plan
    assert plan.plan_hash()
    assert not (root / "runs").exists()
    assert not (root / "training_runs.jsonl").exists()
