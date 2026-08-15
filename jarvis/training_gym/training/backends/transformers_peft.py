"""training_gym/training/backends/transformers_peft.py — V69 M62 S3B: the real trainer.

THIS MODULE HAS NEVER BEEN RUN AGAINST A MODEL
-----------------------------------------------
No live smoke has been performed. The code below is written to be correct and is proved
structurally — that it never reaches a hub, never enables remote code, never falls back
to a pickle — but "it trains" is a claim only a real run can support, and this milestone
does not make it. When the optional packages are installed and an operator supplies the
exact ``TRAIN:<plan-hash>`` token, this is the code that runs.

WHY EVERY FRAMEWORK IMPORT IS INSIDE A FUNCTION
------------------------------------------------
Importing ``torch`` costs seconds and megabytes, and the whole point of the planning stage
is that it is safe and cheap to run. More importantly, the milestone's central claim is
that a normal JARVIS import, a dry run, a dependency check and a hardware check load no
machine-learning framework. That claim is asserted by measuring ``sys.modules`` after the
call, so a module-level ``import torch`` here would break it from three directions at
once. The imports live in :func:`_runtime`, which is called only from
:meth:`TransformersPeftBackend.execute`.

WHY THE VALIDATION SPLIT IS AN EVAL ARM AND NOT A SECOND TRAINING SET
---------------------------------------------------------------------
When the reviewed config sets a ``validation_strategy`` other than ``no``, the promoted
VALIDATION split is encoded by *this* object, through the same ``_encode`` and the same
``build_labels`` the training rows go through, and handed to ``Trainer`` as
``eval_dataset``. It contributes no gradient: ``Trainer`` computes loss over
``eval_dataset`` under ``torch.no_grad`` and takes no optimizer step for it.

Three things are deliberately NOT done with it. There is no early stopping — no
``EarlyStoppingCallback``, and none is importable from here — because a nine-row split
must not decide when a run ends. There is no ``load_best_model_at_end``, which is what
would make ``Trainer`` demand ``save_strategy`` match ``eval_strategy`` and start writing
the pickle-shaped checkpoint state the adapter artifact policy refuses (defect D16).
And there is no generation: evaluation here is teacher-forced cross-entropy over targets
the corpus already contains, so no response body is produced, scored or persisted.

WHY ASSISTANT-ONLY LOSS IS BUILT BY HAND
-----------------------------------------
``trl.DataCollatorForCompletionOnlyLM`` is the obvious tool and it is not used. Its import
location, its constructor keywords and ``SFTTrainer``'s surrounding contract all moved
across the ``trl>=0.9.6`` floor this repository declares, and there is no upper bound — so
a fresh install can resolve to a version where the masking silently means something else.
``-100`` is ``torch.nn.CrossEntropyLoss(ignore_index=...)``, it is framework-level, and it
has not changed. The labels are constructed in
:mod:`training_gym.training.dataset_conversion`, which imports nothing, and every row is
checked to be a real prefix — an off-by-one mask trains on the question and reports that
it did not.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..backend import (
    BackendResult,
    BackendStatus,
    CancellationToken,
    ExecutionRequest,
    InterruptionRequested,
)
from ..chat_render import (
    ReasoningPolicy,
    reasoning_template_kwargs,
    template_honours_reasoning_policy,
)
from ..config import LoRATargetPolicy, TrainingMethod
from ..dataset_conversion import (
    DatasetConversionError,
    MaskingEvidence,
    build_labels,
    chat_template_hash,
    check_masking,
    convert_sft_export,
)

BACKEND_ID = "transformers_peft"
BACKEND_VERSION = "m62.transformers_peft.1"

#: The packages this backend needs at runtime. Reported, never installed.
RUNTIME_PACKAGES = ("torch", "transformers", "peft")

#: One neutral message used only to ask whether the chat template implements the
#: requested reasoning policy. It carries no corpus row, so the check cannot depend on
#: which dataset is being trained on, and it is never tokenized into a training example.
_HONOUR_PROBE_MESSAGES: tuple[dict, ...] = (
    {"role": "user", "content": "reasoning policy probe"},)

#: Passed to every ``from_pretrained``. Not a default that could be overridden: enabling
#: remote code executes Python fetched from a model repository inside this process, and
#: no configuration file, flag or backend option is permitted to grant that.
TRUST_REMOTE_CODE = False


class RuntimeUnavailable(RuntimeError):
    """A framework this backend needs is absent or incompatible. Never installed here."""


@dataclass(frozen=True)
class _Runtime:
    """The framework handles, resolved once, inside ``execute`` and nowhere else."""

    torch: object
    transformers: object
    peft: object
    versions: dict


def _runtime() -> _Runtime:
    """Import the frameworks. The ONLY place in the milestone that does.

    An absent package is a refusal with a name, never an attempt to obtain it. This
    milestone installs nothing: the operator runs the documented command themselves,
    reviews what it would pull in, and re-plans afterwards — because the installed
    versions are inside the plan hash and a changed one invalidates the confirmation.
    """
    missing: list[str] = []
    modules: dict[str, object] = {}
    for name in RUNTIME_PACKAGES:
        try:
            modules[name] = __import__(name)
        except Exception as exc:  # noqa: BLE001 — a broken install is also unavailable
            missing.append(f"{name} ({type(exc).__name__})")
    if missing:
        raise RuntimeUnavailable(
            f"the training frameworks are not importable: {missing}. Install the "
            f"optional profile yourself and re-plan; the installed versions are inside "
            f"the plan hash, so the confirmation must be reissued afterwards")
    versions = {name: str(getattr(module, "__version__", "unknown"))
                for name, module in modules.items()}
    return _Runtime(torch=modules["torch"], transformers=modules["transformers"],
                    peft=modules["peft"], versions=versions)


def _target_modules(policy: LoRATargetPolicy):
    """PEFT wants the STRING ``"all-linear"``, not a one-element tuple containing it.

    :attr:`LoRATargetPolicy.modules` returns a tuple for every policy, which is correct
    for the other two and wrong for this one: handed ``("all-linear",)``, PEFT looks for a
    module literally named ``all-linear``, finds none, and raises — or worse, on some
    versions, adapts nothing and trains a model with no trainable parameters while
    reporting success.
    """
    modules = policy.modules
    if policy is LoRATargetPolicy.ALL_LINEAR:
        return modules[0]
    return list(modules)


def _accepts(target: object, parameter: str) -> bool:
    """Whether the installed ``TrainingArguments`` still takes this keyword.

    Used for exactly one keyword — see :func:`_training_arguments`. Every other keyword is
    passed unconditionally, so a future removal is a loud failure rather than a silently
    dropped setting.
    """
    import inspect

    try:
        return parameter in inspect.signature(target).parameters
    except (TypeError, ValueError):  # pragma: no cover — an unintrospectable callable
        return False


def _serialization_options(transformers: object) -> dict:
    """``save_safetensors=True``, when the installed ``TrainingArguments`` still takes it.

    ``transformers>=5`` removed the parameter: it dropped torch-pickle checkpoint
    serialization entirely, so safetensors became the only format the trainer can write
    and the flag no longer exists to be set. Passing it unconditionally raised
    ``TypeError: TrainingArguments.__init__() got an unexpected keyword argument`` and
    killed the first live smoke. No upper bound is pinned on transformers, so both builds
    have to work.

    Omitting it is safe only because it was never the thing enforcing the guarantee.
    :mod:`training_gym.training.artifacts` refuses ``.bin``, ``.pt``, ``.pkl`` and every
    other pickle-shaped suffix from a run directory and requires
    ``adapter_model.safetensors`` by name, and the adapter itself is written by
    ``save_pretrained(..., safe_serialization=True)`` below, which is unconditional. This
    keyword only ever governed intermediate checkpoints, which this milestone does not
    write by default.

    Only this one keyword is conditional. Every other argument is passed unconditionally
    so that a future removal is a loud failure rather than a silently dropped setting.
    """
    return ({"save_safetensors": True}
            if _accepts(transformers.TrainingArguments, "save_safetensors") else {})


def _token_ids(rendered: object, *, label: str) -> list[int]:
    """The flat token id list, whatever shape the installed ``transformers`` returned.

    ``apply_chat_template(tokenize=True)`` returned a bare ``list[int]`` across the
    ``transformers>=4.44`` floor this repository declares, and returns a ``BatchEncoding``
    on ``transformers>=5``. ``list()`` of the latter yields its KEYS — ``["input_ids",
    "attention_mask"]`` — so the prompt and the full sequence both "tokenized" to length
    two, the assistant-only-loss check concluded there was no completion to supervise, and
    every real row was refused. That is the failure mode the masking self-test exists to
    catch, reached for the wrong reason.

    ``requirements/training.txt`` asserts no upper bound, so the shape is normalised here
    rather than assumed, and an unrecognised one is a refusal: a tokenizer whose output
    this cannot read is not a tokenizer to guess about.
    """
    ids = rendered
    if hasattr(ids, "keys") and "input_ids" in ids:  # BatchEncoding / dict, transformers 5
        ids = ids["input_ids"]
    if hasattr(ids, "tolist"):  # a tensor, if a future default ever returns one
        ids = ids.tolist()
    if isinstance(ids, (list, tuple)) and len(ids) == 1 and isinstance(
            ids[0], (list, tuple)):
        ids = ids[0]  # an unrequested batch dimension of exactly one
    if not isinstance(ids, (list, tuple)) or not ids:
        raise DatasetConversionError(
            f"{label}: the tokenizer returned {type(rendered).__name__}, which this "
            f"build cannot read as a token id sequence; refusing rather than training on "
            f"whatever it happens to iterate as")
    if not all(isinstance(token, int) and not isinstance(token, bool) for token in ids):
        raise DatasetConversionError(
            f"{label}: the tokenizer returned a sequence that is not token ids; refusing "
            f"rather than masking a sequence whose elements are not tokens")
    return list(ids)


def _finite(value: object) -> float:
    """A float a result may carry, or ``0.0``. NaN and infinity are not measurements.

    ``BackendResult`` already refuses a non-finite metric, so a NaN reaching it turns a
    diverged run into a raised contract error rather than a reported number. The
    per-evaluation history has no such guard, and a curve containing ``NaN`` would be
    persisted and later read as though it were data.
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return number


def _dtype(runtime: _Runtime, precision: str):
    """Map the selected precision onto a torch dtype. Unknown means fp32, never a guess."""
    table = {"bf16": "bfloat16", "fp16": "float16", "fp32": "float32",
             "int4_qlora": "bfloat16"}
    return getattr(runtime.torch, table.get(precision, "float32"))


@dataclass
class TransformersPeftBackend:
    """One bounded SFT-LoRA run, with every remote integration explicitly disabled."""

    backend_id: str = BACKEND_ID

    def version(self) -> str:
        return BACKEND_VERSION

    def supports(self, method: TrainingMethod) -> bool:
        """QLoRA is planned but not executed here. It is not "LoRA with a flag".

        Executing it needs a measured CUDA runtime, a compatible ``bitsandbytes``, a
        quantization config this repository has reviewed and a runtime self-test — none
        of which can be written honestly without a machine to prove them on. Silently
        running ordinary LoRA instead would produce an adapter whose manifest says
        ``sft_qlora`` and whose weights say otherwise.
        """
        return method is TrainingMethod.SFT_LORA

    def readiness(self, request: ExecutionRequest) -> tuple[str, ...]:
        """Every reason this request cannot run here. Imports nothing."""
        problems: list[str] = []
        method = request.config.method
        if method is TrainingMethod.SFT_QLORA:
            problems.append(
                "sft_qlora is planned but not executable: it requires a measured CUDA "
                "runtime, a compatible bitsandbytes and a quantization self-test this "
                "build cannot perform. It is left explicitly unsupported rather than "
                "silently downgraded to ordinary LoRA")
        elif not self.supports(method):
            problems.append(f"{method.value} is not executable by this backend")
        if request.config.trust_remote_code:
            problems.append("trust_remote_code is enabled; this backend never sets it")
        if not Path(request.train_file).is_file():
            problems.append("the training corpus is not a readable file")
        if request.config.train_time_validation_enabled:
            # Asked for while refusing is still free. A config that enables train-time
            # validation and reaches the trainer without a validation corpus would either
            # crash after the plan was spent, or -- worse -- be "handled" by quietly
            # training with no eval arm while the config, the plan and the report all say
            # it had one.
            if request.validation_file is None:
                problems.append(
                    f"validation_strategy is "
                    f"{request.config.validation_strategy.value!r} but no validation "
                    f"corpus was supplied; a run cannot measure a split it was not "
                    f"given, and silently training without the eval arm the plan "
                    f"records would misreport what ran")
            elif not Path(request.validation_file).is_file():
                problems.append("the validation corpus is not a readable file")
        elif request.validation_file is not None:
            problems.append(
                "a validation corpus was supplied but validation_strategy is 'no'; the "
                "reviewed config decides whether a run measures its validation split, "
                "and a file passed alongside a config that does not ask for one is a "
                "disagreement about what was approved")
        return tuple(problems)

    # ── the run ───────────────────────────────────────────────────────────────
    def execute(self, request: ExecutionRequest, *,
                cancellation: CancellationToken) -> BackendResult:
        """Load, adapt, train, save. Every failure is a result, never a traceback."""
        started = time.monotonic()
        unready = self.readiness(request)
        if unready:
            return self._blocked(request, unready, started)
        try:
            runtime = _runtime()
        except RuntimeUnavailable as exc:
            return self._failed(request, "dependency", str(exc), started)
        try:
            return self._train(request, runtime=runtime, cancellation=cancellation,
                               started=started)
        except InterruptionRequested:
            raise
        except DatasetConversionError as exc:
            return self._failed(request, "dataset", str(exc), started)
        except MemoryError as exc:  # pragma: no cover — needs a real device
            return self._failed(request, "out_of_memory", str(exc)[:200], started)
        except OSError as exc:  # pragma: no cover — needs a real filesystem failure
            return self._failed(request, "disk_full", type(exc).__name__, started)

    def _train(self, request: ExecutionRequest, *, runtime: _Runtime,
               cancellation: CancellationToken, started: float) -> BackendResult:
        config = request.config
        transformers, peft = runtime.transformers, runtime.peft
        local_only = not request.allow_model_download
        # The plan verified the weights are present in THIS cache, so the load has to
        # read the same one. Left unset, `from_pretrained` resolves against the default
        # hub cache instead: an authority that reported "cache ready" would be followed
        # by a load that finds nothing there, and the plan's verdict would bind nothing.
        # `None` is the library's own default, so naming no cache root changes nothing.
        cache_dir = str(request.model_cache_root) if request.model_cache_root else None

        transformers.set_seed(config.seed)

        tokenizer = transformers.AutoTokenizer.from_pretrained(
            config.tokenizer_id, revision=config.tokenizer_revision,
            trust_remote_code=TRUST_REMOTE_CODE, local_files_only=local_only,
            cache_dir=cache_dir)
        template = getattr(tokenizer, "chat_template", None)
        if not template:
            # No generic fallback is invented. A template decides what the model
            # actually sees, and guessing one means training on text nobody reviewed.
            return self._failed(
                request, "tokenizer",
                f"{config.tokenizer_id} ships no chat template and this build has no "
                f"reviewed template for it; refusing rather than inventing one", started)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token

        # A reasoning policy this template does not implement is a setting that would be
        # RECORDED as applied and would have changed nothing -- the exact failure the
        # evaluation backend already refuses, arriving on the training side. Refused
        # here, before a single optimizer step, using the same shared check rather than a
        # second opinion about what "honours" means (defect D37, S3M.1).
        if config.reasoning_policy.is_explicit and not template_honours_reasoning_policy(
                tokenizer, list(_HONOUR_PROBE_MESSAGES)):
            return self._failed(
                request, "tokenizer",
                f"the training configuration binds "
                f"reasoning_policy={config.reasoning_policy.value} and "
                f"{config.tokenizer_id}'s chat template renders identically with "
                f"enable_thinking on and off, or refuses the keyword. The run would "
                f"record a rendering policy it did not apply, and the adapter would be "
                f"fitted under a prefix nobody chose", started)

        render_policy = config.chat_render_policy(
            chat_template_hash=chat_template_hash(template),
            add_generation_prompt=True)
        full_render_policy = config.chat_render_policy(
            chat_template_hash=chat_template_hash(template),
            add_generation_prompt=False)

        dataset = convert_sft_export(request.train_file,
                                     max_sequence_length=config.max_sequence_length)
        rows, truncated = self._encode(dataset, tokenizer=tokenizer,
                                       max_length=config.max_sequence_length,
                                       reasoning_policy=config.reasoning_policy)
        masking = self._masking_self_test(rows)
        if not masking.verified:
            return self._failed(
                request, "dataset",
                f"assistant-only loss could not be proven ({list(masking.problems)}); "
                f"refusing rather than silently fitting the system and user turns",
                started)

        # The eval arm. Encoded by the SAME tokenizer, the same chat template, the same
        # `max_sequence_length` and the same label masking as the training rows -- a
        # validation loss produced under different rendering rules is not comparable to
        # the training loss it is read against, and the whole point of the number is the
        # comparison.
        eval_rows: list[dict] | None = None
        eval_truncated = 0
        eval_masking: MaskingEvidence | None = None
        if config.train_time_validation_enabled:
            validation = convert_sft_export(
                request.validation_file,
                max_sequence_length=config.max_sequence_length)
            eval_rows, eval_truncated = self._encode(
                validation, tokenizer=tokenizer,
                max_length=config.max_sequence_length,
                reasoning_policy=config.reasoning_policy)
            eval_masking = self._masking_self_test(eval_rows)
            if not eval_masking.verified:
                return self._failed(
                    request, "dataset",
                    f"assistant-only loss could not be proven for the validation split "
                    f"({list(eval_masking.problems)}); an eval loss computed over the "
                    f"prompt tokens is not the quantity the training loss is compared "
                    f"against", started)
            shared = ({r.candidate_id for r in dataset.records}
                      & {r.candidate_id for r in validation.records})
            if shared:
                # The splitter already made these disjoint and the export reads one shard
                # each, so this cannot fire today. It is checked because the failure it
                # describes -- measuring on rows that were fitted -- reports memorisation
                # as generalisation, and it is the one defect a loss curve cannot show.
                return self._failed(
                    request, "dataset",
                    f"{len(shared)} candidate(s) appear in both the training and the "
                    f"validation corpus; a split the model was fitted on measures "
                    f"nothing about it", started)
        cancellation.raise_if_requested()

        model = transformers.AutoModelForCausalLM.from_pretrained(
            config.base_model_id, revision=config.base_model_revision,
            trust_remote_code=TRUST_REMOTE_CODE, local_files_only=local_only,
            cache_dir=cache_dir, torch_dtype=_dtype(runtime, request.precision))
        if config.gradient_checkpointing:
            model.gradient_checkpointing_enable()
            model.config.use_cache = False

        lora = config.lora
        model = peft.get_peft_model(model, peft.LoraConfig(
            r=lora.rank, lora_alpha=lora.alpha, lora_dropout=lora.dropout,
            bias=lora.bias.value, target_modules=_target_modules(lora.target_policy),
            modules_to_save=list(lora.modules_to_save) or None,
            use_rslora=lora.use_rslora, task_type="CAUSAL_LM"))
        trainable, total = self._parameter_counts(model)
        if trainable <= 0:
            return self._failed(
                request, "backend",
                "no parameter is trainable after applying the adapter; the run would "
                "produce the weights it started with and report that it trained", started)
        if total and trainable / total > 0.5:
            return self._failed(
                request, "backend",
                f"{trainable} of {total} parameters are trainable; that is a full "
                f"fine-tune, not the adapter the plan approved", started)
        cancellation.raise_if_requested()

        arguments = transformers.TrainingArguments(
            output_dir=str(request.run_directory),
            num_train_epochs=config.epochs, max_steps=config.max_steps,
            per_device_train_batch_size=config.batch_size,
            gradient_accumulation_steps=config.gradient_accumulation_steps,
            learning_rate=config.learning_rate, weight_decay=config.weight_decay,
            warmup_ratio=config.warmup_ratio, seed=config.seed,
            gradient_checkpointing=config.gradient_checkpointing,
            dataloader_num_workers=config.dataloader_workers,
            logging_steps=config.logging_interval_steps,
            # Unchanged by the eval arm, and that is the point. `eval_strategy` and
            # `save_strategy` are only coupled by `load_best_model_at_end`, which is
            # never set here, so evaluation runs while the trainer still writes no
            # checkpoint at all.
            save_strategy=config.checkpoint_strategy.value,
            save_total_limit=config.max_checkpoints,
            # `eval_strategy`, not `evaluation_strategy`. The rename landed in
            # transformers 4.41 -- below the `>=4.44` floor requirements/training.txt
            # declares -- and the old name was REMOVED in transformers 5, which is what
            # is installed. So `eval_strategy` is the one spelling valid across the whole
            # declared support range, and it is passed unconditionally: this module makes
            # exactly one keyword conditional (see `_serialization_options`) so that a
            # future removal is a loud failure rather than a silently dropped setting.
            eval_strategy=config.validation_strategy.value,
            per_device_eval_batch_size=config.batch_size,
            # Every remote integration, off. transformers defaults report_to="all",
            # which activates any tracker that merely happens to be importable and
            # reintroduces the exfiltration endpoint the config vocabulary removed.
            report_to=[], push_to_hub=False, disable_tqdm=True,
            # Stated rather than left to a default that could change under us. True is
            # what would couple evaluation to checkpoint writing (D16) and let a nine-row
            # split silently choose which weights are saved.
            load_best_model_at_end=False,
            **_serialization_options(transformers))
        trainer = transformers.Trainer(
            model=model, args=arguments, train_dataset=rows,
            # None when the config asks for no validation, which is exactly what
            # `Trainer` received unconditionally before this: the split was promoted,
            # digest-bound into the plan and length-audited, and nothing read it.
            eval_dataset=eval_rows,
            data_collator=transformers.DataCollatorForSeq2Seq(
                tokenizer, padding=True, label_pad_token_id=-100))
        # No callback is registered, and that is a decision rather than an omission:
        # `EarlyStoppingCallback` would let nine validation rows terminate the run, and a
        # stop chosen by a split that small is noise wearing a policy's clothes.
        if getattr(trainer, "callback_handler", None) is not None:
            trainer.callback_handler.callbacks = [
                cb for cb in trainer.callback_handler.callbacks
                if "EarlyStopping" not in type(cb).__name__]

        cancellation.raise_if_requested()
        metrics = trainer.train().metrics
        cancellation.raise_if_requested()
        # Snapshot BEFORE the closing evaluation, so the per-epoch curve and the
        # end-of-run measurement stay two distinguishable things in the record.
        periodic = list(getattr(getattr(trainer, "state", None), "log_history", []) or [])
        final_metrics: dict = {}
        if config.train_time_validation_enabled:
            # One explicit closing pass, because `max_steps` bounds the run and can cut
            # the last epoch short -- the planned candidate stops at 2.99 epochs -- so the
            # final epoch-boundary evaluation is not necessarily a measurement of the
            # weights the run actually ends with, and on some step counts there is no
            # third boundary to evaluate at all. Teacher-forced loss over the same nine
            # rows: no generation, no response body, no gradient, no checkpoint.
            final_metrics = dict(trainer.evaluate() or {})
            cancellation.raise_if_requested()
        validation_evidence = self._validation_evidence(
            config=config, rows=eval_rows, truncated=eval_truncated,
            masking=eval_masking, history=periodic, final_metrics=final_metrics)

        # safe_serialization is passed explicitly rather than relied on as a default:
        # a pickle fallback is the one output shape the artifact policy will not accept.
        trainer.model.save_pretrained(str(request.run_directory),
                                      safe_serialization=True)

        state = getattr(trainer, "state", None)
        completed = int(getattr(state, "global_step", 0) or 0)
        return BackendResult(
            backend_id=self.backend_id, backend_version=BACKEND_VERSION,
            status=BackendStatus.SUCCEEDED,
            steps_attempted=max(completed, config.max_steps),
            steps_completed=completed,
            epochs_completed=float(getattr(state, "epoch", 0.0) or 0.0),
            train_loss=float(metrics.get("train_loss", 0.0)),
            # `train()` returns TRAINING metrics; a per-epoch eval loss lives in
            # `trainer.state.log_history`, which is where the final one is read from.
            # `metrics` is still consulted first so a future transformers that does
            # surface it there is not second-guessed.
            eval_loss=float(metrics.get(
                "eval_loss", validation_evidence.get("final_eval_loss", 0.0)) or 0.0),
            duration_seconds=time.monotonic() - started,
            output_files=tuple(sorted(p.name for p in
                                      Path(request.run_directory).iterdir()
                                      if p.is_file())),
            package_versions=dict(runtime.versions),
            truncated_records=truncated, converted_records=len(rows),
            evidence={
                "assistant_only_loss": True,
                "assistant_only_loss_evidence": masking.to_dict(),
                "train_time_validation": validation_evidence,
                "tokenizer_chat_template_hash": chat_template_hash(
                    getattr(tokenizer, "chat_template", None)),
                # The template SOURCE digest above cannot distinguish two different
                # calls into the same template, which is precisely how D37 went
                # unrecorded through S3H, S3K, S3I and S3L. These three bind the CALL.
                # `chat_render_policy_hash` is the prompt-prefix policy: the generation
                # prefix the adapter is fitted after, and the one an eligibility
                # evaluation must match.
                "chat_render_policy": render_policy.to_dict(),
                "chat_render_policy_hash": render_policy.render_policy_hash(),
                "full_sequence_render_policy_hash":
                    full_render_policy.render_policy_hash(),
                "trainable_parameters": trainable,
                "total_parameters": total,
                "trust_remote_code": False,
                "local_files_only": local_only,
                "deterministic_reproduction_claimed": False,
            })

    # ── encoding ──────────────────────────────────────────────────────────────
    def _encode(self, dataset, *, tokenizer, max_length: int,
                reasoning_policy: ReasoningPolicy = ReasoningPolicy.MODEL_DEFAULT):
        """Tokenize each record and mask the prompt span. Truncation is counted.

        ``reasoning_policy`` decides how the chat template is CALLED (defect D37, closed
        in S3M.1). ``MODEL_DEFAULT`` adds no keyword and reproduces the historical
        rendering exactly, which is what candidate 001 and candidate 002 were fitted
        under. ``DISABLED`` adds ``enable_thinking=False``, which is what an
        eligibility-grade evaluation renders under, so a run that binds it is fitted
        after the same generation prefix it will later be evaluated after.

        The kwarg goes to BOTH calls rather than only the prompt one. On the reviewed
        Qwen3 template the full-sequence render is invariant to it — measured, not
        assumed — but ``build_labels`` refuses unless the prompt tokenizes as a true
        prefix of the full sequence, and rendering the two halves of that comparison
        under different policies is how a boundary silently shifts. One policy, one
        rendering, both calls.
        """
        rows: list[dict] = []
        truncated = 0
        template_kwargs = reasoning_template_kwargs(reasoning_policy)
        for record in dataset.records:
            prompt_ids = _token_ids(
                tokenizer.apply_chat_template(
                    list(record.prompt_messages), tokenize=True,
                    add_generation_prompt=True, **template_kwargs),
                label=f"row {record.row_index} prompt")
            full_ids = _token_ids(
                tokenizer.apply_chat_template(
                    list(record.messages), tokenize=True,
                    add_generation_prompt=False, **template_kwargs),
                label=f"row {record.row_index} full sequence")
            labels = build_labels(list(prompt_ids), list(full_ids))
            if len(full_ids) > max_length:
                truncated += 1
                full_ids, labels = full_ids[:max_length], labels[:max_length]
                if all(token == -100 for token in labels):
                    raise DatasetConversionError(
                        f"row {record.row_index}: truncation removed every supervised "
                        f"token; the example would contribute nothing but cost a step")
            rows.append({"input_ids": list(full_ids),
                         "attention_mask": [1] * len(full_ids),
                         "labels": list(labels),
                         "prompt_length": len(prompt_ids)})
        return rows, truncated

    # ── validation observability ──────────────────────────────────────────────
    def _validation_evidence(self, *, config, rows, truncated: int,
                             masking: MaskingEvidence | None, history: list,
                             final_metrics: dict) -> dict:
        """What the eval arm measured, in numbers only.

        ``history`` is ``trainer.state.log_history`` rather than ``train().metrics``,
        which carries training metrics: the per-evaluation entries are the only place a
        per-epoch curve exists, and a single final number cannot show the train/eval
        divergence the split is here to make visible. ``final_metrics`` is the closing
        evaluation, kept separate so "the loss at the end of epoch 2" and "the loss of the
        weights this run produced" are never read as the same measurement.

        **Numbers only, by construction.** Every value below is an int, a float, a bool or
        a fixed vocabulary string. No prompt, no target, no generated text and no response
        body -- there is none to carry, because a teacher-forced loss produces no
        generation -- so this record can be persisted in ``backend_result.json`` without
        a body-free filter having to be trusted to remove something.

        This is **diagnostic** evidence. It is not a quality score, it does not authorise
        promotion, and it does not stand in for the held-out eligibility corpus.
        """
        if not config.train_time_validation_enabled:
            return {"enabled": False, "strategy": config.validation_strategy.value,
                    "validation_rows": 0, "evaluations": 0,
                    "note": "the reviewed config asks for no train-time validation"}

        evaluations: list[dict] = []
        for entry in history:
            if not isinstance(entry, dict) or "eval_loss" not in entry:
                continue
            measured = {"eval_loss": _finite(entry.get("eval_loss")),
                        "epoch": _finite(entry.get("epoch")),
                        "step": int(entry.get("step") or 0)}
            for optional in ("eval_runtime", "eval_samples_per_second",
                             "eval_steps_per_second"):
                if optional in entry:
                    measured[optional] = _finite(entry.get(optional))
            evaluations.append(measured)
        # Training losses are kept alongside so a reader can put the two curves next to
        # each other without re-deriving one of them from a different artefact.
        train_losses = [{"loss": _finite(e.get("loss")), "epoch": _finite(e.get("epoch")),
                         "step": int(e.get("step") or 0)}
                        for e in history
                        if isinstance(e, dict) and "loss" in e and "eval_loss" not in e]
        closing = {"eval_loss": _finite(final_metrics.get("eval_loss")),
                   "epoch": _finite(final_metrics.get("epoch")),
                   "at_end_of_training": True}
        for optional in ("eval_runtime", "eval_samples_per_second",
                         "eval_steps_per_second"):
            if optional in final_metrics:
                closing[optional] = _finite(final_metrics.get(optional))
        return {
            "enabled": True,
            "strategy": config.validation_strategy.value,
            "split": config.validation_split.value,
            "validation_rows": len(rows or ()),
            "validation_rows_truncated": int(truncated),
            "evaluations": len(evaluations),
            "eval_loss_by_evaluation": evaluations,
            "final_evaluation": closing,
            # The end-of-run number when there is one, and otherwise the last periodic
            # measurement. Never silently one presented as the other.
            "final_eval_loss": (closing["eval_loss"] if final_metrics
                                else (evaluations[-1]["eval_loss"] if evaluations
                                      else 0.0)),
            "train_loss_by_logging_step": train_losses,
            "assistant_only_loss_evidence": masking.to_dict() if masking else {},
            "early_stopping": False,
            "load_best_model_at_end": False,
            "contributes_gradients": False,
            "generation_performed": False,
            "is_held_out_eligibility_evidence": False,
        }

    def _masking_self_test(self, rows) -> MaskingEvidence:
        """Prove on real tokenized rows that no prompt token is supervised.

        Non-vacuous by construction: it fails when there are no rows, when a row has no
        masked prefix, and when a row has no supervised suffix — so it cannot pass by
        having nothing to check.
        """
        strategy = "manual_label_masking(-100)"
        if not rows:
            return MaskingEvidence(strategy=strategy,
                                   problems=("there are no rows to check",))
        problems: list[str] = []
        for index, row in enumerate(rows):
            for problem in check_masking(row["labels"], row["prompt_length"]):
                problems.append(f"row {index}: {problem}")
        first = rows[0]
        return MaskingEvidence(
            strategy=strategy, verified=not problems, problems=tuple(problems[:5]),
            probe_prompt_tokens=first["prompt_length"],
            probe_completion_tokens=len(first["labels"]) - first["prompt_length"])

    def _parameter_counts(self, model) -> tuple[int, int]:
        trainable = total = 0
        for parameter in model.parameters():
            count = int(parameter.numel())
            total += count
            if parameter.requires_grad:
                trainable += count
        return trainable, total

    # ── results ───────────────────────────────────────────────────────────────
    def _blocked(self, request: ExecutionRequest, problems, started: float):
        del request
        return BackendResult(
            backend_id=self.backend_id, backend_version=BACKEND_VERSION,
            status=BackendStatus.BLOCKED, error_category="unsupported_method",
            error_message="; ".join(problems)[:300],
            duration_seconds=time.monotonic() - started)

    def _failed(self, request: ExecutionRequest, category: str, message: str,
                started: float):
        del request
        return BackendResult(
            backend_id=self.backend_id, backend_version=BACKEND_VERSION,
            status=BackendStatus.FAILED, error_category=category,
            error_message=str(message)[:300],
            duration_seconds=time.monotonic() - started)


def build_backend() -> TransformersPeftBackend:
    """The registry's entry point. Constructing this imports no framework."""
    return TransformersPeftBackend()


__all__ = ["BACKEND_ID", "BACKEND_VERSION", "RUNTIME_PACKAGES", "TRUST_REMOTE_CODE",
           "RuntimeUnavailable", "TransformersPeftBackend", "build_backend"]
