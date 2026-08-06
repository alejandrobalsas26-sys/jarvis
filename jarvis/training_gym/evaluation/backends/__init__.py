"""training_gym/evaluation/backends — V69 M62 S3C: the closed backend registry.

WHY THIS PACKAGE EXISTS SEPARATELY
----------------------------------
The purity scan over the evaluation subsystem is non-recursive: it asserts that no module
directly under ``training_gym/evaluation/`` names a package manager, a socket or a
framework. The production backend must eventually import ``torch``, so it lives one level
deeper — exactly the arrangement S3B uses for its training backends, and for the same
reason.

WHAT IS SELECTABLE
------------------
Two names, and one of them is not in the production map. ``FakeEvaluationBackend`` is
importable by test code and is unreachable through :func:`get_backend`, so no config
file, environment variable or CLI flag can route a production evaluation to a double
that invents its answers.

There is no module path, class name or entry point in this lookup. A backend is chosen
from a frozen dict of names this repository has reviewed.
"""
from __future__ import annotations

from ..backend import EvaluationBackendError

#: The only backends a production evaluation may select.
PRODUCTION_BACKEND_IDS: tuple[str, ...] = ("transformers_peft",)

#: Names that must never resolve, whatever a caller passes. Listed explicitly so the
#: refusal is a rule rather than an accident of the production map's contents.
_NEVER_SELECTABLE: frozenset[str] = frozenset({
    "fake", "fake_evaluation", "fake_deterministic", "mock", "stub", "double",
    "null", "noop", "echo",
})


#: What each production backend must find installed before it could run. Declared here,
#: one level below ``training_gym/evaluation/``, for the same reason the backend itself
#: is: the purity scan over that directory must keep holding.
#:
#: These are the packages the *evaluation* path resolves, which is not the training set.
#: ``datasets``, ``trl`` and ``accelerate`` fit a model; generating from one and
#: attaching a LoRA needs torch, transformers, peft and the safetensors reader.
BACKEND_REQUIRED_PACKAGES: dict[str, tuple[str, ...]] = {
    "transformers_peft": ("torch", "transformers", "peft", "safetensors"),
}


def backend_required_packages(backend_id: object) -> tuple[str, ...]:
    """What a live evaluation on this backend needs. Imports nothing, probes nothing.

    Refuses an unknown name rather than returning ``()``. An empty requirement makes a
    dependency report ready by construction, so "I do not know this backend" must not be
    expressible as "this backend needs nothing".
    """
    name = str(backend_id or "").strip()
    if name not in BACKEND_REQUIRED_PACKAGES:
        raise EvaluationBackendError(
            f"evaluation backend {name!r} declares no dependency requirement; a backend "
            f"this repository cannot describe is one it cannot check a host against")
    return BACKEND_REQUIRED_PACKAGES[name]


def available_backends() -> tuple[str, ...]:
    """The backend ids a production evaluation may name."""
    return PRODUCTION_BACKEND_IDS


def get_backend(backend_id: object):
    """Resolve a reviewed backend by name, or refuse.

    Imported lazily inside the function: merely importing this package must not pull in
    ``torch``, so the CLI can print a plan on a host where no framework is installed.
    """
    name = str(backend_id or "").strip()
    if name.lower() in _NEVER_SELECTABLE:
        raise EvaluationBackendError(
            f"evaluation backend {name!r} is a test double and is not reachable from a "
            f"production configuration; a fake backend that could be selected here "
            f"would let a synthetic run be mistaken for a measurement")
    if name not in PRODUCTION_BACKEND_IDS:
        raise EvaluationBackendError(
            f"evaluation backend {name!r} is not a reviewed backend; available "
            f"{sorted(PRODUCTION_BACKEND_IDS)}. A backend is chosen from this list, "
            f"never from a module path, a class name or an environment variable")
    if name == "transformers_peft":
        from importlib import import_module
        module = import_module("training_gym.evaluation.backends.transformers_peft")
        return module.TransformersPeftEvaluationBackend()
    raise EvaluationBackendError(  # pragma: no cover - unreachable by construction
        f"evaluation backend {name!r} is listed but not constructible")


__all__ = ["BACKEND_REQUIRED_PACKAGES", "PRODUCTION_BACKEND_IDS", "available_backends",
           "backend_required_packages", "get_backend"]
