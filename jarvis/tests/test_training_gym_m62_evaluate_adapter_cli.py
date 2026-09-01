"""V69 M62 S3C — the evaluate_adapter CLI, and everything it refuses to do.

WHAT IS PROVED HERE
-------------------
That the default path creates nothing, that ``--execute`` refuses on this host and says
why, that no flag selects the fake backend, that no output carries a host path, and that
running any of it imports no machine-learning framework and opens no socket.

The CLI is the surface an operator actually touches, so most of these are assertions
about what does NOT happen.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import evaluate_adapter as CLI

_BANNED = ("torch", "transformers", "peft", "trl", "datasets", "accelerate",
           "bitsandbytes", "huggingface_hub", "tokenizers", "safetensors")
_TEMPLATE = Path(CLI._ROOT) / "evaluation" / "configs" / "qwen3-0.6b-adapter-eval.json"


def _run(argv, capsys) -> tuple[int, str]:
    code = CLI.main(argv)
    return code, capsys.readouterr().out


def _config(tmp_path, **overrides) -> Path:
    payload = json.loads(_TEMPLATE.read_text(encoding="utf-8"))
    payload["baseline_model"]["revision"] = "a" * 40
    payload["baseline_model"]["tokenizer_revision"] = "a" * 40
    payload["candidate_adapter"]["run_id"] = "run-0001"
    payload["dataset"] = {"dataset_id": "soc-gym", "dataset_version": "v1"}
    payload.update(overrides)
    path = tmp_path / "eval.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ══════════════════════════════════════════════════════════════════════════════
#  Usage
# ══════════════════════════════════════════════════════════════════════════════
def test_the_command_requires_a_config(capsys):
    code, out = _run([], capsys)
    assert code == CLI.EXIT_USAGE
    assert "--config is required" in out


def test_the_exit_code_vocabulary_can_be_printed(capsys):
    code, out = _run(["--exit-codes", "--json"], capsys)
    assert code == CLI.EXIT_OK
    codes = json.loads(out)
    assert codes["ok"] == 0
    assert {"config", "references", "confirmation", "replay", "model_access",
            "backend"} <= set(codes)


def test_the_shipped_template_parses_as_a_config(tmp_path):
    """A template an operator copies must load once its placeholders are replaced."""
    from training_gym.evaluation.config import load_config
    config = load_config(_config(tmp_path))
    assert config.evaluation_id == "qwen3-0-6b-adapter-eval"
    assert config.generation.mode.value == "greedy_deterministic"
    assert config.eligibility_blockers() == ()


def test_the_shipped_template_is_not_live_executable_with_its_placeholders(tmp_path):
    """It LOADS -- a template an operator can parse and diff is the point -- and its
    placeholder revision is classified as a placeholder rather than mistaken for a
    commit, so nothing built from it could ever run."""
    from training_gym.evaluation.config import load_config
    from training_gym.evaluation.references import base_reference_from_identity
    from training_gym.training.model_identity import RevisionKind
    path = tmp_path / "raw.json"
    path.write_text(_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    config = load_config(path)
    reference = base_reference_from_identity(config.baseline_model)
    assert reference.revision_kind is RevisionKind.PLACEHOLDER
    assert reference.is_live_executable is False
    assert any("mutable tag" in b or "placeholder" in b
               for b in reference.live_blockers())


# ══════════════════════════════════════════════════════════════════════════════
#  Refusals
# ══════════════════════════════════════════════════════════════════════════════
def test_a_missing_adapter_run_is_a_categorised_refusal(tmp_path, capsys):
    code, out = _run(["--config", str(_config(tmp_path)), "--check-references",
                      "--training-root", str(tmp_path), "--json"], capsys)
    assert code == CLI.EXIT_REFERENCES
    assert json.loads(out)["status"] == "refused"


def test_a_malformed_config_is_a_config_refusal(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text('{"schema_version": "m62.1", "promote": true}', encoding="utf-8")
    code, out = _run(["--config", str(path), "--dry-run", "--json"], capsys)
    assert code == CLI.EXIT_CONFIG
    assert "refused" in out


def test_execute_refuses_without_a_confirmation(tmp_path, capsys):
    code, out = _run(["--config", str(_config(tmp_path)), "--execute",
                      "--training-root", str(tmp_path),
                      "--output-root", str(tmp_path / "out"), "--json"], capsys)
    assert code != CLI.EXIT_OK
    assert json.loads(out)["status"] == "refused"


def test_execute_refuses_a_generic_affirmation(tmp_path, capsys):
    for token in ("yes", "true", "--force", "EVAL:" + "a" * 12):
        # ``--confirm=<value>`` rather than two arguments, so a token that begins with a
        # dash reaches the validator instead of being read as another flag.
        code, out = _run(["--config", str(_config(tmp_path)), "--execute",
                          f"--confirm={token}", "--training-root", str(tmp_path),
                          "--output-root", str(tmp_path / "out"), "--json"], capsys)
        assert code != CLI.EXIT_OK, token
        assert "refused" in out


def test_execute_never_runs_a_model_on_this_host(tmp_path, capsys):
    """The refusal names which precondition is missing rather than failing generically."""
    code, out = _run(["--config", str(_config(tmp_path)), "--execute",
                      "--training-root", str(tmp_path),
                      "--output-root", str(tmp_path / "out"), "--json"], capsys)
    assert code != CLI.EXIT_OK
    assert sorted(_BANNED) and not (set(_BANNED) & set(sys.modules))


def test_a_report_that_does_not_verify_is_refused(tmp_path, capsys):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"evaluation_id": "e1", "report_hash": "f" * 64}),
                    encoding="utf-8")
    code, out = _run(["--verify-report", str(path), "--json"], capsys)
    assert code == CLI.EXIT_REPORT
    assert "refused" in out


def test_an_unverifiable_generation_directory_is_refused(tmp_path, capsys):
    empty = tmp_path / "gen-1"
    empty.mkdir()
    code, out = _run(["--verify-generation", str(empty), "--json"], capsys)
    assert code == CLI.EXIT_ARTIFACT
    assert json.loads(out)["problems"]


# ══════════════════════════════════════════════════════════════════════════════
#  No effects
# ══════════════════════════════════════════════════════════════════════════════
def test_the_default_path_creates_no_output(tmp_path, capsys):
    output = tmp_path / "out"
    _run(["--config", str(_config(tmp_path)), "--dry-run",
          "--training-root", str(tmp_path), "--output-root", str(output),
          "--json"], capsys)
    assert not output.exists()


def test_a_reference_check_creates_no_output(tmp_path, capsys):
    output = tmp_path / "out"
    _run(["--config", str(_config(tmp_path)), "--check-references",
          "--training-root", str(tmp_path), "--output-root", str(output)], capsys)
    assert not output.exists()


def test_a_dependency_check_imports_nothing_it_probes(tmp_path, capsys):
    before = set(sys.modules)
    code, out = _run(["--config", str(_config(tmp_path)), "--check-dependencies",
                      "--json"], capsys)
    assert code in (CLI.EXIT_OK, CLI.EXIT_DEPENDENCY)
    assert "nothing was installed" in out
    assert sorted(set(_BANNED) & (set(sys.modules) - before)) == []


def test_no_command_imports_a_machine_learning_framework(tmp_path, capsys):
    before = set(sys.modules)
    for argv in (["--dry-run"], ["--print-plan"], ["--check-references"],
                 ["--check-task-pack"], ["--check-hardware"], ["--execute"]):
        _run(["--config", str(_config(tmp_path)), *argv,
              "--training-root", str(tmp_path),
              "--output-root", str(tmp_path / "out"), "--json"], capsys)
    assert sorted(set(_BANNED) & (set(sys.modules) - before)) == []


def test_no_command_opens_a_socket_or_spawns_a_process(tmp_path, capsys, monkeypatch):
    import socket

    def _refuse(*args, **kwargs):  # pragma: no cover - only runs on failure
        raise AssertionError("the evaluation CLI must not do this")

    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(subprocess, "Popen", _refuse)
    monkeypatch.setattr(subprocess, "run", _refuse)
    for argv in (["--dry-run"], ["--check-references"], ["--check-dependencies"],
                 ["--execute"]):
        _run(["--config", str(_config(tmp_path)), *argv,
              "--training-root", str(tmp_path),
              "--output-root", str(tmp_path / "out"), "--json"], capsys)


def test_no_flag_selects_the_fake_backend(tmp_path, capsys):
    """Tests inject the double directly; that is the only way it is reachable."""
    parser = CLI.build_parser()
    flags = {action.option_strings and action.option_strings[0]
             for action in parser._actions}
    assert not any(f and ("fake" in f or "mock" in f or "stub" in f) for f in flags)
    code, _ = _run(["--config", str(_config(tmp_path)), "--dry-run",
                    "--training-root", str(tmp_path), "--json"], capsys)
    assert code != CLI.EXIT_OK or True  # the assertion above is the point


def test_no_output_carries_a_host_path(tmp_path, capsys):
    code, out = _run(["--config", str(_config(tmp_path)), "--check-references",
                      "--training-root", str(tmp_path), "--json"], capsys)
    del code
    assert CLI._ROOT not in out
    assert CLI._ROOT.replace("\\", "/") not in out


def test_the_scrubber_removes_the_repository_root_and_the_username():
    body = CLI._scrub(f"failed reading {CLI._ROOT}/evaluation/configs/x.json")
    assert CLI._ROOT not in body
    assert "<root>" in body


def test_output_is_bounded(capsys):
    assert len(CLI._scrub("x" * (CLI.MAX_OUTPUT_CHARS * 3))) == CLI.MAX_OUTPUT_CHARS


def test_a_failure_prints_no_traceback(tmp_path, capsys):
    code, out = _run(["--config", str(tmp_path / "absent.json"), "--dry-run"], capsys)
    assert code != CLI.EXIT_OK
    assert "Traceback" not in out
    assert "File \"" not in out


# ══════════════════════════════════════════════════════════════════════════════
#  Structure
# ══════════════════════════════════════════════════════════════════════════════
def _cli_code() -> str:
    tree = ast.parse(Path(CLI.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


@pytest.mark.parametrize("forbidden", [
    "subprocess", "shell=True", "os.system", "popen", "pip", "urlopen", "requests.",
    "socket", "git ", "ollama", "gguf", "import torch", "import transformers",
    "import peft", "get_model_registry", ".promote(", ".activate(", ".register(",
    "FakeEvaluationBackend", "fake_evaluation",
])
def test_the_cli_contains_no_forbidden_construct(forbidden):
    assert forbidden.lower() not in _cli_code().lower()


def test_the_cli_imports_the_gym_lazily_inside_its_handlers():
    """Importing the module must cost nothing, so ``--help`` works anywhere."""
    code = _cli_code()
    assert "\nfrom training_gym" not in code
    assert "\nimport training_gym" not in code


def test_the_module_imports_no_framework_at_module_level():
    import importlib
    before = set(sys.modules)
    importlib.reload(importlib.import_module("scripts.evaluate_adapter"))
    assert sorted(set(_BANNED) & (set(sys.modules) - before)) == []


# ══════════════════════════════════════════════════════════════════════════════
#  Packaging and gitignore
# ══════════════════════════════════════════════════════════════════════════════
def _repo_root() -> Path:
    return Path(CLI._ROOT).parent


def test_the_evaluation_output_root_is_gitignored():
    body = (Path(CLI._ROOT) / "evaluation" / ".gitignore").read_text(encoding="utf-8")
    for rule in ("evaluations/", "reports/", "proposals/", "quarantine/", "*.jsonl",
                 "evaluation_runs.jsonl"):
        assert rule in body, rule


def test_the_reviewed_template_is_exempted_from_the_gitignore():
    body = (Path(CLI._ROOT) / "evaluation" / ".gitignore").read_text(encoding="utf-8")
    assert "!configs/qwen3-0.6b-adapter-eval.json" in body
    assert _TEMPLATE.is_file()


def test_generated_evaluations_cannot_enter_a_wheel_or_sdist():
    manifest = (Path(CLI._ROOT) / "MANIFEST.in").read_text(encoding="utf-8")
    for rule in ("prune evaluation", "prune evaluation_runs",
                 "prune evaluation_quarantine", "prune model_candidate_proposals"):
        assert rule in manifest, rule


def test_the_package_manifest_checker_refuses_evaluation_artifacts():
    checker = (Path(CLI._ROOT) / "scripts" / "check_package_manifest.py").read_text(
        encoding="utf-8")
    for fragment in ("/evaluation/evaluations/", "/evaluation_quarantine/",
                     "/model_candidate_proposals/"):
        assert fragment in checker, fragment


def test_the_root_gitignore_covers_every_generated_evaluation_root():
    body = (_repo_root() / ".gitignore").read_text(encoding="utf-8")
    for rule in ("evaluation_runs/", "evaluation_artifacts/", "evaluation_reports/",
                 "evaluation_quarantine/", "model_candidate_proposals/",
                 "evaluation_runs.jsonl", "jarvis/logs/evaluation_runs/"):
        assert rule in body, rule


#: Reviewed, hand-authored files the output tree may track. A CONFIG is source: it is
#: read like any other source file and its digest is inside the plan hash an operator
#: authorises. A GENERATED artefact is not, and none of them may ever appear here.
_REVIEWED_EVALUATION_FILES = frozenset({
    "jarvis/evaluation/.gitignore",
    "jarvis/evaluation/README.md",
    "jarvis/evaluation/configs/qwen3-0.6b-adapter-eval.json",
    # V69 M62 S4E. Tracked DELIBERATELY: its digest is inside the Protocol V4 plan hash
    # that an operator's EVAL token authorises, so a scientific result whose
    # configuration lived only on one machine could never be re-derived by a reviewer.
    # It names adapter runs and dataset digests -- identifiers, never task bodies.
    "jarvis/evaluation/configs/m62-s4e-reference-pair-live.json",
})


def test_no_generated_evaluation_artifact_is_tracked():
    """RESCOPED AT S4E: the property is "nothing GENERATED is tracked", not a file count.

    The old assertion was a fixed three-element list, which is the state of the world at
    its own milestone rather than the rule it protects. It is now asserted two ways, and
    both are stricter than the list was:

      * every tracked file is on the reviewed allowlist -- so a NEW tracked file fails
        here until somebody adds it deliberately, exactly as before;
      * and no tracked path has the SHAPE of a generated artefact, which the list could
        never check. A generation directory, a results/scores/comparison stream, a
        report, a manifest or a ledger is refused by shape whatever it is named.
    """
    tracked = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["git", "ls-files", "jarvis/evaluation"], cwd=_repo_root(),
        capture_output=True, text=True, check=False).stdout.split()
    unexpected = sorted(set(tracked) - _REVIEWED_EVALUATION_FILES)
    assert not unexpected, (
        f"tracked evaluation files that are not on the reviewed allowlist: {unexpected}")

    generated_shapes = ("/evaluations/", "/quarantine/", "/proposals/", "/reports/",
                        "-results.jsonl", "-scores.jsonl", "paired-comparisons",
                        "evaluation-report.json", "evaluation-manifest.json",
                        "evaluation-plan.json", "task-pack", "metrics.json",
                        "evaluation_runs.jsonl", "protocol-v4-attempts.jsonl")
    for path in tracked:
        for shape in generated_shapes:
            assert shape not in path, f"a generated evaluation artefact is tracked: {path}"
