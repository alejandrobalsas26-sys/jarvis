"""tests/test_runtime_profile_v69_m566.py — V69 M56.6 power-aware runtime profiles.

Running a 45-second background prewarm while unplugged spends real battery on an
optimisation the operator may not have asked for. These tests lock the profile
mapping, the derived policy, the explicit-override rule, and — most importantly — the
safety boundary: detection OBSERVES the power source and never controls it.
"""
from __future__ import annotations

from core.runtime_profile import (
    PowerSource,
    RuntimeProfile,
    RuntimeProfileManager,
    classify_profile,
    get_runtime_profile,
    policy_for,
    psutil_power_reader,
    reset_runtime_profile,
)


def teardown_function(_):
    reset_runtime_profile()


def _reader(source, pct=None):
    return lambda: (source, pct)


def _mgr(source, pct=None, **kw):
    return RuntimeProfileManager(reader=_reader(source, pct), **kw)


# ── classification ───────────────────────────────────────────────────────────
def test_ac_maps_to_performance():
    assert classify_profile(PowerSource.AC, 100.0) is RuntimeProfile.AC_PERFORMANCE
    assert classify_profile(PowerSource.AC, None) is RuntimeProfile.AC_PERFORMANCE


def test_battery_maps_to_saver():
    assert classify_profile(PowerSource.BATTERY, 90.0) is RuntimeProfile.BATTERY_SAVER


def test_unknown_source_maps_to_unknown_profile():
    assert classify_profile(PowerSource.UNKNOWN, None) is RuntimeProfile.UNKNOWN


def test_low_charge_forces_saver_even_when_plugged():
    """The charge level is the risk, not the plug state alone."""
    assert classify_profile(PowerSource.AC, 10.0) is RuntimeProfile.BATTERY_SAVER


# ── policy table ─────────────────────────────────────────────────────────────
def test_ac_performance_allows_eager_prewarm_and_dual_residency():
    p = policy_for(RuntimeProfile.AC_PERFORMANCE)
    assert p.background_prewarm_allowed is True
    assert p.prewarm_delay_s == 0.0
    assert p.dual_residency_recommended is True
    assert p.background_deep_allowed is True
    assert p.keep_alive == "30m"


def test_balanced_delays_prewarm_and_forbids_background_deep():
    p = policy_for(RuntimeProfile.BALANCED)
    assert p.background_prewarm_allowed is True
    assert p.prewarm_delay_s > 0.0
    assert p.background_deep_allowed is False


def test_battery_saver_disables_aggressive_background_work():
    p = policy_for(RuntimeProfile.BATTERY_SAVER)
    assert p.background_prewarm_allowed is False, "no automatic full prewarm on battery"
    assert p.background_deep_allowed is False
    assert p.dual_residency_recommended is False
    assert p.keep_alive == "5m"
    ac = policy_for(RuntimeProfile.AC_PERFORMANCE)
    assert p.max_generation_tokens < ac.max_generation_tokens
    assert p.embedding_batch_size < ac.embedding_batch_size


def test_unknown_profile_is_conservative_balanced_not_aggressive():
    unknown = policy_for(RuntimeProfile.UNKNOWN)
    balanced = policy_for(RuntimeProfile.BALANCED)
    assert unknown.prewarm_delay_s == balanced.prewarm_delay_s
    assert unknown.background_deep_allowed is False
    assert unknown.max_generation_tokens == balanced.max_generation_tokens


def test_policy_lookup_is_total():
    assert policy_for("not-a-profile").profile is RuntimeProfile.UNKNOWN  # type: ignore[arg-type]


def test_policy_is_deterministic():
    assert policy_for(RuntimeProfile.BALANCED) == policy_for(RuntimeProfile.BALANCED)


# ── detection ────────────────────────────────────────────────────────────────
def test_detection_on_ac():
    state = _mgr(PowerSource.AC, 95.0).detect()
    assert state.profile is RuntimeProfile.AC_PERFORMANCE
    assert state.source is PowerSource.AC
    assert state.battery_percent == 95.0
    assert state.detected_at is not None


def test_detection_on_battery_blocks_background_prewarm():
    mgr = _mgr(PowerSource.BATTERY, 80.0)
    assert mgr.allows_background_prewarm() is False
    assert mgr.keep_alive() == "5m"
    assert mgr.recommends_dual_residency() is False


def test_detection_failure_is_unknown_not_a_guess():
    def boom():
        raise RuntimeError("no sensor")

    mgr = RuntimeProfileManager(reader=boom)
    state = mgr.detect()
    assert state.profile is RuntimeProfile.UNKNOWN
    assert state.source is PowerSource.UNKNOWN
    # Conservative, still functional.
    assert mgr.allows_background_prewarm() is True
    assert mgr.prewarm_delay_s() > 0.0


def test_detection_is_ttl_cached_and_refreshable():
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return PowerSource.AC, 100.0

    t = [0.0]
    mgr = RuntimeProfileManager(reader=counting, clock=lambda: t[0], ttl_s=60.0)
    mgr.detect()
    mgr.detect()
    assert calls["n"] == 1, "detection must not poll on every call"
    t[0] = 120.0
    mgr.detect()
    assert calls["n"] == 2
    mgr.detect(refresh=True)
    assert calls["n"] == 3


# ── operator override ────────────────────────────────────────────────────────
def test_explicit_override_wins_over_detection():
    mgr = _mgr(PowerSource.BATTERY, 50.0)
    mgr.detect()
    assert mgr.allows_background_prewarm() is False
    mgr.set_override(RuntimeProfile.AC_PERFORMANCE, reason="operator wants a warm model")
    assert mgr.state.effective is RuntimeProfile.AC_PERFORMANCE
    assert mgr.allows_background_prewarm() is True
    snap = mgr.snapshot()
    assert snap["detected_profile"] == "BATTERY_SAVER"
    assert snap["override"] == "AC_PERFORMANCE"
    assert snap["policy_overrides"]["reason"] == "operator wants a warm model"


def test_override_accepts_a_string_and_can_be_cleared():
    mgr = _mgr(PowerSource.BATTERY, 50.0)
    mgr.detect()
    mgr.set_override("ac_performance")
    assert mgr.state.effective is RuntimeProfile.AC_PERFORMANCE
    mgr.set_override(None)
    assert mgr.state.override is None
    assert mgr.state.effective is RuntimeProfile.BATTERY_SAVER


def test_invalid_override_changes_nothing():
    mgr = _mgr(PowerSource.BATTERY, 50.0)
    mgr.detect()
    mgr.set_override("TURBO_MODE")
    assert mgr.state.override is None
    assert mgr.allows_background_prewarm() is False


def test_runtime_never_upgrades_itself_out_of_battery_saver():
    """Only an explicit override may leave BATTERY_SAVER; detection alone must not."""
    mgr = _mgr(PowerSource.BATTERY, 50.0)
    for _ in range(3):
        mgr.detect(refresh=True)
    assert mgr.state.effective is RuntimeProfile.BATTERY_SAVER
    assert mgr.state.override is None


# ── safety: observe, never control ───────────────────────────────────────────
def test_module_contains_no_power_control_primitive():
    """No power-plan mutation and no hardware-control call in the module's CODE.

    Docstrings are stripped first: the module documents what it refuses to do, and
    naming a forbidden API in prose is the opposite of calling it.
    """
    import ast
    import pathlib

    import core.runtime_profile as rp

    tree = ast.parse(pathlib.Path(rp.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)          # drop the docstring
    code = ast.unparse(tree).lower()
    for forbidden in ("powercfg", "setactivescheme", "subprocess", "os.system",
                      "win32api", "setsuspendstate", "powersetactive", "ctypes"):
        assert forbidden not in code, f"{forbidden} must never be called here"


def test_live_power_read_is_bounded_and_never_raises():
    source, pct = psutil_power_reader()
    assert source in set(PowerSource)
    assert pct is None or 0.0 <= pct <= 100.0


def test_summary_is_ascii_single_line():
    mgr = _mgr(PowerSource.AC, 88.0)
    mgr.detect()
    s = mgr.state.summary()
    assert s.isascii() and "\n" not in s
    assert "POWER:" in s


def test_snapshot_shape_is_complete():
    mgr = _mgr(PowerSource.AC, 88.0)
    mgr.detect()
    snap = mgr.snapshot()
    for key in ("profile", "detected_profile", "source", "battery_percent",
                "detected_at", "override", "override_reason", "policy_overrides",
                "policy"):
        assert key in snap
    for key in ("background_prewarm_allowed", "prewarm_delay_s", "keep_alive",
                "dual_residency_recommended", "max_generation_tokens"):
        assert key in snap["policy"]


def test_singleton_is_resettable():
    m = get_runtime_profile()
    assert get_runtime_profile() is m
    reset_runtime_profile()
    assert get_runtime_profile() is not m
