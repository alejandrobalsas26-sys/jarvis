"""V69 M62 — grader protocol, execution safety and aggregation negative controls.

The point of every test here is the same: prove that a check which did not measure
anything cannot be read as a check that passed. So these tests build real
:class:`GraderResult` records, run real bounded commands and inspect the real argv and
environment handed to them. The security-relevant construction is never mocked — a
test that patches ``run_bounded`` and then asserts the policy object proves only that
the policy object is well-formed.

Nothing here needs Docker, a network, a GPU or a model.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from training_gym import policies as P
# Imported from the module, not the package: ``graders.aggregate`` is also the name of
# the package-level function, and the package attribute wins over the submodule.
from training_gym.graders.aggregate import (
    AggregationReport,
    aggregate,
    attach_results,
    requested_graders,
    validate_result_set,
)
from training_gym.graders.base import (
    ChangedFile,
    Grader,
    GraderContext,
    ToolProbe,
    errored,
    grader_environment,
    insufficient,
    make_result,
    not_applicable,
    probe_tool,
    run_bounded,
    skipped,
)
from training_gym.rewards import RewardBreakdown
from training_gym.schemas import ResultStatus, SchemaError, Severity
from training_gym.task_spec import GRADER_IDS, ActionKind, TaskFamily, TaskSpec
from training_gym.trajectory import (
    GraderResult,
    ModelIdentity,
    ModelRole,
    Recommendation,
    TeacherReview,
    Trajectory,
)


# ── local builders (deliberately not imported from a sibling test module) ─────
def make_spec(**overrides) -> TaskSpec:
    """A minimal valid coding-fix task.

    Defined locally on purpose: the repository supports being tested from both its
    git root and its application root, and a helper imported from another test module
    resolves in only one of them."""
    base = {
        "task_id": "coding-fix-001",
        "task_family": TaskFamily.CODING_FIX,
        "prompt": "Fix the failing test in src/mod.py without changing the test.",
        "created_by": "operator",
        "created_at": "2026-07-31T00:00:00Z",
        "allowed_actions": (ActionKind.READ_WORKSPACE_FILE,
                            ActionKind.WRITE_WORKSPACE_FILE,
                            ActionKind.RUN_TESTS, ActionKind.EMIT_ANSWER),
        "required_graders": ("pytest", "diff_budget", "file_boundary", "secret_pii"),
        "scoring": P.ScoringPolicy(
            mandatory_graders=("pytest", "diff_budget", "file_boundary", "secret_pii")),
    }
    base.update(overrides)
    return TaskSpec(**base)


def make_trajectory(spec: TaskSpec, **overrides) -> Trajectory:
    base = {
        "episode_id": "ep-001",
        "task_id": spec.task_id,
        "task_hash": spec.spec_hash(),
        "attempt_number": 1,
        "model": ModelIdentity(role=ModelRole.STUDENT, base_model="qwen3",
                               model_id="qwen3:8b-q4_K_M"),
        "final_answer": "Patched the off-by-one in src/mod.py.",
    }
    base.update(overrides)
    return Trajectory(**base)


def result(grader_id: str, status: ResultStatus = ResultStatus.PASS, **kw) -> GraderResult:
    payload = {"score": 1.0 if status is ResultStatus.PASS else 0.0,
               "non_vacuous_measurement": 3 if status is ResultStatus.PASS else 0}
    payload.update(kw)
    return GraderResult(grader_id=grader_id, grader_version="test.1", status=status,
                        **payload)


def passing_results() -> list[GraderResult]:
    return [result(g) for g in
            ("pytest", "diff_budget", "file_boundary", "secret_pii")]


class _Stub(Grader):
    """A grader whose measurement the test supplies."""

    grader_id = "secret_pii"
    grader_version = "stub.1"

    def __init__(self, produce) -> None:
        self._produce = produce

    def measure(self, ctx: GraderContext):
        return self._produce(self, ctx)


def context(spec: TaskSpec | None = None, **kw) -> GraderContext:
    spec = spec or make_spec()
    kw.setdefault("attempt_id", "attempt-1")
    return GraderContext(spec=spec, **kw)


# ── the protocol refuses to exist in a broken form ───────────────────────────
def test_grader_id_outside_the_frozen_registry_is_an_import_error():
    with pytest.raises(SchemaError, match="not one of the frozen ids"):
        class _Bad(Grader):
            grader_id = "pytest_grader"      # the classic typo
            grader_version = "1"

            def measure(self, ctx):          # pragma: no cover
                raise AssertionError


def test_every_frozen_grader_id_is_a_valid_identifier():
    for grader_id in GRADER_IDS:
        assert GraderResult(grader_id=grader_id, grader_version="v",
                            status=ResultStatus.SKIPPED).grader_id == grader_id


def test_a_grader_supporting_no_family_is_refused():
    with pytest.raises(SchemaError, match="supported_families is empty"):
        class _Bad(Grader):
            grader_id = "ruff"
            grader_version = "1"
            supported_families = frozenset()

            def measure(self, ctx):          # pragma: no cover
                raise AssertionError


def test_missing_grader_version_is_refused():
    with pytest.raises(SchemaError, match="grader_version is required"):
        class _Bad(Grader):
            grader_id = "bandit"
            grader_version = ""

            def measure(self, ctx):          # pragma: no cover
                raise AssertionError


# ── grade() makes a grader honest whether or not it wanted to be ─────────────
def test_unsupported_task_family_is_not_applicable_never_pass():
    class _OnlySigma(Grader):
        grader_id = "detection_rule"
        grader_version = "1"
        supported_families = frozenset({TaskFamily.SIGMA_RULE})

        def measure(self, ctx):              # pragma: no cover
            raise AssertionError("must not run for an unsupported family")

    verdict = _OnlySigma().grade(context())
    assert verdict.status is ResultStatus.NOT_APPLICABLE
    assert verdict.non_vacuous_measurement == 0
    assert not verdict.passed


def test_a_crashing_grader_becomes_error_not_a_silent_pass():
    def boom(grader, ctx):
        raise RuntimeError("the scanner exploded")

    verdict = _Stub(boom).grade(context())
    assert verdict.status is ResultStatus.ERROR
    assert verdict.error
    assert "RuntimeError" in verdict.error
    assert not verdict.passed


def test_a_raw_dictionary_is_never_an_authoritative_verdict():
    verdict = _Stub(lambda g, c: {"status": "pass", "score": 1.0}).grade(context())
    assert verdict.status is ResultStatus.ERROR
    assert "not a GraderResult" in verdict.error


def test_a_result_about_a_different_grader_is_refused():
    def wrong_subject(grader, ctx):
        return GraderResult(grader_id="ruff", grader_version="1",
                            status=ResultStatus.PASS, score=1.0,
                            non_vacuous_measurement=5)

    verdict = _Stub(wrong_subject).grade(context())
    assert verdict.status is ResultStatus.ERROR
    assert "different subject" in verdict.error


def test_pass_with_zero_measurement_cannot_be_constructed_and_becomes_error():
    def vacuous(grader, ctx):
        return make_result(grader, ResultStatus.PASS, score=1.0, measured=0)

    verdict = _Stub(vacuous).grade(context())
    assert verdict.status is ResultStatus.ERROR
    assert not verdict.passed


def test_a_real_pass_records_what_it_measured():
    def honest(grader, ctx):
        return make_result(grader, ResultStatus.PASS, score=1.0, measured=7,
                           evidence=("scanned 7 fields",))

    verdict = _Stub(honest).grade(context())
    assert verdict.status is ResultStatus.PASS
    assert verdict.non_vacuous_measurement == 7
    assert verdict.passed


def test_missing_tool_is_skipped_for_an_advisory_grader():
    spec = make_spec(required_graders=("pytest", "diff_budget", "file_boundary",
                                       "secret_pii", "ruff"))
    grader = _Stub(lambda g, c: skipped(g, "ruff is not installed"))
    verdict = grader.grade(context(spec))
    assert verdict.status is ResultStatus.SKIPPED
    assert not verdict.passed
    assert verdict.status.is_blocking_when_mandatory


def test_missing_tool_for_a_mandatory_grader_is_insufficient_evidence():
    class _NeedsTool(Grader):
        grader_id = "secret_pii"
        grader_version = "1"
        required_tool = "definitely-not-a-real-tool-xyz"

        def measure(self, ctx):
            probe = self.availability(ctx)
            assert not probe.available
            return self.unavailable(ctx, probe)

    verdict = _NeedsTool().grade(context())
    assert verdict.status is ResultStatus.INSUFFICIENT_EVIDENCE
    assert not verdict.passed


def test_result_constructors_never_produce_an_affirmative_status():
    grader = _Stub(lambda g, c: None)
    for verdict in (skipped(grader, "why"), insufficient(grader, "why"),
                    errored(grader, "why"), not_applicable(grader, "why")):
        assert not verdict.passed
        assert verdict.non_vacuous_measurement == 0


# ── the context is an access-control policy, not a bag ───────────────────────
def test_context_has_no_field_for_the_process_environment():
    fields = set(GraderContext.__dataclass_fields__)
    assert not fields & {"env", "environ", "environment", "os_environ", "secrets"}


def test_context_public_dict_carries_no_host_path(tmp_path: Path):
    ctx = context(workspace_root=tmp_path)
    blob = repr(ctx.to_public_dict())
    assert str(tmp_path) not in blob
    assert "workspace_root" not in ctx.to_public_dict()


def test_context_sanitizes_the_workspace_root_out_of_tool_output(tmp_path: Path):
    ctx = context(workspace_root=tmp_path)
    noisy = f"FAILED {tmp_path}{os.sep}tests{os.sep}test_x.py::test_one"
    cleaned = ctx.sanitize(noisy)
    assert str(tmp_path) not in cleaned
    assert "<workspace>" in cleaned


def test_context_rejects_a_traversal_in_an_allowlisted_path():
    with pytest.raises(SchemaError, match="traversal"):
        context(test_paths=("../../etc",))


def test_context_rejects_an_absolute_allowlisted_path():
    with pytest.raises(SchemaError, match="absolute path is refused"):
        context(source_paths=("/etc/passwd",))


def test_context_rejects_a_free_text_tool_call():
    with pytest.raises(SchemaError, match="structured object"):
        context(proposed_tool_calls=("run_shell_command --rf /",))  # type: ignore[arg-type]


def test_context_resolve_refuses_to_leave_the_workspace(tmp_path: Path):
    ctx = context(workspace_root=tmp_path)
    with pytest.raises(SchemaError):
        ctx.resolve("../outside.txt")


def test_context_resolve_refuses_a_symlink_escape(tmp_path: Path):
    outside = tmp_path.parent / "outside-secret"
    outside.mkdir(exist_ok=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    link = workspace / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host does not permit creating symlinks")
    ctx = context(workspace_root=workspace)
    with pytest.raises(SchemaError, match="outside the workspace"):
        ctx.resolve("link/file.txt")


def test_changed_file_rejects_a_windows_drive_path():
    with pytest.raises(SchemaError, match="drive-qualified"):
        ChangedFile(path="C:/Windows/System32/drivers/etc/hosts")


def test_changed_file_rejects_an_unknown_status():
    with pytest.raises(SchemaError, match="unknown status"):
        ChangedFile(path="src/mod.py", status="vandalised")


# ── external command safety ──────────────────────────────────────────────────
def test_run_bounded_refuses_a_shell_string(tmp_path: Path):
    with pytest.raises(SchemaError, match="non-empty list of strings"):
        run_bounded("python -c 'print(1)'", cwd=tmp_path)  # type: ignore[arg-type]


def test_run_bounded_refuses_a_missing_working_directory(tmp_path: Path):
    with pytest.raises(SchemaError, match="working directory does not exist"):
        run_bounded([sys.executable, "-c", "pass"], cwd=tmp_path / "nope")


def test_grader_environment_is_built_not_inherited(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/attacker/site-packages")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8080")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "AKIAnotreal")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
    env = grader_environment()
    assert "PATH" in env
    for leaked in ("PYTHONPATH", "HTTPS_PROXY", "AWS_SECRET_ACCESS_KEY", "LD_PRELOAD"):
        assert leaked not in env
    assert env["PYTHONHASHSEED"] == "0"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_grader_environment_actually_reaches_the_child(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/attacker/site-packages")
    outcome = run_bounded(
        [sys.executable, "-c",
         "import os;print(os.environ.get('PYTHONPATH','<absent>'))"],
        cwd=tmp_path, timeout_s=30)
    assert outcome.succeeded
    assert outcome.stdout.strip() == "<absent>"


def test_run_bounded_captures_a_real_exit_code(tmp_path: Path):
    outcome = run_bounded([sys.executable, "-c", "import sys;sys.exit(3)"],
                          cwd=tmp_path, timeout_s=30)
    assert outcome.started
    assert outcome.exit_code == 3
    assert not outcome.succeeded


def test_run_bounded_bounds_stdout(tmp_path: Path):
    outcome = run_bounded(
        [sys.executable, "-c", "print('A' * 200000)"], cwd=tmp_path, timeout_s=60,
        max_output_bytes=4096)
    assert outcome.truncated
    assert len(outcome.stdout.encode("utf-8")) <= 4096


def test_run_bounded_kills_a_command_that_overruns(tmp_path: Path):
    outcome = run_bounded([sys.executable, "-c", "import time;time.sleep(45)"],
                          cwd=tmp_path, timeout_s=1)
    assert outcome.timed_out
    assert outcome.exit_code is None
    assert not outcome.succeeded
    assert "process tree" in outcome.error


def test_run_bounded_reports_a_command_that_could_not_start(tmp_path: Path):
    outcome = run_bounded([str(tmp_path / "no-such-binary")], cwd=tmp_path, timeout_s=5)
    assert not outcome.started
    assert outcome.exit_code is None
    assert not outcome.succeeded


def test_probe_tool_reports_absence_honestly(tmp_path: Path):
    probe = probe_tool("definitely-not-installed-xyz", cwd=tmp_path)
    assert not probe.available
    assert "not found on PATH" in probe.reason
    assert "never installs" in probe.reason


def test_probe_tool_records_a_version_for_a_present_tool(tmp_path: Path):
    probe = probe_tool(Path(sys.executable).name, version_argv=("--version",),
                       cwd=tmp_path)
    if not probe.available:
        pytest.skip("the interpreter is not resolvable by name on this host")
    assert probe.version
    assert probe.executable


def test_tool_probe_dict_does_not_leak_the_executable_path():
    probe = ToolProbe(name="ruff", available=True, version="ruff 0.1",
                      executable="/opt/secret/bin/ruff")
    assert "executable" not in probe.to_dict()


# ── aggregation: the SET of results, before the arithmetic ───────────────────
def test_duplicate_grader_result_is_refused():
    spec = make_spec()
    results = [*passing_results(), result("pytest")]
    with pytest.raises(SchemaError, match="duplicate result"):
        validate_result_set(spec, results)


def test_duplicate_result_becomes_a_blocked_report_not_an_exception():
    spec = make_spec()
    traj = make_trajectory(spec)
    report = aggregate(spec, traj, results=[*passing_results(), result("pytest")])
    assert not report.eligible_for_review
    assert report.integrity_violations
    assert report.reward.blocked
    assert report.reward.total == 0.0


def test_unknown_grader_id_is_refused():
    spec = make_spec()
    bad = GraderResult(grader_id="totally-made-up", grader_version="1",
                       status=ResultStatus.PASS, score=1.0,
                       non_vacuous_measurement=1)
    with pytest.raises(SchemaError, match="unknown grader id"):
        validate_result_set(spec, [*passing_results(), bad])


def test_a_result_the_task_never_requested_is_refused():
    spec = make_spec()
    with pytest.raises(SchemaError, match="did not request"):
        validate_result_set(spec, [*passing_results(), result("bandit")])


def test_a_raw_dict_in_the_result_set_is_refused():
    spec = make_spec()
    with pytest.raises(SchemaError, match="not a GraderResult"):
        validate_result_set(spec, [{"grader_id": "pytest", "status": "pass"}])


def test_missing_mandatory_grader_blocks():
    spec = make_spec()
    traj = make_trajectory(spec)
    partial = [r for r in passing_results() if r.grader_id != "file_boundary"]
    report = aggregate(spec, traj, results=partial)
    assert not report.eligible_for_review
    assert "file_boundary" in report.missing_graders
    assert any("file_boundary" in b for b in report.blockers)


@pytest.mark.parametrize("status", [ResultStatus.SKIPPED, ResultStatus.ERROR,
                                    ResultStatus.INSUFFICIENT_EVIDENCE,
                                    ResultStatus.FAIL,
                                    ResultStatus.NOT_APPLICABLE])
def test_every_non_affirmative_mandatory_status_blocks(status):
    spec = make_spec()
    traj = make_trajectory(spec)
    results = [r for r in passing_results() if r.grader_id != "pytest"]
    kw = {"error": "tool crashed"} if status is ResultStatus.ERROR else {}
    results.append(result("pytest", status, **kw))
    report = aggregate(spec, traj, results=results)
    assert not report.eligible_for_review
    assert any("pytest" in b for b in report.blockers)
    assert report.reward.total == 0.0


def test_blocking_security_finding_dominates_every_positive_dimension():
    spec = make_spec()
    traj = make_trajectory(spec)
    results = [r for r in passing_results() if r.grader_id != "secret_pii"]
    results.append(result("secret_pii", ResultStatus.FAIL, blocking=True,
                          severity=Severity.BLOCKING))
    report = aggregate(spec, traj, results=results)
    assert report.reward.blocked
    assert report.reward.total == 0.0
    assert not report.eligible_for_review
    assert any("dominates" in b for b in report.blockers)


def test_high_severity_dominates_even_when_the_grader_is_advisory():
    spec = make_spec(required_graders=("pytest", "diff_budget", "file_boundary",
                                       "secret_pii", "ruff"))
    traj = make_trajectory(spec)
    results = [*passing_results(),
               result("ruff", ResultStatus.FAIL, severity=Severity.HIGH)]
    report = aggregate(spec, traj, results=results)
    assert not report.eligible_for_review
    assert report.reward.total == 0.0
    assert any("severity high" in b for b in report.blockers)


def test_an_advisory_failure_stays_visible_without_blocking():
    spec = make_spec(
        required_graders=("pytest", "diff_budget", "file_boundary", "secret_pii",
                          "ruff"),
        scoring=P.ScoringPolicy(
            mandatory_graders=("pytest", "diff_budget", "file_boundary", "secret_pii"),
            min_total_score=0.5))
    traj = make_trajectory(spec)
    results = [*passing_results(), result("ruff", ResultStatus.FAIL)]
    report = aggregate(spec, traj, results=results)
    assert report.eligible_for_review
    assert any("ruff" in a for a in report.advisories)


def test_zero_affirmative_measurements_prevents_approval():
    spec = make_spec()
    traj = make_trajectory(spec)
    results = [result(g, ResultStatus.NOT_APPLICABLE)
               for g in ("pytest", "diff_budget", "file_boundary", "secret_pii")]
    report = aggregate(spec, traj, results=results)
    assert not report.eligible_for_review
    assert report.affirmative_graders == 0
    assert report.reward.total == 0.0


def test_a_teacher_cannot_lift_a_deterministic_failure():
    spec = make_spec()
    traj = make_trajectory(spec)
    results = [r for r in passing_results() if r.grader_id != "pytest"]
    results.append(result("pytest", ResultStatus.FAIL))
    traj.add_teacher_review(TeacherReview(
        provider="anthropic", model="claude", task_hash=spec.spec_hash(),
        attempt_hash=traj.attempt_hash(), rubric_version="r1", overall_score=1.0,
        recommendation=Recommendation.APPROVE, timestamp="2026-07-31T00:00:00Z"))
    report = aggregate(spec, traj, results=results)
    assert not report.eligible_for_review
    assert report.reward.total == 0.0


def test_a_complete_affirmative_result_set_is_eligible_for_review_only():
    spec = make_spec()
    traj = make_trajectory(spec)
    report = aggregate(spec, traj, results=passing_results())
    assert report.eligible_for_review
    assert not report.blockers
    assert report.affirmative_graders == 4
    assert report.reward.total > 0.0
    # Eligible is the weakest claim the evidence supports, and it is never approval.
    assert report.approved is False
    assert report.to_dict()["approved"] is False


def test_aggregation_report_refuses_to_claim_eligibility_with_blockers():
    with pytest.raises(SchemaError, match="blocking finding dominates"):
        AggregationReport(version="v", task_id="t", attempt_id="a",
                            reward=RewardBreakdown(), eligible_for_review=True,
                            blockers=("something failed",))


def test_aggregation_report_refuses_eligibility_with_a_blocked_reward():
    with pytest.raises(SchemaError, match="blocked reward"):
        AggregationReport(version="v", task_id="t", attempt_id="a",
                            reward=RewardBreakdown(blocked=True,
                                                   block_reasons=("x",)),
                            eligible_for_review=True)


def test_aggregation_record_is_hashable_and_carries_no_host_path(tmp_path: Path):
    spec = make_spec()
    traj = make_trajectory(spec)
    report = aggregate(spec, traj, results=passing_results())
    payload = report.to_dict()
    assert str(tmp_path) not in repr(payload)
    assert len(report.report_hash()) == 64


def test_attach_results_replaces_rather_than_appends():
    spec = make_spec()
    traj = make_trajectory(spec)
    attach_results(spec, traj, passing_results())
    attach_results(spec, traj, passing_results())
    assert len(traj.grader_results) == 4


def test_requested_graders_includes_every_mandatory_id():
    spec = make_spec()
    requested = set(requested_graders(spec))
    assert set(spec.scoring.mandatory_graders) <= requested
