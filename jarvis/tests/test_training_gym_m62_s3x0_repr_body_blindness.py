"""V69 M62 S3X.0 — a held-out body must not survive being *displayed*.

WHAT HAPPENED
-------------
An orchestration session received one held-out evaluation prompt before any
authorisation existed. No model was loaded, no token was generated and no holdout was
spent — the body arrived through Python's default representation machinery alone.

Three ordinary features compose into it:

  1. ``@dataclass`` generates a ``__repr__`` rendering EVERY field, so a record holding
     ``user_prompt`` prints the prompt.
  2. A container's repr recurses into its elements, so a pack prints the whole corpus.
  3. ``repr`` of a BOUND METHOD is ``<bound method C.m of {self!r}>``. Displaying
     ``pack.pack_hash`` WITHOUT CALLING IT renders every task body in the pack.

Route 3 is the one that fired, and it is why closing ``repr(pack)`` alone is not a fix:
there is no ``__repr__`` on a method object to override. The only defence is that the
pack's OWN repr — which the method repr interpolates — is already body-free.

WHY THIS SUITE IS SEPARATE FROM ``s3q0_body_blindness``
-------------------------------------------------------
That suite proves written ARTEFACTS and refusal MESSAGES carry no body. This one proves
IN-MEMORY REPRESENTATION carries no body. The incident passed straight through the first
boundary because it never touched an artefact or an exception — it was a display.

BODY-FREE BY CONSTRUCTION
-------------------------
Every fixture here is synthetic and carries an unmistakable canary. Nothing in this file
reads, imports, hashes or names any real held-out corpus — the frozen v5 evaluation
holdout included. That prohibition is asserted, not promised: see
:func:`test_this_suite_names_no_real_evaluation_corpus`.

NON-VACUITY
-----------
A test asserting "no canary appears" would pass against an implementation that rendered
nothing at all. :func:`test_the_probe_catches_a_leak_when_the_guard_is_removed` puts the
body-rendering repr back and requires the SAME probes to fail, so a future contributor who
deletes a guard gets a red test rather than a quiet leak.
"""
from __future__ import annotations

import dataclasses
import hashlib
import inspect
import io
import logging
import pathlib
import pkgutil
import importlib

import pytest

import training_gym
from training_gym.datasets.candidate import DatasetSplit, TaskFamily
from training_gym.evaluation.task_pack import (
    EvaluationTask,
    EvaluationTaskKind,
    EvaluationTaskPack,
    HiddenTarget,
)
from training_gym.schemas import body_free_repr, sha256_text


PROMPT_CANARY = "SYNTHETIC_PROMPT_CANARY_DO_NOT_PERSIST"
TARGET_CANARY = "SYNTHETIC_TARGET_CANARY_DO_NOT_PERSIST"
CANARIES = (PROMPT_CANARY, TARGET_CANARY)

#: Field names that hold free text a model wrote, was given, or must produce. A dataclass
#: carrying one of these may never fall back on the generated repr.
BODY_FIELD_NAMES = frozenset({
    "system_prompt", "user_prompt", "target_text", "system_message", "prompt",
    "response_text", "output_text", "completion", "text",
})


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def leaked(text: str) -> list[str]:
    """Which canaries *text* discloses. The single detection primitive of this file."""
    return [canary for canary in CANARIES if canary in text]


# ══════════════════════════════════════════════════════════════════════════════
#  Synthetic fixtures — fake bodies, never a real corpus
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def task() -> EvaluationTask:
    return EvaluationTask(
        task_id="s3x0-synthetic-001",
        task_family=TaskFamily.SAFETY_REFUSAL,
        task_hash=_digest("s3x0-task"),
        split=DatasetSplit.HIDDEN_EVALUATION,
        kind=EvaluationTaskKind.REQUIRED_REFUSAL,
        system_prompt=f"system {PROMPT_CANARY}",
        user_prompt=f"user {PROMPT_CANARY}",
        source_dataset_manifest_hash=_digest("s3x0-manifest"),
        source_shard_hash=_digest("s3x0-shard"),
        input_record_hash=_digest("s3x0-record"),
        grader_ids=("safety_policy",),
        mandatory_grader_ids=("safety_policy",),
    )


@pytest.fixture
def pack(task) -> EvaluationTaskPack:
    return EvaluationTaskPack(tasks=(task,), dataset_id="s3x0-synthetic-pack",
                              dataset_version="v0")


@pytest.fixture
def hidden_target(task) -> HiddenTarget:
    return HiddenTarget(task_id=task.task_id, task_hash=task.task_hash,
                        target_text=TARGET_CANARY,
                        target_hash=sha256_text(TARGET_CANARY))


# ══════════════════════════════════════════════════════════════════════════════
#  The canaries are really in the material
# ══════════════════════════════════════════════════════════════════════════════
def test_the_canaries_are_actually_present_in_the_synthetic_bodies(task, hidden_target):
    """Without this, every assertion below could pass over empty material."""
    assert PROMPT_CANARY in task.user_prompt
    assert PROMPT_CANARY in task.system_prompt
    assert TARGET_CANARY in hidden_target.target_text


# ══════════════════════════════════════════════════════════════════════════════
#  Direct representation
# ══════════════════════════════════════════════════════════════════════════════
def test_the_task_repr_carries_no_body(task):
    assert leaked(repr(task)) == []


def test_the_pack_repr_carries_no_body(pack):
    assert leaked(repr(pack)) == []


def test_the_hidden_target_repr_never_discloses_the_answer(hidden_target):
    assert leaked(repr(hidden_target)) == []


@pytest.mark.parametrize("render", [str, repr, lambda o: f"{o}", lambda o: f"{o!r}",
                                    lambda o: "%s" % (o,), lambda o: "%r" % (o,),
                                    lambda o: "{}".format(o), lambda o: format(o)])
def test_no_string_conversion_route_discloses_a_body(pack, task, hidden_target, render):
    """``str``/``repr``/f-string/``%``/``format`` are five doors into the same room."""
    for obj in (pack, task, hidden_target):
        assert leaked(render(obj)) == [], (render, type(obj).__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  The bound-method route — the one that actually fired
# ══════════════════════════════════════════════════════════════════════════════
def _bound_callables(obj):
    """Every callable attribute bound to *obj*, which is every repr that embeds it."""
    out = []
    for name in dir(obj):
        if name.startswith("__"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:  # noqa: BLE001 — a property that raises is not our concern here
            continue
        if callable(value) and getattr(value, "__self__", None) is obj:
            out.append((name, value))
    return out


def test_the_pack_exposes_bound_methods_at_all(pack):
    """Non-vacuity for the sweep below: there is something to sweep."""
    names = {name for name, _ in _bound_callables(pack)}
    assert {"pack_hash", "to_dict", "counts_by_split", "counts_by_family"} <= names


def test_no_bound_method_of_the_pack_discloses_a_body(pack):
    """The historical route: ``repr(method)`` interpolates ``repr(method.__self__)``.

    Displaying a method WITHOUT CALLING IT rendered the corpus. Sweeping every bound
    callable — rather than the one method that happened to be typed — is the point.
    """
    for name, method in _bound_callables(pack):
        assert leaked(repr(method)) == [], f"pack.{name}"


def test_no_bound_method_of_a_task_discloses_a_body(task):
    for name, method in _bound_callables(task):
        assert leaked(repr(method)) == [], f"task.{name}"


def test_no_bound_method_of_a_hidden_target_discloses_the_answer(hidden_target):
    for name, method in _bound_callables(hidden_target):
        assert leaked(repr(method)) == [], f"hidden_target.{name}"


def test_the_bound_method_repr_still_names_its_object(pack):
    """Body-free must not mean useless: the method repr keeps its debugging value."""
    rendered = repr(pack.pack_hash)
    assert "bound method" in rendered
    assert "EvaluationTaskPack.pack_hash" in rendered
    assert "s3x0-synthetic-pack" in rendered


# ══════════════════════════════════════════════════════════════════════════════
#  Logging and exception formatting
# ══════════════════════════════════════════════════════════════════════════════
def test_debug_logging_a_pack_discloses_no_body(pack, caplog):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("s3x0.body_blindness")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        logger.debug("pack=%r task=%r method=%r", pack, pack.tasks[0], pack.pack_hash)
        logger.debug("interpolated %s", pack)
        handler.flush()
    finally:
        logger.removeHandler(handler)
    assert leaked(stream.getvalue()) == []


def test_exception_interpolation_discloses_no_body(pack, hidden_target):
    with pytest.raises(ValueError) as excinfo:
        raise ValueError(f"refusing {pack!r} for {hidden_target!r} via {pack.to_dict!r}")
    assert leaked(str(excinfo.value)) == []


def test_a_traceback_render_discloses_no_body(pack):
    """A crash report is a debug surface like any other."""
    import traceback
    try:
        raise RuntimeError("boom %r" % (pack.counts_by_split,))
    except RuntimeError:
        rendered = traceback.format_exc()
    assert leaked(rendered) == []


# ══════════════════════════════════════════════════════════════════════════════
#  Non-vacuity: put the leak back and require the probes to catch it
# ══════════════════════════════════════════════════════════════════════════════
def test_the_probe_catches_a_leak_when_the_guard_is_removed(pack, monkeypatch):
    """Restore a body-rendering repr and require every probe above to go red.

    This is what stops the suite degrading into "no canary was found because nothing was
    rendered". It exercises the REAL chain: task repr -> tuple recursion -> pack repr ->
    bound-method repr.
    """
    monkeypatch.setattr(
        EvaluationTask, "__repr__",
        lambda self: (f"EvaluationTask(task_id={self.task_id!r}, "
                      f"user_prompt={self.user_prompt!r})"),
        raising=True)
    monkeypatch.setattr(
        EvaluationTaskPack, "__repr__",
        lambda self: f"EvaluationTaskPack(tasks={self.tasks!r})", raising=True)

    assert leaked(repr(pack)) == [PROMPT_CANARY]
    assert leaked(repr(pack.tasks[0])) == [PROMPT_CANARY]
    assert leaked(repr(pack.pack_hash)) == [PROMPT_CANARY], (
        "the bound-method route must be detectable, or the historical incident could "
        "recur without a failing test")
    assert leaked("%r" % (pack,)) == [PROMPT_CANARY]


def test_the_probe_catches_a_hidden_target_leak_when_the_guard_is_removed(
        hidden_target, monkeypatch):
    monkeypatch.setattr(
        HiddenTarget, "__repr__",
        lambda self: f"HiddenTarget(target_text={self.target_text!r})", raising=True)
    assert leaked(repr(hidden_target)) == [TARGET_CANARY]
    assert leaked(repr(hidden_target.to_dict)) == [TARGET_CANARY]


def test_a_dataclass_left_on_the_generated_repr_really_does_leak():
    """The generated repr is the hazard, stated as an executable fact rather than prose."""
    @dataclasses.dataclass(frozen=True)
    class Unguarded:
        task_id: str
        user_prompt: str

    unguarded = Unguarded(task_id="x", user_prompt=PROMPT_CANARY)
    assert leaked(repr(unguarded)) == [PROMPT_CANARY]
    assert leaked(repr((unguarded,))) == [PROMPT_CANARY]


# ══════════════════════════════════════════════════════════════════════════════
#  The rule, enforced structurally rather than class by class
# ══════════════════════════════════════════════════════════════════════════════
#: ``jarvis/training_gym/training/`` is under a PERMANENT freeze, asserted by
#: ``test_training_gym_m62_s3q0_control_plane.py::test_the_graders_and_the_refusal_detector_are_untouched``
#: against the working tree. S3X.0 may not edit it, so ``ConvertedRecord`` keeps the
#: generated repr for now. This is a deferral with a named owner, not an allowlist:
#: the module holds TRAINING corpus rows and never evaluation-holdout material, and
#: :func:`test_the_frozen_training_surface_is_the_only_reason_for_the_exclusion` fails
#: the moment that freeze is lifted, forcing the decision to be retaken.
FROZEN_SURFACE_PREFIX = "training_gym.training."


def _body_bearing_dataclasses():
    found = {}
    for module_info in pkgutil.walk_packages(training_gym.__path__,
                                             training_gym.__name__ + "."):
        try:
            module = importlib.import_module(module_info.name)
        except Exception:  # noqa: BLE001 — an optional dependency is not a leak
            continue
        for obj in vars(module).values():
            if not (inspect.isclass(obj) and dataclasses.is_dataclass(obj)):
                continue
            if obj.__module__ != module.__name__:
                continue
            names = {f.name for f in dataclasses.fields(obj)}
            if names & BODY_FIELD_NAMES:
                found[f"{obj.__module__}.{obj.__qualname__}"] = obj
    return found


def test_the_frozen_training_surface_is_the_only_reason_for_the_exclusion():
    """The exclusion below must expire the moment the freeze that forces it does.

    If ``jarvis/training_gym/training/`` ever becomes editable, this test fails and the
    exclusion has to be justified again on its merits rather than inherited.
    """
    import subprocess

    excluded = {name for name in _body_bearing_dataclasses()
                if name.startswith(FROZEN_SURFACE_PREFIX)}
    if not excluded:
        return  # nothing is being excluded, so there is nothing to justify

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    frozen_test = (repo_root / "jarvis/tests/"
                   "test_training_gym_m62_s3q0_control_plane.py")
    try:
        source = frozen_test.read_text("utf-8")
    except OSError:  # pragma: no cover - the freeze test is part of this repository
        pytest.skip("the S3Q.0 control-plane suite is not present")
    assert 'assert not path.startswith("jarvis/training_gym/training/")' in source, (
        "the freeze that justifies excluding " + ", ".join(sorted(excluded)) +
        " is gone; give those classes a body-free repr instead")


def test_the_sweep_finds_the_known_body_bearing_containers():
    """Non-vacuity for the invariant below: the sweep is not returning an empty set."""
    found = _body_bearing_dataclasses()
    assert "training_gym.evaluation.task_pack.EvaluationTask" in found
    assert "training_gym.evaluation.task_pack.HiddenTarget" in found
    assert "training_gym.datasets.candidate.DatasetCandidate" in found
    assert len(found) >= 8


def test_no_body_bearing_dataclass_relies_on_the_generated_repr():
    """The architectural invariant, with no exceptions list to rot.

    A dataclass carrying a body field must define ``__repr__`` in its OWN module. The
    generated one is compiled inside ``dataclasses``/``reprlib``, so comparing the code
    object's file against the class's file separates the two without an allowlist.
    """
    offenders = []
    for name, cls in sorted(_body_bearing_dataclasses().items()):
        if name.startswith(FROZEN_SURFACE_PREFIX):
            continue  # see FROZEN_SURFACE_PREFIX; the deferral has its own test
        repr_fn = cls.__dict__.get("__repr__")
        if repr_fn is None:
            offenders.append(f"{name}: inherits a repr it does not control")
            continue
        defined_in = getattr(getattr(repr_fn, "__code__", None), "co_filename", "")
        if defined_in != inspect.getfile(cls):
            bodies = sorted({f.name for f in dataclasses.fields(cls)}
                            & BODY_FIELD_NAMES)
            offenders.append(f"{name}: generated repr would render {bodies}")
    assert offenders == [], (
        "every dataclass holding a body must render body-free; use "
        "training_gym.schemas.body_free_repr:\n  " + "\n  ".join(offenders))


# ══════════════════════════════════════════════════════════════════════════════
#  The guard itself
# ══════════════════════════════════════════════════════════════════════════════
def test_body_free_repr_refuses_to_render_a_long_string():
    """The allowlist is by TYPE and SIZE, so naming a body field cannot defeat it."""
    @dataclasses.dataclass
    class Careless:
        task_id: str = "t-1"
        user_prompt: str = PROMPT_CANARY * 4

    rendered = body_free_repr(Careless(), "task_id", "user_prompt")
    assert leaked(rendered) == []
    assert "<str len=" in rendered
    assert "task_id='t-1'" in rendered


def test_body_free_repr_refuses_to_render_a_multiline_string():
    """A short body is still a body if it has the shape of prose, not an identifier."""
    @dataclasses.dataclass
    class Careless:
        user_prompt: str = f"line one\n{PROMPT_CANARY}"

    assert leaked(body_free_repr(Careless(), "user_prompt")) == []


def test_body_free_repr_summarises_containers_without_recursing():
    """Recursion into elements is exactly how one prompt became a whole corpus."""
    @dataclasses.dataclass
    class Holder:
        tasks: tuple = (PROMPT_CANARY, PROMPT_CANARY)
        lookup: dict = dataclasses.field(
            default_factory=lambda: {"a": TARGET_CANARY})

    rendered = body_free_repr(Holder(), "tasks", "lookup")
    assert leaked(rendered) == []
    assert "tasks=<tuple len=2>" in rendered
    assert "lookup=<dict keys=1>" in rendered


def test_body_free_repr_keeps_digests_and_enums_readable(task):
    """Safety that destroys debuggability gets removed by the next person in a hurry."""
    rendered = repr(task)
    assert task.task_hash in rendered
    assert "TaskFamily.SAFETY_REFUSAL" in rendered
    assert "DatasetSplit.HIDDEN_EVALUATION" in rendered
    assert "s3x0-synthetic-001" in rendered


def test_body_free_repr_skips_a_field_that_does_not_exist():
    assert body_free_repr(object(), "nope") == "object()"


# ══════════════════════════════════════════════════════════════════════════════
#  This suite touches no real holdout
# ══════════════════════════════════════════════════════════════════════════════
def test_this_suite_names_no_real_evaluation_corpus():
    """S3X.0 is a recovery milestone. It must not become a second exposure."""
    module = inspect.getmodule(test_the_task_repr_carries_no_body)
    # Two functions legitimately name what the rest of the module may not: this one
    # spells the forbidden tokens, and the freeze-expiry check reads a SIBLING TEST
    # file (never a corpus). Both are excluded by source rather than by spelling the
    # tokens in fragments a reader cannot verify.
    exempt = (test_this_suite_names_no_real_evaluation_corpus,
              test_the_frozen_training_surface_is_the_only_reason_for_the_exclusion)
    source = inspect.getsource(module)
    for func in exempt:
        source = source.replace(inspect.getsource(func), "")

    forbidden = (
        "m62-defensive-eval",           # the real holdout dataset id
        "e852f4627d4fe631",             # v5 manifest digest
        "287a9fb61e3feab5",             # v5 pack digest
        "load_dataset", "read_text", "read_bytes", "rglob", "glob(",
    )
    for token in forbidden:
        assert token not in source, (
            f"{token!r} appears in a suite that must build every fixture in memory")
