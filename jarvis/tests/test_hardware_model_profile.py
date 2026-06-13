"""
tests/test_hardware_model_profile.py — Phase 4 tests.

Pure classification + dataclass behavior. No GPU required — the tier classifier
is tested directly, and detect_model_profile() is exercised for not-raising and
returning a valid enum on whatever host runs CI.
"""
from __future__ import annotations

import pytest

from core.hardware_model_profile import (
    HardwareModelProfile,
    HardwareTier,
    _classify_tier,
    detect_model_profile,
    recommended_models_for_tier,
)


class TestTierClassifier:
    @pytest.mark.parametrize("vram,expected", [
        (0, HardwareTier.LOW),
        (8, HardwareTier.LOW),
        (12, HardwareTier.MID),
        (16, HardwareTier.MID),
        (24, HardwareTier.HIGH),
        (32, HardwareTier.HIGH),
        (48, HardwareTier.EXTREME),
        (80, HardwareTier.EXTREME),
    ])
    def test_classify(self, vram, expected):
        assert _classify_tier(vram) is expected


class TestRecommendedModels:
    @pytest.mark.parametrize("tier", list(HardwareTier))
    def test_every_tier_has_all_roles(self, tier):
        models = recommended_models_for_tier(tier)
        for role in ("fast", "coder", "deep", "vision", "embedding", "verifier"):
            assert role in models and models[role]


class TestDetect:
    def test_detect_returns_valid_profile(self):
        prof = detect_model_profile()
        assert isinstance(prof, HardwareModelProfile)
        assert prof.tier in HardwareTier
        assert prof.recommended_models
        assert all(cmd.startswith("ollama pull ") for cmd in prof.pull_commands())

    def test_pull_commands_deduped(self):
        prof = detect_model_profile()
        assert len(prof.pull_commands()) == len(set(prof.pull_commands()))
