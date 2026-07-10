"""tests/test_telemetry_intel_v68.py — V68 M39 collector telemetry intelligence.

Proves the telemetry layer turns raw counters into honest temporal intelligence:
  * rate & lag — events/sec and median source->ingest lag are computed from the bounded
    ring, and drive LAGGING;
  * freshness vs quiet — a low-volume collector with recent events is HEALTHY (quiet !=
    broken); one that fell silent past the horizon is STALE, then BLIND;
  * reliability signals — out-of-order, dedup, error and drop ratios; backpressure and
    flapping (repeated restarts) dominate the derived state;
  * recovery — a just-restarted collector that is producing again reads RECOVERING;
  * boundedness — the arrival ring never exceeds its cap regardless of event volume;
  * a never-configured / never-seen collector is DORMANT, not a failure — until the long
    blind horizon, when absence of expected telemetry becomes BLIND (uncertainty, not
    an inferred compromise).

Deterministic: every timestamp is an explicit epoch pinned off T0; no wall-clock, no
network. Pure stdlib.
"""
from __future__ import annotations

from core.telemetry_intel import (
    TelemetryMeter,
    TelemetryRegistry,
    TelemetryState,
    _WINDOW,
    _observed_epoch,
)

T0 = 1_752_000_000.0   # fixed epoch anchor (2025-07-08T…Z), never wall-clock


def _meter(**kw) -> TelemetryMeter:
    return TelemetryMeter(collector_id="c1", created_epoch=T0, **kw)


# ── rate & freshness ────────────────────────────────────────────────────────────
class TestRateAndFreshness:
    def test_events_per_second_from_window(self):
        m = _meter()
        for i in range(10):
            m.record(now=T0 + i)          # one event per second
        snap = m.snapshot(now=T0 + 9)
        assert abs(snap["events_per_second"] - 1.0) < 1e-6
        assert abs(snap["events_per_minute"] - 60.0) < 1e-3

    def test_low_volume_recent_is_healthy_not_stale(self):
        m = _meter()
        m.record(now=T0)
        m.record(now=T0 + 5)
        assert m.snapshot(now=T0 + 6)["state"] == TelemetryState.HEALTHY.value

    def test_silent_collector_goes_stale_then_blind(self):
        m = _meter()
        m.record(now=T0)
        assert m.snapshot(now=T0 + 400)["state"] == TelemetryState.STALE.value
        assert m.snapshot(now=T0 + 1000)["state"] == TelemetryState.BLIND.value

    def test_last_event_age_reported(self):
        m = _meter()
        m.record(now=T0 + 10)
        assert m.snapshot(now=T0 + 25)["last_event_age_s"] == 15.0


# ── lag / skew / ordering ────────────────────────────────────────────────────────
class TestLagAndOrdering:
    def test_median_lag_and_lagging_state(self):
        m = _meter()
        for i in range(5):
            # ingest 40s after the source observed it → lagging
            m.record(now=T0 + i, observed=T0 + i - 40)
        snap = m.snapshot(now=T0 + 4)
        assert snap["median_lag_s"] == 40.0
        assert snap["clock_skew_s"] == -40.0
        assert snap["state"] == TelemetryState.LAGGING.value

    def test_out_of_order_rate(self):
        m = _meter()
        m.record(now=T0, observed=T0)
        m.record(now=T0 + 1, observed=T0 + 5)
        m.record(now=T0 + 2, observed=T0 + 3)   # observed goes backwards -> out of order
        assert m.out_of_order == 1
        assert m.snapshot(now=T0 + 2)["out_of_order_rate"] == round(1 / 3, 4)


# ── reliability signals ──────────────────────────────────────────────────────────
class TestReliability:
    def test_error_and_dedup_ratios(self):
        m = _meter()
        m.record(now=T0)
        m.record(now=T0 + 1, error=True)
        m.record(now=T0 + 2, duplicate=True)
        snap = m.snapshot(now=T0 + 2)
        assert snap["error_rate"] == round(1 / 3, 4)
        assert snap["dedup_ratio"] == round(1 / 4, 4)

    def test_backpressure_dominates_state(self):
        m = _meter()
        m.record(now=T0)
        m.set_backpressure(True, backlog=200)
        snap = m.snapshot(now=T0 + 1)
        assert snap["state"] == TelemetryState.BACKPRESSURED.value
        assert snap["queue_depth"] == 200

    def test_drops_mark_backpressured(self):
        m = _meter()
        m.record(now=T0)
        m.record_drop(3)
        snap = m.snapshot(now=T0 + 1)
        assert snap["drops"] == 3
        assert snap["drop_ratio"] == round(3 / 4, 4)
        assert snap["state"] == TelemetryState.BACKPRESSURED.value

    def test_flapping_on_repeated_restarts(self):
        m = _meter()
        m.record(now=T0)
        for i in range(3):
            m.record_restart(now=T0 + i)
        assert m.snapshot(now=T0 + 3)["state"] == TelemetryState.FLAPPING.value

    def test_recovering_after_single_restart(self):
        m = _meter()
        m.record_restart(now=T0)
        m.record(now=T0 + 5)                    # producing again shortly after restart
        assert m.snapshot(now=T0 + 6)["state"] == TelemetryState.RECOVERING.value

    def test_noisy_on_high_rate(self):
        m = _meter()
        for i in range(200):
            m.record(now=T0 + i * 0.001)        # ~1000 eps
        assert m.snapshot(now=T0 + 0.2)["state"] == TelemetryState.NOISY.value


# ── dormant vs blind (quiet != failure) ───────────────────────────────────────────
class TestDormantVsBlind:
    def test_never_seen_is_dormant_early(self):
        m = _meter()
        assert m.snapshot(now=T0 + 10)["state"] == TelemetryState.DORMANT.value

    def test_never_seen_becomes_blind_after_horizon(self):
        m = _meter()
        assert m.snapshot(now=T0 + 1000)["state"] == TelemetryState.BLIND.value

    def test_unconfigured_is_dormant(self):
        m = _meter(configured=False)
        m.record(now=T0)
        assert m.snapshot(now=T0 + 1)["state"] == TelemetryState.DORMANT.value


# ── boundedness (Rule of Silicon) ─────────────────────────────────────────────────
class TestBounded:
    def test_arrival_ring_is_capped(self):
        m = _meter()
        for i in range(_WINDOW * 4):
            m.record(now=T0 + i)
        assert len(m._arrivals) == _WINDOW
        assert m.events == _WINDOW * 4          # counter still totals everything


# ── registry + restart sync + event parsing ──────────────────────────────────────
class TestRegistry:
    def test_record_extracts_observed_from_event(self):
        reg = TelemetryRegistry()
        reg.record("c", event={"observed_at": T0 - 30}, now=T0)
        snap = reg.snapshot("c", now=T0)
        assert snap["median_lag_s"] == 30.0

    def test_signed_envelope_observed_extraction(self):
        assert _observed_epoch({"__payload": {"observed_at": T0}}) == T0

    def test_sync_restart_count_diffs(self):
        reg = TelemetryRegistry()
        reg.record("c", now=T0)
        reg.sync_restart_count("c", 2, now=T0)
        reg.sync_restart_count("c", 2, now=T0 + 1)   # no change -> no new restarts
        assert reg.meter("c").restarts == 2
        reg.sync_restart_count("c", 5, now=T0 + 2)
        assert reg.meter("c").restarts == 5

    def test_all_snapshots_keyed_by_collector(self):
        reg = TelemetryRegistry()
        reg.record("a", now=T0)
        reg.record("b", now=T0)
        snaps = reg.all_snapshots(now=T0)
        assert set(snaps) == {"a", "b"}
