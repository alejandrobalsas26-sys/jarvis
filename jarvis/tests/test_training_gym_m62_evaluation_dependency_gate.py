"""V69 M62 S3E.1 — the evaluation dependency gate, which used to answer no question.

THE DEFECT THIS PINS SHUT
-------------------------
``DependencyReport.ready`` is true when every package in ``method_packages`` is
installed. ``build_dependency_report`` filled that tuple from a ``TrainingMethod``, and
an evaluation has no training method — it selects a *backend*. So all three call sites in
``scripts/evaluate_adapter.py`` passed nothing, ``method_packages`` was empty, ``all()``
over an empty tuple is ``True``, and ``--execute``'s dependency check could not fail on a
host with no torch, no transformers and no peft.

An empty requirement is now refused rather than satisfied, and the CLI states which
backend it is asking about. These tests exist because the previous shape passed its own
test suite comfortably: nothing asserted that the gate could ever say no.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from training_gym.evaluation import backends as B
from training_gym.evaluation.backend import EvaluationBackendError
from training_gym.training.config import DependencyProfile, TrainingMethod
from training_gym.training.dependencies import (
    DependencyReport,
    DependencyState,
    PackageAvailability,
    build_dependency_report,
)

CLI = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_adapter.py"
EVAL_PACKAGES = ("torch", "transformers", "peft", "safetensors")


def report_for(host: dict[str, DependencyState], *,
               required: tuple[str, ...] = EVAL_PACKAGES) -> DependencyReport:
    """A report describing an imaginary host, without touching this one."""
    return DependencyReport(
        profile=DependencyProfile.TRAINING,
        packages=tuple(PackageAvailability(name, state)
                       for name, state in sorted(host.items())),
        method_packages=required)


ALL_INSTALLED = {p: DependencyState.INSTALLED for p in EVAL_PACKAGES}


# ── an unasked question is not a passed one ───────────────────────────────────
def test_an_empty_requirement_is_never_ready():
    """The exact shape every evaluation call site produced before this milestone."""
    empty = report_for(ALL_INSTALLED, required=())
    assert empty.ready is False
    assert empty.blockers(), "a report that asked nothing must say so"


def test_an_empty_requirement_is_not_ready_even_on_a_bare_host():
    assert report_for({}, required=()).ready is False


def test_a_satisfied_requirement_is_ready():
    assert report_for(ALL_INSTALLED).ready is True
    assert report_for(ALL_INSTALLED).blockers() == ()


# ── each package the evaluation actually resolves ─────────────────────────────
@pytest.mark.parametrize("absent", EVAL_PACKAGES)
def test_any_missing_evaluation_package_blocks(absent):
    host = dict(ALL_INSTALLED, **{absent: DependencyState.MISSING})
    result = report_for(host)
    assert result.ready is False
    assert any(absent in problem for problem in result.blockers())


def test_a_missing_transformers_blocks_a_production_evaluation():
    result = report_for(dict(ALL_INSTALLED, transformers=DependencyState.MISSING))
    assert result.ready is False


def test_a_missing_peft_blocks_an_adapter_evaluation():
    """The candidate arm is the base model plus a LoRA; without peft there is no LoRA."""
    result = report_for(dict(ALL_INSTALLED, peft=DependencyState.MISSING))
    assert result.ready is False
    assert "peft" in B.backend_required_packages("transformers_peft")


@pytest.mark.parametrize("state", [DependencyState.UNKNOWN,
                                   DependencyState.VERSION_INCOMPATIBLE])
def test_an_undetermined_or_stale_package_is_not_a_satisfied_one(state):
    assert report_for(dict(ALL_INSTALLED, peft=state)).ready is False


# ── the backend declares the requirement ──────────────────────────────────────
def test_the_production_backend_declares_what_it_resolves():
    assert set(B.backend_required_packages("transformers_peft")) == set(EVAL_PACKAGES)


def test_every_selectable_backend_declares_a_requirement():
    for backend_id in B.available_backends():
        assert B.backend_required_packages(backend_id)


@pytest.mark.parametrize("name", ["", "fake", "mock", "unknown_backend", None])
def test_an_undescribable_backend_is_refused_rather_than_treated_as_needing_nothing(name):
    """Returning () here would make the gate vacuous again through the back door."""
    with pytest.raises(EvaluationBackendError):
        B.backend_required_packages(name)


def test_declaring_a_requirement_probes_it_without_importing_it():
    """The probe reads metadata. A gate that imported torch to check for torch is not
    a gate a host without torch could run."""
    result = build_dependency_report(
        profile=DependencyProfile.TRAINING,
        required_packages=B.backend_required_packages("transformers_peft"))
    assert set(result.method_packages) == set(EVAL_PACKAGES)
    assert result.to_dict()["installs_anything"] is False


def test_a_training_method_and_a_backend_requirement_compose():
    """Threading one must not silently drop the other."""
    result = build_dependency_report(
        profile=DependencyProfile.TRAINING, method=TrainingMethod.SFT_LORA,
        required_packages=("safetensors",))
    assert "safetensors" in result.method_packages
    for package in TrainingMethod.SFT_LORA.required_packages:
        assert package in result.method_packages


def test_a_requirement_is_not_double_counted():
    result = build_dependency_report(profile=DependencyProfile.TRAINING,
                                     required_packages=("torch", "torch", "peft"))
    assert list(result.method_packages).count("torch") == 1


# ── the call sites, which is where the defect lived ───────────────────────────
def cli_tree() -> ast.Module:
    return ast.parse(CLI.read_text(encoding="utf-8"))


def test_the_cli_never_calls_the_dependency_authority_without_a_requirement():
    """An AST check, because this is precisely the mistake a reader does not see.

    Three call sites each omitted one keyword argument and the whole gate stopped
    working. The CLI now routes every request through one helper that cannot omit it.
    """
    offenders = []
    for node in ast.walk(cli_tree()):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
        if name != "build_dependency_report":
            continue
        keywords = {kw.arg for kw in node.keywords}
        if not keywords & {"method", "required_packages"}:
            offenders.append(node.lineno)
    assert not offenders, (
        f"build_dependency_report called with no requirement at line(s) {offenders}; "
        f"an empty request makes the gate vacuous")


def test_the_cli_asks_about_the_backend_it_plans_against():
    source = CLI.read_text(encoding="utf-8")
    assert "backend_required_packages" in source
    assert 'BACKEND_ID = "transformers_peft"' in source


def test_the_execute_path_refuses_before_it_could_load_anything():
    """Order matters: the dependency refusal must precede any backend construction."""
    source = CLI.read_text(encoding="utf-8")
    body = source.split("def _execute(", 1)[1]
    gate = body.index("dependencies.ready")
    for later in ("get_backend", "run_paired_evaluation"):
        if later in body:
            assert body.index(later) > gate, (
                f"{later} is reachable before the dependency gate")


def test_no_path_in_the_cli_installs_anything():
    source = CLI.read_text(encoding="utf-8")
    for forbidden in ("pip install", "subprocess", "check_call", "os.system",
                      "urllib", "requests."):
        assert forbidden not in source, f"{forbidden!r} has no place in this command"


def test_the_install_hint_is_a_string_the_operator_runs():
    result = report_for(ALL_INSTALLED)
    assert "pip install" in result.install_hint()
    assert result.to_dict()["install_command_is_operator_run"] is True


# ── the fake backend stays reachable for qualification ────────────────────────
def test_the_fake_backend_needs_no_production_package():
    """Synthetic end-to-end qualification must run on a host with no torch.

    The fake backend is constructed directly by test code and is unreachable through
    `get_backend`, so it never consults this authority at all — which is what keeps the
    qualification honest in both directions: it cannot be routed to by a config, and it
    cannot be blocked by a missing framework.
    """
    from training_gym.evaluation.backends.fake import FakeEvaluationBackend
    assert FakeEvaluationBackend() is not None
    assert "fake" not in B.BACKEND_REQUIRED_PACKAGES
    with pytest.raises(EvaluationBackendError):
        B.get_backend("fake")
