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

        transformers.set_seed(config.seed)

        tokenizer = transformers.AutoTokenizer.from_pretrained(
            config.tokenizer_id, revision=config.tokenizer_revision,
            trust_remote_code=TRUST_REMOTE_CODE, local_files_only=local_only)
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

        dataset = convert_sft_export(request.train_file,
                                     max_sequence_length=config.max_sequence_length)
        rows, truncated = self._encode(dataset, tokenizer=tokenizer,
                                       max_length=config.max_sequence_length)
        masking = self._masking_self_test(rows)
        if not masking.verified:
            return self._failed(
                request, "dataset",
                f"assistant-only loss could not be proven ({list(masking.problems)}); "
                f"refusing rather than silently fitting the system and user turns",
                started)
        cancellation.raise_if_requested()

        model = transformers.AutoModelForCausalLM.from_pretrained(
            config.base_model_id, revision=config.base_model_revision,
            trust_remote_code=TRUST_REMOTE_CODE, local_files_only=local_only,
            torch_dtype=_dtype(runtime, request.precision))
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
            save_strategy=config.checkpoint_strategy.value,
            save_total_limit=config.max_checkpoints, save_safetensors=True,
            # Every remote integration, off. transformers defaults report_to="all",
            # which activates any tracker that merely happens to be importable and
            # reintroduces the exfiltration endpoint the config vocabulary removed.
            report_to=[], push_to_hub=False, disable_tqdm=True)
        trainer = transformers.Trainer(
            model=model, args=arguments, train_dataset=rows,
            data_collator=transformers.DataCollatorForSeq2Seq(
                tokenizer, padding=True, label_pad_token_id=-100))

        cancellation.raise_if_requested()
        metrics = trainer.train().metrics
        cancellation.raise_if_requested()

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
            eval_loss=float(metrics.get("eval_loss", 0.0)),
            duration_seconds=time.monotonic() - started,
            output_files=tuple(sorted(p.name for p in
                                      Path(request.run_directory).iterdir()
                                      if p.is_file())),
            package_versions=dict(runtime.versions),
            truncated_records=truncated, converted_records=len(rows),
            evidence={
                "assistant_only_loss": True,
                "assistant_only_loss_evidence": masking.to_dict(),
                "tokenizer_chat_template_hash": chat_template_hash(
                    getattr(tokenizer, "chat_template", None)),
                "trainable_parameters": trainable,
                "total_parameters": total,
                "trust_remote_code": False,
                "local_files_only": local_only,
                "deterministic_reproduction_claimed": False,
            })

    # ── encoding ──────────────────────────────────────────────────────────────
    def _encode(self, dataset, *, tokenizer, max_length: int):
        """Tokenize each record and mask the prompt span. Truncation is counted."""
        rows: list[dict] = []
        truncated = 0
        for record in dataset.records:
            prompt_ids = tokenizer.apply_chat_template(
                list(record.prompt_messages), tokenize=True,
                add_generation_prompt=True)
            full_ids = tokenizer.apply_chat_template(
                list(record.messages), tokenize=True, add_generation_prompt=False)
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
