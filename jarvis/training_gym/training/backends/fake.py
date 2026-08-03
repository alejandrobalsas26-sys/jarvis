"""training_gym/training/backends/fake.py — V69 M62 S3B: a trainer that cannot train.

WHY THIS EXISTS AND WHY IT IS UNREACHABLE
------------------------------------------
Every interesting property of the execution stage is a property of what happens when the
backend misbehaves: it returns a NaN loss, it writes a pickle, it plants a symlink, it
claims more steps than it took, it exits without an adapter, it hangs and is cancelled.
None of those can be provoked from a real trainer without a GPU, a model download and
half an hour — so the tests drive this instead.

It is deliberately absent from :data:`~training_gym.training.backends._PRODUCTION_BACKENDS`.
No string an operator can type resolves to it, there is no ``--fake-backend`` flag, and
the only way to obtain one is to import this module by name, which production code never
does. A test double that production can reach is not a test double, it is a bypass.

WHAT IT IS ALLOWED TO DO
------------------------
Write small, inert, synthetic files inside the run directory it was handed. It imports no
framework, opens no socket, spawns nothing and never touches the network. The
``safetensors`` files it writes are real length-prefixed headers with fabricated tensor
names, because the artifact validator reads that header and a test that fed it something
else would be testing a different parser.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..backend import (
    BackendResult,
    BackendStatus,
    CancellationToken,
    ExecutionRequest,
    InterruptionRequested,
)
from ..config import TrainingMethod

BACKEND_ID = "fake_deterministic"
BACKEND_VERSION = "m62.fake.1"


class FakeMode(str, Enum):
    """Every way a backend can be wrong, as a value a test can parametrize over."""

    SUCCESS = "success"
    INTERRUPTION = "interruption"
    DELAYED_CANCELLATION = "delayed_cancellation"
    EXCEPTION = "exception"
    NAN_LOSS = "nan_loss"
    INFINITE_LOSS = "infinite_loss"
    ZERO_STEPS = "zero_steps"
    OVERCLAIMED_STEPS = "overclaimed_steps"
    EMPTY_ADAPTER = "empty_adapter"
    MISSING_ADAPTER_CONFIG = "missing_adapter_config"
    MALFORMED_ADAPTER_CONFIG = "malformed_adapter_config"
    UNSAFE_BIN_OUTPUT = "unsafe_bin_output"
    PARTIAL_SAFETENSORS = "partial_safetensors"
    ZERO_TENSORS = "zero_tensors"
    BASE_MODEL_TENSORS = "base_model_tensors"
    UNEXPECTED_FILE = "unexpected_file"
    SYMLINK_ARTIFACT = "symlink_artifact"
    OVERSIZED_ARTIFACT = "oversized_artifact"
    BASE_MODEL_MISMATCH = "base_model_mismatch"
    LORA_CONFIG_MISMATCH = "lora_config_mismatch"
    OUT_OF_MEMORY = "out_of_memory"
    DISK_FULL = "disk_full"
    WRITE_OUTSIDE_WORKSPACE = "write_outside_workspace"
    UNSUPPORTED = "unsupported"


def _safetensors_bytes(tensor_names: tuple[str, ...], *, truncate: bool = False,
                       payload_bytes: int = 64) -> bytes:
    """A real, minimal safetensors file: u64 header length, JSON header, then data."""
    offset = 0
    header: dict = {}
    for name in tensor_names:
        header[name] = {"dtype": "F32", "shape": [payload_bytes // 4],
                        "data_offsets": [offset, offset + payload_bytes]}
        offset += payload_bytes
    header["__metadata__"] = {"format": "pt"}
    encoded = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    blob = len(encoded).to_bytes(8, "little") + encoded + (b"\x00" * offset)
    return blob[:len(blob) // 2] if truncate else blob


@dataclass
class FakeTrainingBackend:
    """A deterministic trainer double. Same inputs, same files, same numbers, always."""

    mode: FakeMode = FakeMode.SUCCESS
    backend_id: str = BACKEND_ID
    steps: int = 8
    train_loss: float = 1.25
    eval_loss: float = 1.4
    cancel_after_steps: int = 3
    observed: list[str] = field(default_factory=list)

    def version(self) -> str:
        return BACKEND_VERSION

    def supports(self, method: TrainingMethod) -> bool:
        return method in (TrainingMethod.SFT_LORA, TrainingMethod.SFT_QLORA)

    def readiness(self, request: ExecutionRequest) -> tuple[str, ...]:
        if self.mode is FakeMode.UNSUPPORTED:
            return ("the fake backend was asked to report itself unready",)
        if not self.supports(request.config.method):
            return (f"{request.config.method.value} is not executable by this backend",)
        return ()

    # -- the run ---------------------------------------------------------------
    def execute(self, request: ExecutionRequest, *,
                cancellation: CancellationToken) -> BackendResult:
        self.observed.append(request.plan.plan_hash())
        directory = Path(request.run_directory)

        if self.mode is FakeMode.EXCEPTION:
            raise RuntimeError("the fake backend was asked to fail")
        if self.mode is FakeMode.OUT_OF_MEMORY:
            return self._failed(request, "out_of_memory",
                                "the device ran out of memory")
        if self.mode is FakeMode.DISK_FULL:
            return self._failed(request, "disk_full",
                                "no space left on the output volume")
        if self.mode is FakeMode.INTERRUPTION:
            cancellation.request("the fake backend was asked to stop")
            self._write_partial(directory)
            raise InterruptionRequested("training: stop requested (fake backend)")
        if self.mode is FakeMode.DELAYED_CANCELLATION:
            for step in range(self.steps):
                if step >= self.cancel_after_steps and cancellation.requested:
                    self._write_partial(directory)
                    raise InterruptionRequested(
                        "training: stop requested between steps")
            return self._succeeded(request, directory)

        self._write_artifacts(request, directory)
        return self._succeeded(request, directory)

    # -- artifacts -------------------------------------------------------------
    def _write_partial(self, directory: Path) -> None:
        """Half a weights file, which is exactly what an interrupt leaves behind."""
        (directory / "adapter_model.safetensors").write_bytes(
            _safetensors_bytes(("base_model.model.layers.0.lora_A.weight",),
                               truncate=True))

    def _write_artifacts(self, request: ExecutionRequest, directory: Path) -> None:
        mode = self.mode
        lora = dict(request.config.lora.to_dict())
        config_payload = {
            "peft_type": "LORA", "task_type": "CAUSAL_LM",
            "base_model_name_or_path": request.config.base_model_id,
            "r": lora.get("rank"), "lora_alpha": lora.get("alpha"),
            "lora_dropout": lora.get("dropout"),
            "target_modules": list(lora.get("target_modules") or ()),
            "bias": lora.get("bias"),
        }
        if mode is FakeMode.BASE_MODEL_MISMATCH:
            config_payload["base_model_name_or_path"] = "Other/Model-7B"
        if mode is FakeMode.LORA_CONFIG_MISMATCH:
            config_payload["r"] = int(config_payload["r"] or 0) + 1
            config_payload["lora_alpha"] = int(config_payload["lora_alpha"] or 0) + 1

        if mode is not FakeMode.MISSING_ADAPTER_CONFIG:
            text = ("{not json" if mode is FakeMode.MALFORMED_ADAPTER_CONFIG
                    else json.dumps(config_payload, sort_keys=True))
            (directory / "adapter_config.json").write_text(text, encoding="utf-8")

        weights = directory / "adapter_model.safetensors"
        if mode is FakeMode.EMPTY_ADAPTER:
            weights.write_bytes(b"")
        elif mode is FakeMode.PARTIAL_SAFETENSORS:
            weights.write_bytes(_safetensors_bytes(
                ("base_model.model.layers.0.lora_A.weight",), truncate=True))
        elif mode is FakeMode.ZERO_TENSORS:
            weights.write_bytes(_safetensors_bytes(()))
        elif mode is FakeMode.BASE_MODEL_TENSORS:
            weights.write_bytes(_safetensors_bytes(
                ("base_model.model.layers.0.lora_A.weight",
                 "model.layers.0.self_attn.q_proj.weight")))
        elif mode is FakeMode.OVERSIZED_ARTIFACT:
            weights.write_bytes(_safetensors_bytes(
                ("base_model.model.layers.0.lora_A.weight",),
                payload_bytes=4096))
        else:
            weights.write_bytes(_safetensors_bytes((
                "base_model.model.layers.0.lora_A.weight",
                "base_model.model.layers.0.lora_B.weight")))

        if mode is FakeMode.UNSAFE_BIN_OUTPUT:
            (directory / "adapter_model.bin").write_bytes(b"\x80\x04\x95pickle")
        if mode is FakeMode.UNEXPECTED_FILE:
            (directory / "optimizer_state.json").write_text("{}", encoding="utf-8")
        if mode is FakeMode.SYMLINK_ARTIFACT:
            decoy = directory / "adapter_config.json"
            link = directory / "special_tokens_map.json"
            try:
                link.symlink_to(decoy)
            except (OSError, NotImplementedError):
                # Creating a symlink needs elevation on Windows. The test that cares
                # skips there; writing a plain file instead keeps the mode usable.
                link.write_text("{}", encoding="utf-8")
        if mode is FakeMode.WRITE_OUTSIDE_WORKSPACE:
            (directory.parent / "escaped.json").write_text("{}", encoding="utf-8")

    # -- results ---------------------------------------------------------------
    def _names(self, directory: Path) -> tuple[str, ...]:
        return tuple(sorted(p.name for p in directory.iterdir() if not p.is_dir()))

    def _succeeded(self, request: ExecutionRequest, directory: Path) -> BackendResult:
        mode = self.mode
        steps = 0 if mode is FakeMode.ZERO_STEPS else self.steps
        completed = steps
        attempted = steps
        if mode is FakeMode.OVERCLAIMED_STEPS:
            # Bounded by BackendResult itself, so the overclaim is expressed as an
            # attempt count below the plan's ceiling rather than an impossible object.
            attempted = completed = request.config.max_steps + 50
        loss = self.train_loss
        if mode is FakeMode.NAN_LOSS:
            loss = float("nan")
        elif mode is FakeMode.INFINITE_LOSS:
            loss = float("inf")
        return BackendResult(
            backend_id=self.backend_id, backend_version=BACKEND_VERSION,
            status=BackendStatus.SUCCEEDED, steps_attempted=attempted,
            steps_completed=completed, epochs_completed=1.0,
            train_loss=loss, eval_loss=self.eval_loss, duration_seconds=0.5,
            output_files=self._names(directory), converted_records=4,
            package_versions={"fake": BACKEND_VERSION},
            evidence={"mode": mode.value, "assistant_only_loss": True})

    def _failed(self, request: ExecutionRequest, category: str,
                message: str) -> BackendResult:
        del request
        return BackendResult(
            backend_id=self.backend_id, backend_version=BACKEND_VERSION,
            status=BackendStatus.FAILED, error_category=category,
            error_message=message, evidence={"mode": self.mode.value})


__all__ = ["BACKEND_ID", "BACKEND_VERSION", "FakeMode", "FakeTrainingBackend"]
