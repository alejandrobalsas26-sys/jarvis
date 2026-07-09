"""core/telemetry_intel.py — V68 M39: collector rate, lag & reliability intelligence.

Evolves the V67 collector fabric's raw counters into BOUNDED, incremental temporal
intelligence — rates, lag, freshness, clock skew, out-of-order/drop/dedup/error ratios,
restart behaviour — and a derived reliability state. It is NOT a monitoring platform: no
infinite time series, no external TSDB. Each collector keeps one small fixed-size ring of
recent event timestamps; every metric is computed from that ring on demand.

Crucially it distinguishes *quiet* from *broken*: a legitimately low-volume collector with
recent events is HEALTHY, not FAILED. Only a collector that has fallen silent past its
staleness horizon, or is flapping/backpressured/noisy, is flagged — and telemetry never
concludes an attack from a collector going dark; it produces uncertainty, not compromise.

Determinism: every method takes an explicit ``now`` epoch (tests pin it); production passes
wall-clock. All state is bounded (fixed-size deques), so memory never grows with uptime.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

_WINDOW = 256               # max recent events retained per collector
_RESTART_WINDOW_S = 600.0   # look-back for restart-rate / flapping
_STALE_S = 300.0            # no event for this long → stale (default; overridable)
_BLIND_S = 900.0            # silent this long after having had events → blind
_NOISY_EPS = 50.0          # events/sec above this → noisy (default)
_LAG_S = 30.0              # median source→ingest lag above this → lagging
_RECOVERY_S = 120.0        # events within this long after a restart → recovering


class TelemetryState(str, Enum):
    HEALTHY = "healthy"
    LAGGING = "lagging"
    STALE = "stale"
    NOISY = "noisy"
    BACKPRESSURED = "backpressured"
    FLAPPING = "flapping"
    RECOVERING = "recovering"
    BLIND = "blind"
    DORMANT = "dormant"        # unconfigured / legitimately idle — NOT a failure
    UNKNOWN = "unknown"


def _now() -> float:
    return time.time()


def _epoch(value) -> float | None:
    """Parse a unix float or ISO-8601 string to an epoch; None if unparseable."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, AttributeError):
        return None


@dataclass
class TelemetryMeter:
    """Bounded rolling telemetry for one collector."""
    collector_id: str
    configured: bool = True
    events: int = 0
    errors: int = 0
    duplicates: int = 0
    drops: int = 0
    restarts: int = 0
    out_of_order: int = 0
    created_epoch: float = field(default_factory=_now)
    last_event_epoch: float | None = None
    last_success_epoch: float | None = None
    backlog: int = 0
    backpressure: bool = False
    _arrivals: deque = field(default_factory=lambda: deque(maxlen=_WINDOW))   # (ingest, observed|None)
    _restarts: deque = field(default_factory=lambda: deque(maxlen=64))        # restart epochs
    _last_observed: float | None = None
    _last_restart_count: int = 0

    # ── recording (incremental, O(1)) ─────────────────────────────────────────
    def record(self, *, now: float | None = None, observed: float | None = None,
               error: bool = False, duplicate: bool = False) -> None:
        now = now if now is not None else _now()
        self.events += 1
        self.last_event_epoch = now
        if not error:
            self.last_success_epoch = now
        if error:
            self.errors += 1
        if duplicate:
            self.duplicates += 1
        if observed is not None:
            if self._last_observed is not None and observed < self._last_observed:
                self.out_of_order += 1
            else:
                self._last_observed = observed
        self._arrivals.append((now, observed))

    def record_drop(self, n: int = 1) -> None:
        self.drops += max(0, n)

    def record_restart(self, *, now: float | None = None) -> None:
        now = now if now is not None else _now()
        self.restarts += 1
        self._restarts.append(now)

    def sync_restart_count(self, cumulative: int, *, now: float | None = None) -> None:
        """Reconcile a supervisor's cumulative restart count into timestamped restart
        events so the recent-window rate stays meaningful. Only the *new* restarts since
        the last sync are stamped (bounded); a decrease (supervisor reset) re-baselines."""
        cumulative = max(0, int(cumulative))
        if cumulative < self._last_restart_count:
            self._last_restart_count = cumulative      # supervisor re-registered
            return
        for _ in range(min(cumulative - self._last_restart_count, 64)):
            self.record_restart(now=now)
        self._last_restart_count = cumulative

    def set_backpressure(self, flag: bool, *, backlog: int = 0) -> None:
        self.backpressure = bool(flag)
        self.backlog = max(0, backlog)

    # ── derived metrics + state ────────────────────────────────────────────────
    def _rate_eps(self) -> float:
        if len(self._arrivals) < 2:
            return 0.0
        span = self._arrivals[-1][0] - self._arrivals[0][0]
        return (len(self._arrivals) - 1) / span if span > 0 else 0.0

    def _median_lag(self) -> float | None:
        lags = [ingest - obs for ingest, obs in self._arrivals if obs is not None]
        if not lags:
            return None
        lags.sort()
        return lags[len(lags) // 2]

    def _restart_rate(self, now: float) -> int:
        return sum(1 for t in self._restarts if now - t <= _RESTART_WINDOW_S)

    def snapshot(self, *, now: float | None = None, stale_s: float = _STALE_S,
                 noisy_eps: float = _NOISY_EPS) -> dict:
        now = now if now is not None else _now()
        eps = round(self._rate_eps(), 4)
        last_event_age = (now - self.last_event_epoch) if self.last_event_epoch else None
        last_success_age = (now - self.last_success_epoch) if self.last_success_epoch else None
        lag = self._median_lag()
        skew = (-lag) if lag is not None else None    # observed-ingest (signed)
        restart_rate = self._restart_rate(now)
        state = self._classify(now, eps, last_event_age, lag, restart_rate,
                               stale_s, noisy_eps)
        return {
            "collector_id": self.collector_id,
            "state": state.value,
            "events": self.events,
            "events_per_second": eps,
            "events_per_minute": round(eps * 60, 3),
            "last_event_age_s": _r(last_event_age),
            "last_success_age_s": _r(last_success_age),
            "median_lag_s": _r(lag),
            "clock_skew_s": _r(skew),
            "out_of_order_rate": round(self.out_of_order / max(1, self.events), 4),
            "error_rate": round(self.errors / max(1, self.events), 4),
            "dedup_ratio": round(self.duplicates / max(1, self.events + self.duplicates), 4),
            "drop_ratio": round(self.drops / max(1, self.events + self.drops), 4),
            "drops": self.drops,
            "queue_depth": self.backlog,
            "restarts": self.restarts,
            "restart_rate_10m": restart_rate,
            "backpressure": self.backpressure,
        }

    def _classify(self, now: float, eps: float, last_event_age: float | None,
                  lag: float | None, restart_rate: int, stale_s: float,
                  noisy_eps: float) -> TelemetryState:
        if not self.configured:
            return TelemetryState.DORMANT
        # never received anything: quiet/unconfigured, NOT a failure (unless long-blind)
        if self.events == 0:
            if now - self.created_epoch > _BLIND_S:
                return TelemetryState.BLIND      # expected but never observed
            return TelemetryState.DORMANT
        # backpressure dominates (data is being dropped)
        if self.backpressure or self.drops > 0:
            return TelemetryState.BACKPRESSURED
        # flapping: repeated restarts recently
        if restart_rate >= 3:
            return TelemetryState.FLAPPING
        # went silent after having produced events
        if last_event_age is not None and last_event_age > _BLIND_S:
            return TelemetryState.BLIND
        if last_event_age is not None and last_event_age > stale_s:
            return TelemetryState.STALE
        # recovering: restarted recently but events are flowing again
        if self._restarts and (now - self._restarts[-1]) <= _RECOVERY_S:
            return TelemetryState.RECOVERING
        # lagging: source→ingest delay high
        if lag is not None and lag > _LAG_S:
            return TelemetryState.LAGGING
        # noisy: event rate abnormally high
        if eps > noisy_eps:
            return TelemetryState.NOISY
        return TelemetryState.HEALTHY


def _r(v) -> float | None:
    return round(v, 3) if isinstance(v, (int, float)) else None


# ══════════════════════════════════════════════════════════════════════════════
#  Registry — one meter per collector; fed at the fabric's ingestion seam
# ══════════════════════════════════════════════════════════════════════════════
class TelemetryRegistry:
    def __init__(self) -> None:
        self._meters: dict[str, TelemetryMeter] = {}

    def meter(self, collector_id: str, *, configured: bool = True) -> TelemetryMeter:
        m = self._meters.get(collector_id)
        if m is None:
            m = TelemetryMeter(collector_id=collector_id, configured=configured)
            self._meters[collector_id] = m
        return m

    def record(self, collector_id: str, *, event: dict | None = None,
               now: float | None = None, error: bool = False,
               duplicate: bool = False) -> None:
        observed = _observed_epoch(event) if event is not None else None
        self.meter(collector_id).record(now=now, observed=observed, error=error,
                                        duplicate=duplicate)

    def record_drop(self, collector_id: str, n: int = 1) -> None:
        self.meter(collector_id).record_drop(n)

    def record_restart(self, collector_id: str, *, now: float | None = None) -> None:
        self.meter(collector_id).record_restart(now=now)

    def sync_restart_count(self, collector_id: str, cumulative: int, *,
                           now: float | None = None) -> None:
        self.meter(collector_id).sync_restart_count(cumulative, now=now)

    def set_backpressure(self, collector_id: str, flag: bool, *, backlog: int = 0) -> None:
        self.meter(collector_id).set_backpressure(flag, backlog=backlog)

    def snapshot(self, collector_id: str, *, now: float | None = None) -> dict | None:
        m = self._meters.get(collector_id)
        return m.snapshot(now=now) if m else None

    def all_snapshots(self, *, now: float | None = None) -> dict:
        return {cid: m.snapshot(now=now) for cid, m in self._meters.items()}


def _observed_epoch(event: dict) -> float | None:
    """Extract the source-observed time from a raw event or a signed envelope, without
    mutating it."""
    if not isinstance(event, dict):
        return None
    payload = event.get("__payload") if "__payload" in event else event
    if not isinstance(payload, dict):
        return None
    for key in ("observed_at", "timestamp", "ts", "time"):
        if key in payload:
            e = _epoch(payload[key])
            if e is not None:
                return e
    return None


# Module-level singleton — fed by collector_fabric.managed_broadcast.
telemetry = TelemetryRegistry()
