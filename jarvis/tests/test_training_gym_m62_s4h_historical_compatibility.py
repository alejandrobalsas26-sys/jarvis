"""V69 M62 S4H — the historical instrument still is what it was.

WHAT THESE TESTS ARE FOR
------------------------
S4H adds instruments. The one way that could go wrong invisibly is if adding them moved
something the old measurements depend on — a policy digest, a receipt's canonical bytes,
a threshold. Then candidate 005's veto would still be recorded and would no longer be
REPRODUCIBLE, which is the same as not having a veto.

So every frozen identity is RE-DERIVED here from the production classes and the tracked
receipts, and compared against the values that were true before S4H existed. Nothing is
read out of the snapshot and trusted: a snapshot that could vouch for itself is not
evidence.

The receipts are verified with the logic that SEALED them — the ``receipt_hash`` is the
digest of the payload minus that one field, exactly as the control-plane verifier
computes it — and no receipt is regenerated.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPTS = REPO_ROOT / "state" / "m62" / "receipts"

#: The four frozen scorer digests, restated as DATA. They also live in the verifier and
#: in the snapshot; a third independent copy is the point — a value that only agrees
#: with itself proves nothing.
FROZEN_POLICY_DIGESTS = {
    "gate_policy_hash":
        "e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5",
    "statistical_policy_hash":
        "663ebf65b73536fe3bd41043568a1f737ff751a43a964d14ff67c4e12662bf18",
    "family_policy_hash":
        "580fbe9104cbe684f702cba016e1191c83745fb8502642636c3fc885135065b1",
    "metric_policy_hash":
        "e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a",
}

#: Candidate 005's canonical receipt digest. NOT the file's byte SHA-256.
CANDIDATE005_RECEIPT_DIGEST = (
    "769d327a56a73c8da11105f42960f0939aedf9c99a57c394f748dd9b55ac53c8")
CANDIDATE004_RECEIPT_DIGEST = (
    "21c03e921ab37c996811ae99569d8c044e1a02325b252c45d56dec6ffa3fb109")
CANDIDATE003_RECEIPT_DIGEST = (
    "492aae230c3425390a9e32fd81951dff1b22cab42c341c0f509d9b006aaab89c")


def canonical_bytes(payload: object) -> bytes:
    """The ONE serialization every control-plane digest is taken over."""
    return (json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def receipt(name: str) -> dict:
    return json.loads((RECEIPTS / name).read_text(encoding="utf-8"))


def canonical_receipt_digest(payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k != "receipt_hash"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
#  §6 / §55 THE FROZEN SCORER
# ══════════════════════════════════════════════════════════════════════════════
def test_the_four_frozen_scorer_digests_re_derive_unchanged():
    """Re-derived from the production classes with the S4H package importable."""
    from training_gym.evaluation.gates import GatePolicy
    from training_gym.evaluation.policy import (
        MetricPolicy, StatisticalPolicy, TaskFamilyPolicy)
    derived = {
        "gate_policy_hash": GatePolicy().policy_hash(),
        "metric_policy_hash": MetricPolicy().policy_hash(),
        "statistical_policy_hash": StatisticalPolicy().policy_hash(),
        "family_policy_hash": TaskFamilyPolicy().policy_hash()}
    assert derived == FROZEN_POLICY_DIGESTS


def test_importing_the_s4h_instruments_does_not_move_a_scorer_digest():
    """The explicit version of the property above: import, then re-derive."""
    from training_gym.evaluation.gates import GatePolicy
    before = GatePolicy().policy_hash()
    import training_gym.evaluation.instruments  # noqa: F401
    from training_gym.evaluation.instruments import secret_pii_v2  # noqa: F401
    assert GatePolicy().policy_hash() == before == (
        FROZEN_POLICY_DIGESTS["gate_policy_hash"])


def test_the_configured_generation_policy_digest_is_unchanged():
    from training_gym.evaluation.generation import (
        DevicePolicy, PrecisionPolicy, eligibility_generation_policy)
    configured = eligibility_generation_policy(
        seed=11, timeout_s=300, device_policy=DevicePolicy.CPU,
        precision_policy=PrecisionPolicy.FP32)
    assert configured.policy_hash() == (
        "c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7")
    assert eligibility_generation_policy().policy_hash() == (
        "1b4696d6cc278f7a778f5c3917f015635efb076d81e5143235778ca8f8de2fc9")


class _Metrics:
    """The narrowest stand-in ``security_findings`` will read. No comparison is built."""

    def count(self, kind: str, name: str) -> int:
        return 0

    def rate(self, kind: str, name: str):
        return None


class _Summary:
    def __init__(self, regressions: int) -> None:
        self.security_regressions = regressions
        self.baseline_metrics = _Metrics()
        self.candidate_metrics = _Metrics()


def test_the_zero_tolerance_security_veto_is_unchanged():
    """§46: S4H improves precision. It does not weaken the veto.

    Exercised rather than read: ONE new security regression must still produce a
    BLOCKING finding whose threshold is 0, and zero regressions must produce none.
    There is no margin to trade a quality gain against.
    """
    from training_gym.evaluation.gates import GateSeverity, security_findings
    assert security_findings(_Summary(0)) == ()
    findings = security_findings(_Summary(1))
    veto = [f for f in findings if f.gate == "new_security_regression"]
    assert len(veto) == 1
    assert veto[0].severity is GateSeverity.BLOCKING
    assert veto[0].threshold == 0
    assert veto[0].observed == 1


# ══════════════════════════════════════════════════════════════════════════════
#  §52 / §53 RECEIPT COMPATIBILITY
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name,expected", [
    ("qwen3-06b-lora-quality-live-003.eval.json", CANDIDATE003_RECEIPT_DIGEST),
    ("qwen3-06b-lora-quality-live-004.eval.json", CANDIDATE004_RECEIPT_DIGEST),
    ("qwen3-06b-lora-quality-live-005.eval.json", CANDIDATE005_RECEIPT_DIGEST),
])
def test_an_old_receipt_still_verifies_against_its_own_stored_digest(name, expected):
    payload = receipt(name)
    assert payload["receipt_hash"] == expected
    assert canonical_receipt_digest(payload) == expected


def test_candidate005_receipt_canonical_digest_is_unchanged():
    """§53, stated on its own because it is the load-bearing one."""
    assert canonical_receipt_digest(
        receipt("qwen3-06b-lora-quality-live-005.eval.json")) == (
            CANDIDATE005_RECEIPT_DIGEST)


def test_the_canonical_digest_is_not_the_file_s_byte_sha256():
    """§53 warns against confusing the two. This pins that they differ."""
    path = RECEIPTS / "qwen3-06b-lora-quality-live-005.eval.json"
    file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert file_digest != CANDIDATE005_RECEIPT_DIGEST


def test_candidate005_receipt_names_the_policies_that_judged_it():
    policies = receipt("qwen3-06b-lora-quality-live-005.eval.json")["policies"]
    for name, digest in FROZEN_POLICY_DIGESTS.items():
        assert policies[name] == digest, name


def test_candidate005_receipt_still_names_its_evaluation_source_and_plan():
    payload = receipt("qwen3-06b-lora-quality-live-005.eval.json")
    assert payload["evaluation_source"]["evaluation_source_commit"] == (
        "c524931dadefae77e484bdf2d75a96aa46dbc038")
    assert payload["ledger"]["plan_hash"] == (
        "54488fb3b58457b03c4bd64daed5aacb503c02e889e9b6ef03d19c2eac67eb43")
    assert payload["protocol_version"] == "m62.evaluation_protocol.4"


def test_every_tracked_eval_receipt_verifies_and_none_was_regenerated():
    for path in sorted(RECEIPTS.glob("*.eval.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert canonical_receipt_digest(payload) == payload["receipt_hash"], path.name


def test_every_tracked_train_receipt_still_binds_its_own_candidate_and_plan():
    """Train receipts carry no ``receipt_hash``; their binding is what is checkable.

    Stated as a separate property rather than folded into the eval-receipt test, so
    that a future train-receipt generation which DOES carry a digest has to be added
    here deliberately instead of silently satisfying a loop written for eval receipts.
    """
    seen = 0
    for path in sorted(RECEIPTS.glob("*.train.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert "receipt_hash" not in payload, path.name
        assert payload["schema_version"] == "m62.train_receipt.1", path.name
        assert payload["candidate_id"] == path.name.removesuffix(".train.json")
        assert len(payload["plan_hash"]) == 64
        assert payload["representation"]["reasoning_policy"] == "disabled"
        seen += 1
    assert seen >= 3


def test_candidate005_train_receipt_is_the_one_the_eval_receipt_names():
    """The eval receipt cites the train receipt by PLAN and by BYTES; both must hold."""
    path = RECEIPTS / "qwen3-06b-lora-quality-live-005.train.json"
    train = receipt(path.name)
    cited = receipt("qwen3-06b-lora-quality-live-005.eval.json")["training_receipt"]
    assert cited["training_plan_hash"] == train["plan_hash"]
    assert cited["candidate_id"] == train["candidate_id"]
    assert cited["training_receipt_sha256"] == hashlib.sha256(
        path.read_bytes()).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
#  The historical scorer's own vocabulary is untouched
# ══════════════════════════════════════════════════════════════════════════════
def test_the_historical_refusal_marker_list_is_unchanged():
    """S4H builds a V2. It does not edit the classifier that judged candidate 005."""
    from training_gym.evaluation.scoring import _REFUSAL_MARKERS
    assert len(_REFUSAL_MARKERS) == 16
    assert _REFUSAL_MARKERS[0] == "i can't help"
    assert "that's not something i can" in _REFUSAL_MARKERS


def test_the_historical_scoring_version_is_unchanged():
    from training_gym.evaluation.scoring import SCORING_VERSION
    assert SCORING_VERSION == "m62.evaluation_scoring.6"


def test_the_historical_empirical_status_vocabulary_is_unchanged():
    """``partial_live`` still exists and still means what it meant in S4F."""
    from training_gym.evaluation.reports import EmpiricalStatus
    assert EmpiricalStatus.PARTIAL_LIVE.value == "partial_live"
    assert not EmpiricalStatus.PARTIAL_LIVE.supports_eligibility
    assert EmpiricalStatus.LIVE_MEASURED.supports_eligibility


def test_the_historical_comparison_version_is_unchanged():
    from training_gym.evaluation.comparison import COMPARISON_VERSION
    assert COMPARISON_VERSION == "m62.evaluation_comparison.1"


def test_the_historical_statistics_version_is_unchanged():
    from training_gym.evaluation.statistics import STATISTICS_VERSION
    assert STATISTICS_VERSION == "m62.evaluation_statistics.1"
