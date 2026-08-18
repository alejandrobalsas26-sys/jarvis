"""training_gym/evaluation/preflight.py — V69 M62 S3Q.0: exact plan binding, body-opaque.

THE DEFECT THIS CLOSES
----------------------
``scripts/evaluate_adapter.py::_plan()`` bound three fields whose names claim an exact
runtime identity and whose values were digests of something else:

    task_pack_hash            sha256({dataset manifest, split counts})
    hidden_target_store_hash  sha256(dataset manifest hash)
    order_assignment_hash     sha256("<policy>:<seed>")

while execution derived ``BuiltPack.pack.pack_hash()``,
``BuiltPack.targets.store_hash()`` and
``runner.order_assignment_hash(pack, seed=seed)`` from the material itself. An operator
approving ``EVAL:<plan-hash>`` was therefore approving a manifest digest and a seed, not
the pack that would be handed to the model. Two different packs built from one manifest —
a builder change, a grader-registry change, a schema change — produced the same
confirmation.

This module is the ONE place a pack identity is derived, and both planning and execution
consume it. There is no second formula to drift from the first.

BODY-OPAQUE, NOT BODY-BLIND
---------------------------
Deriving the exact identity requires building the pack, and building the pack requires
reading held-out prompts and targets off disk. That is
``BODY_OPAQUE_PROGRAMMATIC_ACCESS``: reviewed code reads the bytes, hashes them, and
returns digests and counts. No prompt, no target, no record and no schema prose leaves
this module — :class:`PackIdentity` has no field that could hold one, which is the same
structural argument :class:`~training_gym.evaluation.backend.EvaluationRequest` makes
about hidden targets.

It is emphatically NOT ``ORCHESTRATOR_SEMANTIC_ACCESS``. Nothing here prints, logs,
returns or persists a body, and :func:`assert_body_free` is asserted over every payload
this module hands out.

WHY THE PACK IS NOT CACHED ACROSS THE CONFIRMATION BOUNDARY
-----------------------------------------------------------
Holding a built pack — and therefore a live :class:`HiddenTargetStore` — in module state
between planning and execution would put held-out answers in mutable process memory
across a human decision point, and would let a plan approved against one on-disk state
execute against a pack materialised before it. So the pack is built, hashed and dropped.
Execution rebuilds it deterministically and :func:`plan_binding_mismatches` re-checks the
rebuild against the confirmed plan BEFORE the holdout is committed to the model.

TOKEN SILENCE IS HYGIENE, NOT CRYPTOGRAPHY
------------------------------------------
``EVAL:<plan-hash>`` is deterministically derivable from ``plan_hash``, which
:func:`preflight_report` publishes. Withholding the literal does not make it secret,
unpredictable or authenticating, and it prevents no theft. What it does is separate
PRE-GO from GO: a preflight an operator runs to decide cannot leave the exact string that
authorises the decision in a shell history, a ticket or a document.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..schemas import SchemaError, sha256_obj
from .pack_builder import BuiltPack, build_task_pack_from_dataset
from .plan import CONFIRMATION_PREFIX
from .runner import ORDER_POLICY_BALANCED, order_assignment_hash, order_balance

#: Bumped when the shape of a derived pack identity changes. Distinct from every
#: measurement policy identity: this versions the BINDING, not the model's behaviour.
EVALUATION_PREFLIGHT_VERSION = "m62.evaluation_preflight.1"

#: Anything shaped like a spendable evaluation confirmation. Deliberately requires the
#: full 64-character digest, so the bare prefix in a docstring, a schema constant or an
#: error message is not mistaken for a materialised authority.
CONFIRMATION_LITERAL_RE = re.compile(
    re.escape(CONFIRMATION_PREFIX) + r"[0-9a-fA-F]{64}")

#: Fields on :class:`PackIdentity` that an approved plan binds exactly. A mismatch in any
#: one of them means the confirmed plan and the pack about to be measured are not the
#: same object, and execution stops before the model sees a held-out task.
EXACT_BOUND_FIELDS: tuple[str, ...] = (
    "dataset_id", "dataset_version", "dataset_manifest_hash", "pack_hash",
    "hidden_target_store_hash", "order_policy", "order_assignment_hash", "task_count")


class PreflightError(SchemaError):
    """A pack identity that could not be derived, or one that does not match the plan."""


# ══════════════════════════════════════════════════════════════════════════════
#  The identity
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class PackIdentity:
    """Everything a plan binds about the held-out material, and nothing it says.

    The field list IS the body firewall. There is no ``tasks``, ``prompts``,
    ``targets``, ``records`` or ``schemas`` field, so a caller that obtains one of these
    has obtained digests and counts — not material — however carelessly it is logged.
    """

    dataset_id: str
    dataset_version: str
    dataset_manifest_hash: str
    pack_hash: str
    hidden_target_store_hash: str
    order_policy: str
    order_assignment_hash: str
    task_count: int
    target_count: int
    counts_by_split: dict
    counts_by_family: dict
    counts_by_kind: dict
    shard_hashes: dict
    order_balance: tuple[int, int]
    pack_manifest_hash: str
    builder_version: str
    seed: int
    eligibility_blockers: tuple[str, ...] = ()
    preflight_version: str = EVALUATION_PREFLIGHT_VERSION

    def to_dict(self) -> dict:
        """The canonical body-free description. Asserted body-free before it returns."""
        payload = {
            "preflight_version": self.preflight_version,
            "builder_version": self.builder_version,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_manifest_hash": self.dataset_manifest_hash,
            "pack_hash": self.pack_hash,
            "hidden_target_store_hash": self.hidden_target_store_hash,
            "pack_manifest_hash": self.pack_manifest_hash,
            "order_policy": self.order_policy,
            "order_assignment_hash": self.order_assignment_hash,
            "order_balance_baseline_first": self.order_balance[0],
            "order_balance_candidate_first": self.order_balance[1],
            "seed": int(self.seed),
            "task_count": int(self.task_count),
            "target_count": int(self.target_count),
            "counts_by_split": dict(sorted(self.counts_by_split.items())),
            "counts_by_family": dict(sorted(self.counts_by_family.items())),
            "counts_by_kind": dict(sorted(self.counts_by_kind.items())),
            "shard_hashes": dict(sorted(self.shard_hashes.items())),
            "eligibility_blockers": list(self.eligibility_blockers),
        }
        assert_body_free(payload, label="pack identity")
        return payload

    def identity_hash(self) -> str:
        return sha256_obj(self.to_dict())


def derive_pack_identity(built: BuiltPack, *, seed: int) -> PackIdentity:
    """The body-free identity of a pack that is already in memory.

    The canonical hashes are asked of the objects that own them —
    ``pack.pack_hash()``, ``targets.store_hash()`` and the runner's own
    ``order_assignment_hash`` — rather than recomputed here. A second implementation of
    any of the three would be a second opinion about what was measured.
    """
    if not isinstance(built, BuiltPack):
        raise PreflightError("preflight: expected a BuiltPack")
    pack = built.pack
    return PackIdentity(
        dataset_id=built.dataset_id,
        dataset_version=built.dataset_version,
        dataset_manifest_hash=built.dataset_manifest_hash,
        pack_hash=pack.pack_hash(),
        hidden_target_store_hash=built.targets.store_hash(),
        order_policy=ORDER_POLICY_BALANCED,
        order_assignment_hash=order_assignment_hash(pack, seed=seed),
        task_count=len(pack),
        target_count=len(built.targets),
        counts_by_split=dict(pack.counts_by_split()),
        counts_by_family=dict(pack.counts_by_family()),
        counts_by_kind=dict(built.counts_by_kind()),
        shard_hashes=dict(built.shard_hashes),
        order_balance=order_balance(pack, seed=seed),
        pack_manifest_hash=built.manifest_hash(),
        builder_version=built.builder_version,
        seed=int(seed),
        eligibility_blockers=tuple(pack.eligibility_blockers()))


def prepare_pack_identity(*, root: str | Path, dataset_id: str, dataset_version: str,
                          splits: Sequence[object], generation: int,
                          seed: int) -> PackIdentity:
    """Build the pack body-opaquely, take its identity, and drop it.

    The built pack and its hidden-target store are local to this call and are not
    returned, stored, cached or logged. What survives is a :class:`PackIdentity`, which
    has nowhere to put a body.
    """
    built = build_task_pack_from_dataset(
        root=root, dataset_id=dataset_id, dataset_version=dataset_version,
        splits=splits, generation=generation)
    return derive_pack_identity(built, seed=seed)


# ══════════════════════════════════════════════════════════════════════════════
#  Binding checks — the TOCTOU seam between approval and measurement
# ══════════════════════════════════════════════════════════════════════════════
def _compare(label: str, approved: object, actual: object) -> str:
    return (f"{label}: the confirmed plan bound {approved!r}, the material about to be "
            f"measured is {actual!r}")


def plan_binding_mismatches(plan: object, identity: PackIdentity) -> tuple[str, ...]:
    """Every exact identity the approved plan bound that this pack does not reproduce.

    Empty means the pack an operator approved is the pack that is about to be measured.
    Anything else means the world moved between approval and execution, and the run must
    stop while the holdout is still unspent.
    """
    problems: list[str] = []
    for label, approved, actual in (
            ("task_pack_hash", getattr(plan, "task_pack_hash", None),
             identity.pack_hash),
            ("hidden_target_store_hash",
             getattr(plan, "hidden_target_store_hash", None),
             identity.hidden_target_store_hash),
            ("order_policy", getattr(plan, "order_policy", None),
             identity.order_policy),
            ("order_assignment_hash", getattr(plan, "order_assignment_hash", None),
             identity.order_assignment_hash),
            ("dataset_manifest_hash", getattr(plan, "dataset_manifest_hash", None),
             identity.dataset_manifest_hash),
            ("expected_task_count", getattr(plan, "expected_task_count", None),
             identity.task_count)):
        if approved != actual:
            problems.append(_compare(label, approved, actual))
    return tuple(problems)


def execution_binding_mismatches(*, plan: object, identity: PackIdentity,
                                 baseline: object, adapter: object,
                                 generation_policy: object) -> tuple[str, ...]:
    """The full re-verification a live run owes itself before it spends the holdout.

    The pack, the store and the order assignment are re-derived from disk; the two
    references and the generation policy are re-asked for their digests. A plan approved
    against one adapter, one baseline or one decoding policy may not execute against
    another, and "it is the same one" is checked rather than assumed.
    """
    problems = list(plan_binding_mismatches(plan, identity))
    for label, approved, actual in (
            ("baseline_reference_hash", getattr(plan, "baseline_reference_hash", None),
             baseline.reference_hash()),
            ("candidate_adapter_reference_hash",
             getattr(plan, "candidate_adapter_reference_hash", None),
             adapter.reference_hash()),
            ("tokenizer_identity_hash", getattr(plan, "tokenizer_identity_hash", None),
             baseline.tokenizer_identity_hash),
            ("generation_policy_hash", getattr(plan, "generation_policy_hash", None),
             generation_policy.policy_hash())):
        if approved != actual:
            problems.append(_compare(label, approved, actual))
    return tuple(problems)


# ══════════════════════════════════════════════════════════════════════════════
#  Body-free and token-silent assertions
# ══════════════════════════════════════════════════════════════════════════════
#: Keys a body-free payload may never carry, whatever their value. Checked by name as
#: well as by content, because a field called ``user_prompt`` holding an empty string
#: today is a field holding a prompt after one refactor.
FORBIDDEN_BODY_KEYS: frozenset[str] = frozenset({
    "user_prompt", "system_prompt", "prompt", "prompts", "target_text", "target",
    "targets", "expected_answer", "answer", "response_text", "response", "responses",
    "rubric", "rubric_text", "record", "records", "task_records", "tasks",
    "expected_output_schema", "tool_schemas", "body", "bodies", "text",
})


def _walk(payload: object, path: str = "$"):
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield path, str(key), value
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            yield from _walk(value, f"{path}[{index}]")


def body_free_problems(payload: object, *, label: str = "payload") -> tuple[str, ...]:
    """Why this payload is not safe to print, persist or hand to an orchestrator."""
    problems: list[str] = []
    for path, key, _value in _walk(payload):
        if key.lower() in FORBIDDEN_BODY_KEYS:
            problems.append(
                f"{label}: {path}.{key} is a body-bearing field name; a body-free "
                f"surface may carry a digest of the material and never the material")
    return tuple(dict.fromkeys(problems))


def assert_body_free(payload: object, *, label: str = "payload") -> None:
    problems = body_free_problems(payload, label=label)
    if problems:
        raise PreflightError("; ".join(problems))


def confirmation_literals(payload: object) -> tuple[str, ...]:
    """Every materialised ``EVAL:<64-hex>`` reachable in this payload."""
    found: list[str] = []
    for _path, _key, value in _walk({"root": payload}):
        if isinstance(value, str):
            found.extend(CONFIRMATION_LITERAL_RE.findall(value))
    if isinstance(payload, str):
        found.extend(CONFIRMATION_LITERAL_RE.findall(payload))
    return tuple(dict.fromkeys(found))


def assert_token_silent(payload: object, *, label: str = "payload") -> None:
    """Refuse to emit a payload that has materialised a spendable confirmation.

    This is ceremony hygiene, not a secrecy claim: the string is derivable by anyone who
    holds ``plan_hash``, which this same payload publishes. What it buys is that a
    PRE-GO surface cannot deposit the GO string into a terminal, a log or a ticket.
    """
    found = confirmation_literals(payload)
    if found:
        raise PreflightError(
            f"{label}: a confirmation literal was materialised into a surface that must "
            f"stay token-silent. The digest is derivable from plan_hash by design; what "
            f"this refusal protects is the separation between deciding and authorising")


# ══════════════════════════════════════════════════════════════════════════════
#  The token-silent live preflight report
# ══════════════════════════════════════════════════════════════════════════════
def preflight_report(*, plan: object, identity: PackIdentity | None,
                     dependency_ready: bool, live_blockers: Sequence[str] = (),
                     generation_policy: object | None = None) -> dict:
    """Everything an operator needs to DECIDE, and nothing that authorises.

    Carries ``plan_hash`` — the identity of the thing being decided about — and
    deliberately omits ``confirmation_required``, ``confirmation_token`` and the
    ``EVAL:`` literal. It is the one surface a future PRE-GO ceremony may use.
    """
    payload = {
        "status": "preflight",
        "preflight_version": EVALUATION_PREFLIGHT_VERSION,
        "token_silent": True,
        "evaluation_id": getattr(plan, "evaluation_id", ""),
        "generation": getattr(plan, "generation", 0),
        "plan_hash": plan.plan_hash(),
        "plan_schema_version": getattr(plan, "plan_schema_version", ""),
        "evaluator_version": getattr(plan, "evaluator_version", ""),
        "is_executable": getattr(plan, "is_executable", False),
        "blockers": list(getattr(plan, "blockers", ())),
        "warnings": list(getattr(plan, "warnings", ())),
        "live_execution_blockers": list(live_blockers),
        "dependencies_ready": bool(dependency_ready),
        "backend_id": getattr(plan, "backend_id", ""),
        "baseline_reference_hash": getattr(plan, "baseline_reference_hash", ""),
        "candidate_adapter_reference_hash":
            getattr(plan, "candidate_adapter_reference_hash", ""),
        "tokenizer_identity_hash": getattr(plan, "tokenizer_identity_hash", ""),
        "evaluation_config_hash": getattr(plan, "evaluation_config_hash", ""),
        "dataset_id": identity.dataset_id if identity else "",
        "dataset_version": identity.dataset_version if identity else "",
        "dataset_manifest_hash": getattr(plan, "dataset_manifest_hash", ""),
        "task_pack_hash": getattr(plan, "task_pack_hash", ""),
        "hidden_target_store_hash": getattr(plan, "hidden_target_store_hash", ""),
        "order_policy": getattr(plan, "order_policy", ""),
        "order_assignment_hash": getattr(plan, "order_assignment_hash", ""),
        "task_count": getattr(plan, "expected_task_count", 0),
        "counts_by_split": dict(identity.counts_by_split) if identity else {},
        "counts_by_family": dict(identity.counts_by_family) if identity else {},
        "counts_by_kind": dict(identity.counts_by_kind) if identity else {},
        "pack_identity_hash": identity.identity_hash() if identity else "",
        "generation_policy_hash": getattr(plan, "generation_policy_hash", ""),
        "grader_policy_hash": getattr(plan, "grader_policy_hash", ""),
        "metric_policy_hash": getattr(plan, "metric_policy_hash", ""),
        "statistical_policy_hash": getattr(plan, "statistical_policy_hash", ""),
        "gate_policy_hash": getattr(plan, "gate_policy_hash", ""),
        "family_policy_hash": getattr(plan, "family_policy_hash", ""),
        "resource_policy_hash": getattr(plan, "resource_policy_hash", ""),
        "dependency_report_hash": getattr(plan, "dependency_report_hash", ""),
        "hardware_report_hash": getattr(plan, "hardware_report_hash", ""),
        "performs_inference": bool(getattr(plan, "performs_inference", False)),
        "expected_effects": plan.expected_effects(),
        "authority_form": f"{CONFIRMATION_PREFIX}<plan-hash>",
        "note": ("PRE-GO only. This surface reports what a live evaluation WOULD do and "
                 "deliberately does not materialise the confirmation string that would "
                 "authorise it. Nothing was executed, no model was loaded, no held-out "
                 "task body was read into this report and no plan was consumed"),
    }
    if generation_policy is not None:
        payload["reasoning_policy"] = getattr(
            getattr(generation_policy, "reasoning_policy", ""), "value", "")
        payload["max_new_tokens"] = getattr(generation_policy, "max_new_tokens", 0)
        payload["configured_timeout_s"] = getattr(generation_policy, "timeout_s", 0)
        # D33 stays OPEN and is restated wherever the timeout is reported, so nobody
        # reads a configured ceiling as an enforced one.
        payload["timeout_enforced"] = False
    assert_body_free(payload, label="live preflight")
    assert_token_silent(payload, label="live preflight")
    return payload


__all__ = [
    "CONFIRMATION_LITERAL_RE", "EVALUATION_PREFLIGHT_VERSION", "EXACT_BOUND_FIELDS",
    "FORBIDDEN_BODY_KEYS", "PackIdentity", "PreflightError", "assert_body_free",
    "assert_token_silent", "body_free_problems", "confirmation_literals",
    "derive_pack_identity", "execution_binding_mismatches", "plan_binding_mismatches",
    "prepare_pack_identity", "preflight_report",
]
