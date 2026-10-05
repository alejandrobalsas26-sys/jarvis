"""V69 M67A.1 — no schema may claim authority over hidden model reasoning.

THE RESIDUAL THIS CLOSES
------------------------
§7 is an epistemic rule: historical visible rationale from Claude is SOURCE MATERIAL, not
privileged hidden reasoning, and no field may be named ``chain_of_thought``. At M67A the
rule held *by construction* — the name appeared nowhere — but nothing pinned it. The
forbidden-token scan in ``test_reasoning_absent_controls_m67a`` covers ``candidate006``,
``eval-v8``, ``TRAIN:``, ``EVAL:`` and ``PROMOTE:`` and does NOT cover this name, so a
later generation could have introduced the field and no test would have objected.

M67A.1 is exactly when that gap stops being theoretical. The supplied corpus is a Word
document of visible "thinking" prose — 44 pages of first-person assistant narration with no
user turns and no answers. That is the single most tempting material in the project to
label ``chain_of_thought``, because superficially it looks like one. It is not: it is a
visible, exported, operator-supplied summary, and treating it as privileged hidden
reasoning would (a) claim access to proprietary model internals this project does not have,
and (b) convert prose of unknown provenance into ground truth by the act of naming it.

WHY A NAME AND NOT A BEHAVIOUR
------------------------------
The hazard here really is nominal. A field called ``assistant_reasoning`` carrying visible
exported text is correct and already exists. The identical bytes under the name
``chain_of_thought`` assert something false about where they came from. So the control is a
closed denylist of names that assert hidden-reasoning authority, matched EXACTLY on the
normalised identifier — never as a substring, or every legitimate ``reasoning_relevance``
and ``assistant_reasoning`` in the package would trip it.

Every detector below has a non-vacuity witness: a synthetic violation it must FIRE on. A
structural test that cannot fail is not a control, and this file would otherwise be the
easiest place in the repository to write one by accident.
"""
from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import json
import pkgutil
from enum import Enum
from pathlib import Path

import pytest

import reasoning_distillation
from reasoning_distillation import adapters
from reasoning_distillation.models import TurnRole

#: Names that assert authority over hidden/privileged model reasoning. Matched exactly on
#: the normalised identifier. Each entry is a claim this project cannot support.
FORBIDDEN_NAMES: frozenset[str] = frozenset({
    # the name §7 forbids outright, and its obvious spellings
    "chainofthought", "chainofthoughts", "chainsofthought", "cot",
    # "there is a hidden version and this is it"
    "hiddenreasoning", "hiddenthoughts", "hiddenchain", "hiddenrationale",
    "hiddenmonologue", "hiddenstate",
    # "this is the model's interior, not its output"
    "internalreasoning", "internalmonologue", "internalthoughts",
    "innermonologue", "innerthoughts", "innerreasoning",
    # "this one is the privileged/true one"
    "privatereasoning", "privilegedreasoning", "secretreasoning",
    "truereasoning", "actualreasoning", "realreasoning",
    "authoritativereasoning", "groundtruthreasoning", "verifiedreasoning",
    "rawthoughts", "latentreasoning",
})


def normalise(identifier: str) -> str:
    """An identifier reduced to its comparison key: lowercase alphanumerics only.

    So ``chain_of_thought``, ``chainOfThought``, ``CHAIN_OF_THOUGHT`` and
    ``chain-of-thought`` all collapse to one key, and a rename cannot evade the check by
    changing only the separators.
    """
    return "".join(ch for ch in identifier.lower() if ch.isalnum())


def is_forbidden(identifier: str) -> bool:
    return normalise(identifier) in FORBIDDEN_NAMES


def package_sources() -> "list[tuple[str, str]]":
    """Every ``.py`` in the package, as ``(relative path, source)``."""
    root = Path(inspect.getfile(reasoning_distillation)).parent
    out: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        out.append((str(path.relative_to(root)), path.read_text(encoding="utf-8")))
    assert out, "the scan found no package sources, so it is not checking anything"
    return out


def declared_names(source: str) -> "set[str]":
    """Every name this module DECLARES or uses as a mapping key.

    Deliberately broad: annotated and plain assignment targets, dataclass fields, class and
    function names, parameters, keyword arguments, attribute names, and string literals
    used as dict keys or in sets/lists of field names. A hidden-reasoning field could be
    introduced as any of these, and a scan that only read dataclass fields would miss a
    plain ``record["chain_of_thought"] = ...``.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Attribute):
                    names.add(target.attr)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A string constant is only a candidate when it could BE an identifier.
            if node.value and node.value.replace("_", "").replace("-", "").isalnum():
                names.add(node.value)
    return names


# ── the static control ──────────────────────────────────────────────────────────────

def test_no_package_source_declares_a_hidden_reasoning_authority_name():
    """§7: the field must not exist, under any of its spellings, anywhere."""
    offenders: list[str] = []
    for relpath, source in package_sources():
        for name in sorted(declared_names(source)):
            if is_forbidden(name):
                offenders.append(f"{relpath}: {name}")
    assert not offenders, (
        "a hidden-reasoning authority name is declared in the corpus package; visible "
        f"exported rationale must not be named as privileged model internals: {offenders}")


def test_the_scanner_fires_on_a_synthetic_violation():
    """NON-VACUITY for the static control. Without this the scan may check nothing."""
    violation = (
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class Bad:\n"
        "    chain_of_thought: str = ''\n")
    found = {n for n in declared_names(violation) if is_forbidden(n)}
    assert found == {"chain_of_thought"}


@pytest.mark.parametrize("spelling", [
    "chain_of_thought", "chainOfThought", "CHAIN_OF_THOUGHT", "cot",
    "hidden_reasoning", "internal_monologue", "true_reasoning",
    "ground_truth_reasoning", "raw_thoughts",
])
def test_every_denied_spelling_is_actually_detected(spelling):
    """NON-VACUITY per entry: a denylist with a typo silently permits what it names."""
    assert is_forbidden(spelling), f"{spelling!r} must be detected"


@pytest.mark.parametrize("legitimate", [
    "assistant_reasoning", "reasoning_relevance", "reasoning_distillation",
    "ASSISTANT_REASONING", "visible_rationale", "rationale", "reasoning_text",
    "thought", "thinking_tag",
])
def test_legitimate_reasoning_names_are_not_swept_up(legitimate):
    """The control must be exact. A substring match would break the real schema."""
    assert not is_forbidden(legitimate), (
        f"{legitimate!r} names VISIBLE rationale and is legitimate; a control that "
        f"rejected it would force the schema to stop describing what it holds")


# ── the runtime control: the live schema, not just its source text ──────────────────

def test_no_dataclass_field_in_the_package_is_a_hidden_reasoning_name():
    """Source text and live schema can diverge; a field could be added dynamically."""
    offenders: list[str] = []
    checked = 0
    package_path = Path(inspect.getfile(reasoning_distillation)).parent
    modules = [reasoning_distillation]
    for info in pkgutil.walk_packages([str(package_path)],
                                      prefix="reasoning_distillation."):
        try:
            modules.append(importlib.import_module(info.name))
        except Exception:  # pragma: no cover - an unimportable module is another test's job
            continue
    for module in modules:
        for _, obj in inspect.getmembers(module, dataclasses.is_dataclass):
            if not isinstance(obj, type):
                continue
            for field in dataclasses.fields(obj):
                checked += 1
                if is_forbidden(field.name):
                    offenders.append(f"{obj.__module__}.{obj.__name__}.{field.name}")
    assert checked > 50, f"only {checked} dataclass fields scanned; the sweep is too narrow"
    assert not offenders, offenders


def test_no_enum_member_or_value_in_the_package_claims_hidden_reasoning():
    offenders: list[str] = []
    checked = 0
    package_path = Path(inspect.getfile(reasoning_distillation)).parent
    for info in pkgutil.walk_packages([str(package_path)],
                                      prefix="reasoning_distillation."):
        try:
            module = importlib.import_module(info.name)
        except Exception:  # pragma: no cover
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, Enum) or obj is Enum:
                continue
            for member in obj:
                checked += 1
                if is_forbidden(member.name) or (
                        isinstance(member.value, str) and is_forbidden(member.value)):
                    offenders.append(f"{obj.__name__}.{member.name}")
    assert checked > 20, f"only {checked} enum members scanned; the sweep is too narrow"
    assert not offenders, offenders


def test_the_turn_role_vocabulary_names_visibility_not_interiority():
    """The role that holds rationale must say it is the assistant's, not that it is hidden."""
    assert TurnRole.ASSISTANT_REASONING.value == "assistant_reasoning"
    assert not any(is_forbidden(r.name) or is_forbidden(r.value) for r in TurnRole)


def test_the_published_input_contract_offers_no_hidden_reasoning_field():
    """An operator converting their exports must not be invited to supply one."""
    contract = json.dumps(adapters.supported())
    for name in sorted(FORBIDDEN_NAMES):
        assert name not in normalise(contract), name


def test_the_json_contract_turn_keys_stay_closed():
    """Defence in depth: even if a name slipped in, an unknown turn key is refused."""
    description = adapters.contract_description()
    assert set(description["turn_keys"]) == {"role", "text", "extra"}
    assert not any(is_forbidden(k) for k in description["turn_keys"])


def test_the_prohibition_is_stated_where_a_schema_author_will_read_it():
    """A control nobody is told about gets re-broken by the next generation."""
    models = (Path(inspect.getfile(reasoning_distillation)).parent / "models.py")
    text = models.read_text(encoding="utf-8")
    assert "chain_of_thought" not in text, (
        "models.py must not contain the literal name even in prose: the forbidden-name "
        "scanner reads string constants, and a doc mention would be indistinguishable "
        "from a field for any future automated check")
    assert "ASSISTANT_REASONING" in text and "ASSISTANT_ANSWER" in text
