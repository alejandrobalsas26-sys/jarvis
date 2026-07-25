"""tests/test_ci_workflow_v69_m612.py — V69 M61.2: the gates cannot silently pass.

The pre-M61 workflow had two defects that made it unfit as a release gate:
``pip-audit -r requirements/base.txt || true`` discarded the audit's exit code *and*
its visibility, and a ``docker-build`` job required a Docker daemon. A gate that
cannot fail is not a gate.

These tests read ``.github/workflows/ci.yml`` and assert the properties that make it
one: it parses, it runs on the right events, every mandatory job runs a real command
with a real exit code, the single advisory job is explicitly labelled, nothing needs a
live service or a privilege, and no build artifact that could carry runtime state is
uploaded.

They deliberately do NOT assert that CI passed remotely — that is not something a
local test can know.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is a base dependency")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Jobs whose failure MUST block. The advisory job is excluded by name, once.
ADVISORY_JOBS = {"dependency-audit"}

#: Services and privileges CI must never require.
FORBIDDEN_REQUIREMENTS = (
    "ollama", "ollama serve", "docker build", "docker run", "redis-server",
    "postgres", "pg_ctl", "zeek", "sysmon", "sliver-server", "msfconsole",
    "sudo ", "runas", "JARVIS_TRUSTED_LAB",
)


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _steps(job: dict) -> list[dict]:
    return [s for s in job.get("steps", []) if isinstance(s, dict)]


def _run_commands(workflow: dict, *, jobs=None) -> list[tuple[str, str]]:
    out = []
    for name, job in workflow["jobs"].items():
        if jobs is not None and name not in jobs:
            continue
        for step in _steps(job):
            if "run" in step:
                out.append((name, str(step["run"])))
    return out


# ── it exists and parses ─────────────────────────────────────────────────────
def test_ci_workflow_exists():
    assert _WORKFLOW.is_file(), "no .github/workflows/ci.yml"


def test_ci_workflow_parses(workflow):
    assert isinstance(workflow, dict)
    assert workflow["name"]
    assert workflow["jobs"]


def test_superseded_workflow_is_removed():
    """Two workflows would double-run every gate and disagree on what blocks."""
    assert not (_REPO_ROOT / ".github" / "workflows" / "main.yml").exists()


# ── triggers ─────────────────────────────────────────────────────────────────
def test_triggers_cover_push_pr_and_manual_dispatch(workflow):
    # PyYAML parses a bare `on:` key as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert "workflow_dispatch" in triggers, "no manual dispatch"
    assert triggers["push"]["branches"] == ["master"]
    assert triggers["pull_request"]["branches"] == ["master"]


# ── mandatory gates cannot silently pass ─────────────────────────────────────
def test_no_mandatory_job_swallows_a_failure(workflow):
    mandatory = {n for n in workflow["jobs"] if n not in ADVISORY_JOBS}
    for job, command in _run_commands(workflow, jobs=mandatory):
        assert "|| true" not in command, f"job {job!r} discards a failure with `|| true`"
        assert "|| exit 0" not in command, f"job {job!r} forces success"


def test_no_mandatory_step_uses_continue_on_error(workflow):
    for name, job in workflow["jobs"].items():
        if name in ADVISORY_JOBS:
            continue
        assert not job.get("continue-on-error"), f"job {name!r} is continue-on-error"
        for step in _steps(job):
            assert not step.get("continue-on-error"), \
                f"job {name!r} has a continue-on-error step"


def test_the_advisory_job_still_surfaces_its_result(workflow):
    """Advisory is not the same as hidden — that was the `|| true` defect."""
    job = workflow["jobs"]["dependency-audit"]
    advisory = [s for s in _steps(job) if s.get("continue-on-error")]
    assert len(advisory) == 1, "exactly one step may be advisory"
    assert advisory[0].get("id"), "an advisory step must be referenceable"
    reported = "\n".join(str(s.get("run", "")) for s in _steps(job))
    assert "outcome" in reported, "the advisory outcome is never reported"


def test_bandit_remains_a_blocking_gate(workflow):
    """High-severity static analysis is a correctness gate, not an advisory one."""
    steps = _steps(workflow["jobs"]["dependency-audit"])
    bandit = [s for s in steps if "bandit" in str(s.get("run", ""))]
    assert bandit and not bandit[0].get("continue-on-error")


# ── the required gates are actually present ─────────────────────────────────
@pytest.mark.parametrize("fragment", [
    "ruff check",
    "compileall",
    "pytest",
    "check_release_consistency.py",
    "check_base_import.py",
    "check_package_manifest.py",
    "python -m build",
])
def test_required_gate_is_present(workflow, fragment):
    joined = "\n".join(c for _job, c in _run_commands(workflow))
    assert fragment in joined, f"CI never runs {fragment!r}"


def test_an_authoritative_job_runs_the_complete_suite(workflow):
    """At least one job must run the whole deterministic suite, not a subset."""
    commands = [c for job, c in _run_commands(workflow, jobs={"tests"})]
    joined = "\n".join(commands)
    assert "pytest -q" in joined
    # A path argument would make it a subset run.
    for line in joined.splitlines():
        if "pytest" in line and "tests/" in line:
            pytest.fail(f"the authoritative job runs a subset: {line.strip()}")


def test_checkout_and_python_setup_in_every_job(workflow):
    for name, job in workflow["jobs"].items():
        uses = [str(s.get("uses", "")) for s in _steps(job)]
        assert any(u.startswith("actions/checkout@") for u in uses), f"{name}: no checkout"
        assert any(u.startswith("actions/setup-python@") for u in uses), \
            f"{name}: no python setup"


def test_python_311_is_the_authoritative_runtime(workflow):
    versions = []
    for job in workflow["jobs"].values():
        for step in _steps(job):
            with_block = step.get("with") or {}
            if "python-version" in with_block:
                versions.append(str(with_block["python-version"]))
    assert "3.11" in versions
    assert versions.count("3.11") >= 4, "3.11 is not the dominant runtime"


def test_compatibility_job_targets_312(workflow):
    steps = _steps(workflow["jobs"]["compat"])
    assert any(str((s.get("with") or {}).get("python-version")) == "3.12" for s in steps)


# ── no live service, no privilege, no secret ────────────────────────────────
def test_ci_requires_no_live_service_or_privilege(workflow):
    joined = "\n".join(c for _job, c in _run_commands(workflow)).lower()
    for forbidden in FORBIDDEN_REQUIREMENTS:
        assert forbidden.lower() not in joined, f"CI requires {forbidden!r}"


def test_no_job_declares_a_service_container(workflow):
    for name, job in workflow["jobs"].items():
        assert "services" not in job, f"job {name!r} declares a service container"
        assert "container" not in job, f"job {name!r} runs in a container"


def test_no_secrets_are_referenced(workflow):
    raw = _WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in raw, "the workflow references a repository secret"
    assert "ANTHROPIC_API_KEY" not in raw
    assert "OPENROUTER" not in raw


def test_no_artifact_upload(workflow):
    """A build artifact from this repository could carry runtime state."""
    for name, job in workflow["jobs"].items():
        for step in _steps(job):
            uses = str(step.get("uses", ""))
            assert not uses.startswith("actions/upload-artifact"), \
                f"job {name!r} uploads an artifact"


def test_host_auto_install_stays_disabled_in_ci(workflow):
    env = workflow.get("env", {})
    assert str(env.get("JARVIS_AUTO_INSTALL_DEPS", "")).lower() == "false"


# ── the referenced scripts exist ────────────────────────────────────────────
@pytest.mark.parametrize("script", [
    "check_release_consistency.py", "check_base_import.py",
    "check_package_manifest.py", "soak_stabilization_m61.py", "doctor.py",
])
def test_referenced_script_exists(script):
    assert (_REPO_ROOT / "jarvis" / "scripts" / script).is_file()


def test_ci_uses_the_constraints_file(workflow):
    joined = "\n".join(c for _job, c in _run_commands(workflow))
    assert "constraints-ci.txt" in joined, "CI resolves without the constraint set"
