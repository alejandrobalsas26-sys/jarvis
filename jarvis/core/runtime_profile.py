"""core/runtime_profile.py — V69 M56.6: power-aware deterministic runtime profiles.

WHAT THIS IS, AND WHAT IT REFUSES TO BE
---------------------------------------
A 15 W U-series laptop on battery is a different machine from the same laptop on AC.
Running a 45-second background prewarm while unplugged spends real battery on an
optimisation the operator may not want. So the runtime picks a PROFILE and derives
its background-work policy from it.

It does this by OBSERVING the power source, never by controlling it:

  * no Windows power plan is read for modification or changed — ever;
  * no hardware-control call, no powercfg, no WMI method invocation;
  * detection is one bounded ``psutil.sensors_battery()`` read, which needs no
    administrator rights and returns ``None`` on a desktop.

An undetectable power source is UNKNOWN and gets the conservative BALANCED policy —
never an aggressive assumption in either direction.

RELATION TO THE EXISTING POWER MONITOR
--------------------------------------
:mod:`core.power_monitor` already watches AC/battery transitions and retunes the
hardware profile's throughput knobs (pools / ctx). This module does not duplicate or
replace it: it reads the same signal and answers a different, M56-specific question —
what background residency work is ALLOWED right now. Both can coexist because neither
writes the other's state.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

# Below this charge, even BALANCED behaves like BATTERY_SAVER: an optional warmup is
# never worth the last few percent of a battery.
_CRITICAL_BATTERY_PCT = 25.0
_DETECT_TTL_S = 60.0


class PowerSource(str, Enum):
    AC = "AC"
    BATTERY = "BATTERY"
    UNKNOWN = "UNKNOWN"


class RuntimeProfile(str, Enum):
    AC_PERFORMANCE = "AC_PERFORMANCE"
    BALANCED = "BALANCED"
    BATTERY_SAVER = "BATTERY_SAVER"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProfilePolicy:
    """The deterministic policy derived from a profile. Pure data.

    Every field is a decision the residency subsystem actually consults — nothing is
    advisory decoration.
    """

    profile: RuntimeProfile
    background_prewarm_allowed: bool
    prewarm_delay_s: float
    dual_residency_recommended: bool
    keep_alive: str
    background_deep_allowed: bool
    max_generation_tokens: int
    embedding_batch_size: int
    rationale: str = ""

    def snapshot(self) -> dict:
        return {
            "profile": self.profile.value,
            "background_prewarm_allowed": self.background_prewarm_allowed,
            "prewarm_delay_s": self.prewarm_delay_s,
            "dual_residency_recommended": self.dual_residency_recommended,
            "keep_alive": self.keep_alive,
            "background_deep_allowed": self.background_deep_allowed,
            "max_generation_tokens": self.max_generation_tokens,
            "embedding_batch_size": self.embedding_batch_size,
            "rationale": self.rationale,
        }


# The complete, closed policy table. Deterministic: same profile -> same policy.
_POLICIES: dict[RuntimeProfile, ProfilePolicy] = {
    RuntimeProfile.AC_PERFORMANCE: ProfilePolicy(
        profile=RuntimeProfile.AC_PERFORMANCE,
        background_prewarm_allowed=True, prewarm_delay_s=0.0,
        dual_residency_recommended=True, keep_alive="30m",
        background_deep_allowed=True, max_generation_tokens=512,
        embedding_batch_size=32,
        rationale="on AC: warm the interactive model eagerly and keep it resident",
    ),
    RuntimeProfile.BALANCED: ProfilePolicy(
        profile=RuntimeProfile.BALANCED,
        # Allowed, but AFTER a short delay and at low priority, so boot-critical work
        # and the operator's first keystrokes are never competing with a warmup.
        background_prewarm_allowed=True, prewarm_delay_s=10.0,
        dual_residency_recommended=True, keep_alive="10m",
        background_deep_allowed=False, max_generation_tokens=384,
        embedding_batch_size=16,
        rationale="balanced: prewarm after a short delay at low priority",
    ),
    RuntimeProfile.BATTERY_SAVER: ProfilePolicy(
        profile=RuntimeProfile.BATTERY_SAVER,
        # The headline battery rule: no automatic full native prewarm by default.
        background_prewarm_allowed=False, prewarm_delay_s=0.0,
        dual_residency_recommended=False, keep_alive="5m",
        background_deep_allowed=False, max_generation_tokens=256,
        embedding_batch_size=8,
        rationale="on battery: no automatic prewarm, no background DEEP, "
                  "smaller caps and shorter keep_alive",
    ),
    RuntimeProfile.UNKNOWN: ProfilePolicy(
        profile=RuntimeProfile.UNKNOWN,
        # Conservative BALANCED behaviour — never an aggressive assumption.
        background_prewarm_allowed=True, prewarm_delay_s=10.0,
        dual_residency_recommended=True, keep_alive="10m",
        background_deep_allowed=False, max_generation_tokens=384,
        embedding_batch_size=16,
        rationale="power source undetectable: conservative balanced behaviour",
    ),
}


def policy_for(profile: RuntimeProfile) -> ProfilePolicy:
    """The policy for a profile. Total — an unknown value yields the UNKNOWN policy."""
    return _POLICIES.get(profile, _POLICIES[RuntimeProfile.UNKNOWN])


def psutil_power_reader() -> tuple[PowerSource, float | None]:
    """One bounded, read-only power-source read. Needs no administrator rights.

    Returns (source, battery_percent). A desktop (``sensors_battery() is None``) is
    reported as AC, which is factually what a mains-powered desktop is. Any failure
    is UNKNOWN — never a guess.
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return PowerSource.UNKNOWN, None
    try:
        battery = psutil.sensors_battery()
    except Exception:  # noqa: BLE001
        return PowerSource.UNKNOWN, None
    if battery is None:
        return PowerSource.AC, None      # no battery present = mains powered
    try:
        plugged = battery.power_plugged
        pct = float(battery.percent)
    except Exception:  # noqa: BLE001
        return PowerSource.UNKNOWN, None
    if plugged is None:
        return PowerSource.UNKNOWN, pct
    return (PowerSource.AC if plugged else PowerSource.BATTERY), pct


def classify_profile(source: PowerSource, percent: float | None = None) -> RuntimeProfile:
    """Map an observed power source to a profile. Pure and total.

    A low battery forces BATTERY_SAVER even on AC-with-low-charge machines that
    report plugged-in while still draining — the charge level is the risk, not the
    plug state alone.
    """
    if source is PowerSource.BATTERY:
        return RuntimeProfile.BATTERY_SAVER
    if source is PowerSource.AC:
        if percent is not None and percent <= _CRITICAL_BATTERY_PCT:
            return RuntimeProfile.BATTERY_SAVER
        return RuntimeProfile.AC_PERFORMANCE
    return RuntimeProfile.UNKNOWN


@dataclass
class RuntimeProfileState:
    """The current profile, its source, and any explicit operator override."""

    profile: RuntimeProfile = RuntimeProfile.UNKNOWN
    source: PowerSource = PowerSource.UNKNOWN
    battery_percent: float | None = None
    detected_at: float | None = None
    override: RuntimeProfile | None = None
    override_reason: str = ""
    policy_overrides: dict = field(default_factory=dict)

    @property
    def effective(self) -> RuntimeProfile:
        """The operator's explicit override always wins — M56.6 requires that a
        battery default can be overridden deliberately, never silently."""
        return self.override or self.profile

    def policy(self) -> ProfilePolicy:
        return policy_for(self.effective)

    def snapshot(self) -> dict:
        return {
            "profile": self.effective.value,
            "detected_profile": self.profile.value,
            "source": self.source.value,
            "battery_percent": self.battery_percent,
            "detected_at": self.detected_at,
            "override": self.override.value if self.override else None,
            "override_reason": self.override_reason,
            "policy_overrides": dict(self.policy_overrides),
            "policy": self.policy().snapshot(),
        }

    def summary(self) -> str:
        return "POWER: profile={} source={} battery={} override={}".format(
            self.effective.value, self.source.value,
            f"{self.battery_percent:.0f}%" if self.battery_percent is not None else "n/a",
            self.override.value if self.override else "none",
        )


class RuntimeProfileManager:
    """Detects the power source (bounded, cached) and exposes the derived policy.

    Never changes a power plan, never makes a hardware-control call, and never polls
    on the interactive path — detection is TTL-cached and refreshed explicitly.
    """

    def __init__(self, *, reader: Callable[[], tuple[PowerSource, float | None]] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 ttl_s: float = _DETECT_TTL_S) -> None:
        self._reader = reader or psutil_power_reader
        self._clock = clock
        self._ttl = ttl_s
        self.state = RuntimeProfileState()

    def detect(self, *, refresh: bool = False) -> RuntimeProfileState:
        now = self._clock()
        if (not refresh and self.state.detected_at is not None
                and (now - self.state.detected_at) <= self._ttl):
            return self.state
        try:
            source, pct = self._reader()
        except Exception:  # noqa: BLE001 — detection must never crash the runtime
            source, pct = PowerSource.UNKNOWN, None
        self.state.source = source
        self.state.battery_percent = pct
        self.state.profile = classify_profile(source, pct)
        self.state.detected_at = now
        return self.state

    def policy(self, *, refresh: bool = False) -> ProfilePolicy:
        return self.detect(refresh=refresh).policy()

    def set_override(self, profile: RuntimeProfile | str | None, *,
                     reason: str = "operator") -> RuntimeProfileState:
        """Explicit operator override. ``None`` clears it.

        Deliberately explicit: the battery default may be overridden, but only by an
        operator saying so — the runtime never upgrades itself out of BATTERY_SAVER.
        """
        if profile is None:
            self.state.override = None
            self.state.override_reason = ""
            self.state.policy_overrides = {}
            return self.state
        try:
            resolved = (profile if isinstance(profile, RuntimeProfile)
                        else RuntimeProfile(str(profile).strip().upper()))
        except ValueError:
            return self.state          # an invalid name changes nothing
        self.state.override = resolved
        self.state.override_reason = reason
        self.state.policy_overrides = {"profile": resolved.value, "reason": reason}
        return self.state

    # ── policy questions the residency subsystem asks ────────────────────────
    def allows_background_prewarm(self) -> bool:
        return self.policy().background_prewarm_allowed

    def prewarm_delay_s(self) -> float:
        return self.policy().prewarm_delay_s

    def recommends_dual_residency(self) -> bool:
        return self.policy().dual_residency_recommended

    def keep_alive(self) -> str:
        return self.policy().keep_alive

    def snapshot(self) -> dict:
        return self.state.snapshot()


# ── Process-global singleton ─────────────────────────────────────────────────
_manager: RuntimeProfileManager | None = None


def get_runtime_profile() -> RuntimeProfileManager:
    global _manager
    if _manager is None:
        _manager = RuntimeProfileManager()
    return _manager


def reset_runtime_profile(instance: RuntimeProfileManager | None = None) -> None:
    """Tests / a fresh process."""
    global _manager
    _manager = instance
