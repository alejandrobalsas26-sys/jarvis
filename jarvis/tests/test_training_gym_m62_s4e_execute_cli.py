"""V69 M62 S4E — the ``--execute`` surface, and everything it refuses.

WHY THIS FILE EXISTS
--------------------
The S4E preauth reported `V4_REAL_EXECUTOR: PASS` on the strength of the orchestrator
module and its tests. That was true of the module and false of the COMMAND: the only
callers of ``execute_v4_evaluation`` were tests, so the frozen plan described a source
state in which no tracked entry point could consume an operator's token and run the exam.
The gap was found by grepping for production callers after the token arrived, with the
holdout still unspent.

This suite exists so that can never be true again: it asserts that the command exists,
that it is the ONLY mode which could reach a model, and that every way of approaching it
without the exact token is refused before anything is created.

NOTHING HERE LOADS A WEIGHT. Every assertion is about a refusal or about a shape.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from scripts import evaluate_reference_pair as CLI

_BANNED = ("torch", "transformers", "peft", "trl", "accelerate", "bitsandbytes",
           "safetensors")


# ══════════════════════════════════════════════════════════════════════════════
#  The command exists and is wired to the orchestrator
# ══════════════════════════════════════════════════════════════════════════════
def test_the_execute_mode_is_registered():
    """The regression that produced this file: a plan nobody could execute."""
    parser = CLI.build_parser()
    actions = {a.dest for a in parser._actions}  # noqa: SLF001 — the registry IS the fact
    assert "execute" in actions
    assert "confirm" in actions


def test_execute_actually_calls_the_v4_orchestrator():
    """Asserted over the SOURCE, so a mode that exists and does nothing still fails."""
    source = Path(CLI.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_execute")
    called = {n.func.id for n in ast.walk(func)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "execute_v4_evaluation" in called
    assert "check_v4_confirmation" in called, (
        "the execute path must consume the authority, not merely accept it")


def test_main_dispatches_execute():
    source = Path(CLI.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    main = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    assert "_execute" in {n.func.id for n in ast.walk(main)
                          if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def test_no_mode_but_execute_can_reach_the_orchestrator():
    """Every other mode is metadata. Asserted structurally, not promised in a docstring."""
    source = Path(CLI.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for name in ("_check_artifacts", "_print_plan", "_derive_plan"):
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name)
        called = {n.func.id for n in ast.walk(func)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "execute_v4_evaluation" not in called, name


# ══════════════════════════════════════════════════════════════════════════════
#  The authority gate
# ══════════════════════════════════════════════════════════════════════════════
class _Plan:
    """A stand-in carrying only what the confirmation check reads."""

    def __init__(self, digest: str = "a" * 64) -> None:
        self._digest = digest

    def plan_hash(self) -> str:
        return self._digest

    def confirmation_token(self) -> str:
        return f"EVAL:{self._digest}"


@pytest.mark.parametrize("token", [
    "", "yes", "true", "go", "run", "proceed", "continue", "dale", "--force",
    "EVAL:", "EVAL:deadbeef", "EVAL:" + "b" * 64, "eval:" + "a" * 64,
    "TRAIN:" + "a" * 64, "@/tmp/token.txt", "/tmp/token",
])
def test_only_the_exact_token_authorises(token):
    from training_gym.evaluation.plan_v4 import (
        PlanV4ConfirmationRejected,
        check_v4_confirmation,
    )

    with pytest.raises(PlanV4ConfirmationRejected):
        check_v4_confirmation(token, _Plan())


def test_the_exact_token_is_accepted():
    from training_gym.evaluation.plan_v4 import check_v4_confirmation

    plan = _Plan()
    assert check_v4_confirmation(f"EVAL:{'a' * 64}", plan) == f"EVAL:{'a' * 64}"


def test_a_token_with_surrounding_whitespace_is_still_the_same_token():
    from training_gym.evaluation.plan_v4 import check_v4_confirmation

    assert check_v4_confirmation(f"  EVAL:{'a' * 64}  ", _Plan())


# ══════════════════════════════════════════════════════════════════════════════
#  The control-plane second opinion
# ══════════════════════════════════════════════════════════════════════════════
def test_a_spent_holdout_is_refused_by_the_control_plane_guard():
    """eval-v6 is USED_IMMUTABLE. No ledger anywhere makes it unspent again."""
    problems = CLI._control_plane_blockers("m62-defensive-eval", "v6")
    assert problems
    assert any("USED_IMMUTABLE" in p or "spender" in p for p in problems)


def test_an_unknown_holdout_is_refused():
    problems = CLI._control_plane_blockers("m62-defensive-eval", "v99")
    assert problems
    assert any("never heard of" in p for p in problems)


def test_the_live_holdout_passes_the_guard_while_it_is_unspent():
    """A guard that refused everything would prove nothing."""
    assert CLI._control_plane_blockers("m62-defensive-eval", "v7") == []


# ══════════════════════════════════════════════════════════════════════════════
#  Progress is body-free by construction
# ══════════════════════════════════════════════════════════════════════════════
def test_the_progress_line_carries_only_counts_and_identifiers(capsys):
    CLI._progress({"task_index": 7, "task_count": 36, "task_id": "synthetic-0007",
                   "arm": "reference", "status": "succeeded", "latency_ms": 15700})
    out = capsys.readouterr().out
    assert "TASK 07/36" in out and "REFERENCE" in out and "succeeded" in out
    assert "synthetic-0007" not in out, (
        "the progress line must not name the task; a per-task id printed 72 times is the "
        "exam's contents list read aloud")


def test_the_progress_body_has_no_field_a_response_could_occupy():
    from training_gym.evaluation import runner_v4 as V4

    source = Path(V4.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # The dict literal handed to on_arm_complete, found by its unique key.
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "task_index"
                for k in node.keys):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            assert keys == {"task_index", "task_count", "task_id", "arm", "status",
                            "latency_ms"}
            return
    pytest.fail("the progress body was not found; it must stay a closed literal")


def test_importing_the_command_loads_no_machine_learning_framework():
    assert sorted(set(_BANNED) & set(sys.modules)) == []
