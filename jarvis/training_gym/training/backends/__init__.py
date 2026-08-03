"""training_gym/training/backends — V69 M62 S3B: the two things that can actually train.

WHY THIS IS A SUBPACKAGE
------------------------
Two reasons, and both are load-bearing.

The first is the purity scan. ``test_no_s3a_module_can_invoke_a_package_manager`` walks
``training_gym/training/*.py`` and refuses any module that so much as names a process or
network primitive. That scan is non-recursive on purpose, and the production backend is
the one module in this milestone that legitimately imports a machine-learning framework.
Keeping it below the scanned directory means the scan stays a hard rule over the control
plane rather than a rule with an exemption written into it.

The second is reachability. There is no config field and no CLI flag that names a
backend. :func:`get_backend` resolves from a fixed table, and the table's production entry
is the only one an operator can reach. The fake backend is importable by test code and by
nothing else — it is not registered, so no string an operator can type resolves to it.
"""
from __future__ import annotations

from ..backend import BackendError, TrainingBackend, require_backend

#: The production registry. Deliberately not populated by discovery or entry points:
#: a backend that appears because a package was installed is a backend nobody reviewed.
_PRODUCTION_BACKENDS: dict[str, str] = {
    "transformers_peft": "transformers_peft",
}


def available_backend_ids() -> tuple[str, ...]:
    return tuple(sorted(_PRODUCTION_BACKENDS))


def get_backend(name: str) -> TrainingBackend:
    """Resolve a production backend by name, or refuse. Never falls back to a default.

    A fallback would mean that a typo, or a registry that failed to populate, silently
    selects something other than what was asked for — and the caller would find out by
    reading the artifacts.
    """
    module_name = _PRODUCTION_BACKENDS.get(str(name))
    if module_name is None:
        raise BackendError(
            f"training: no backend named {str(name)[:40]!r} "
            f"(available: {list(available_backend_ids())})")
    from importlib import import_module

    module = import_module(f"{__name__}.{module_name}")
    return require_backend(module.build_backend())


__all__ = ["available_backend_ids", "get_backend"]
