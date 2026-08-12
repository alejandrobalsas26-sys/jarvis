"""V69 M62 S3G — D30: an unverified model cache must block a plan, not decorate it.

THE DEFECT
----------
``plan_training`` derives ``plan.blockers`` from ``feasibility.missing_evidence``, which
is a snapshot of the local ``missing`` list taken when the feasibility report is built.
The cache check appended to ``missing`` AFTER that construction, so its finding was
written into a list nothing read again.

The consequence is specific and expensive. ``download_required`` treats ``UNKNOWN`` as
"a download might be needed", and the download policy is ``deny``, so the planner's own
``download_note`` said *"a future execution would refuse rather than fetch them"* — while
the plan returned ``is_executable: True`` with an empty blocker list. A plan in that
state issues a spendable single-use ``TRAIN:<hash>`` token for a run that cannot load its
model, which is the same wasted-token failure mode D22 already cost this milestone once.

These tests pin both directions: unverified cache blocks, present cache does not. The
second one matters as much as the first — a fix that blocked unconditionally would make
every plan unexecutable and would pass a one-sided test.
"""
from __future__ import annotations

import pytest

from training_gym.training.config import ModelDownloadPolicy
from training_gym.training.model_identity import cache_directory_name
from training_gym.training.planner import plan_training

pytest.importorskip("scripts.build_quality_training_config")
from scripts import build_quality_training_config as QCFG  # noqa: E402
from scripts import build_training_corpus as QC  # noqa: E402


@pytest.fixture(scope="module")
def corpus_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("m62-s3g-plan-corpus")
    QC.build(root)
    return root


def _config(corpus_root, output_root, **overrides):
    config = QCFG.build_config(QCFG.RECOMMENDED_OPTION, dataset_root=corpus_root,
                               output_root=output_root)
    if overrides:
        from dataclasses import replace
        config = replace(config, **overrides)
    return config


def _plan(config, corpus_root, output_root, cache_root):
    return plan_training(config, dataset_root=corpus_root, output_root=output_root,
                         model_cache_root=cache_root, export_root=corpus_root)


CACHE_BLOCKER = "model weights are not known to be cached and the download policy is deny"


def test_an_unnamed_cache_blocks_the_plan(corpus_root, tmp_path):
    """No cache root supplied. UNKNOWN is not a pass."""
    result = _plan(_config(corpus_root, tmp_path / "runs"), corpus_root,
                   tmp_path / "runs", None)
    assert result.identity.cache_status.value == "unknown"
    assert CACHE_BLOCKER in result.plan.blockers
    assert result.plan.is_executable is False


def test_a_cache_root_that_does_not_hold_the_model_blocks_the_plan(corpus_root,
                                                                   tmp_path):
    empty = tmp_path / "empty-cache"
    empty.mkdir()
    result = _plan(_config(corpus_root, tmp_path / "runs"), corpus_root,
                   tmp_path / "runs", empty)
    assert result.identity.cache_status.value == "absent"
    assert CACHE_BLOCKER in result.plan.blockers
    assert result.plan.is_executable is False


def test_a_cache_holding_the_model_does_not_carry_the_blocker(corpus_root, tmp_path):
    """The other direction. A fix that blocked unconditionally would be worse."""
    cache = tmp_path / "reviewed-cache"
    entry = cache / cache_directory_name(QCFG.BASE_MODEL_ID)
    entry.mkdir(parents=True)
    (entry / "refs").write_text("present", encoding="utf-8")

    result = _plan(_config(corpus_root, tmp_path / "runs"), corpus_root,
                   tmp_path / "runs", cache)
    assert result.identity.cache_status.value == "present"
    assert CACHE_BLOCKER not in result.plan.blockers


def test_the_planner_note_and_the_plan_blockers_agree(corpus_root, tmp_path):
    """The defect was precisely that these two disagreed."""
    for cache_root, expect_blocked in ((None, True), (tmp_path / "cache", False)):
        if cache_root is not None:
            entry = cache_root / cache_directory_name(QCFG.BASE_MODEL_ID)
            entry.mkdir(parents=True, exist_ok=True)
            (entry / "refs").write_text("present", encoding="utf-8")
        output_root = tmp_path / f"runs-{expect_blocked}"
        result = _plan(_config(corpus_root, output_root), corpus_root, output_root,
                       cache_root)
        note_refuses = "would refuse rather than fetch" in result.download_note
        assert note_refuses is expect_blocked
        assert (CACHE_BLOCKER in result.plan.blockers) is expect_blocked


def test_an_authorised_download_policy_does_not_get_the_deny_blocker(corpus_root,
                                                                     tmp_path):
    """The blocker is about the DENY policy specifically, not about caching alone."""
    config = _config(corpus_root, tmp_path / "runs",
                     model_download_policy=ModelDownloadPolicy.ALLOW_WITH_EXPLICIT_FLAG)
    result = _plan(config, corpus_root, tmp_path / "runs", None)
    assert result.identity.cache_status.value == "unknown"
    assert CACHE_BLOCKER not in result.plan.blockers


def test_the_recommended_candidate_keeps_its_declared_identity(corpus_root, tmp_path):
    """Guards the run identity against a future edit that reuses a smoke name."""
    config = _config(corpus_root, tmp_path / "runs")
    assert config.run_id == "qwen3-06b-lora-quality-live-001"
    assert "smoke" not in config.run_id
    assert config.base_model_revision == QCFG.BASE_MODEL_REVISION
    assert len(config.base_model_revision) == 40
    assert config.checkpoint_strategy.value == "no"
    assert config.precision_policy.value == "fp32"
    assert config.device_policy.value == "cpu"
    assert config.model_download_policy is ModelDownloadPolicy.DENY
    assert config.trust_remote_code is False
    assert config.dataset_reference.dataset_id == QC.DATASET_ID
    assert config.dataset_reference.is_fully_specified


def test_planning_is_still_a_dry_run(corpus_root, tmp_path):
    """The fix moved code; it must not have introduced an effect."""
    output_root = tmp_path / "runs"
    before = sorted(p.name for p in tmp_path.iterdir())
    _plan(_config(corpus_root, output_root), corpus_root, output_root, None)
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not output_root.exists()
