"""core/qualification.py — V69 M59.3: bounded, reproducible prefix qualification.

Turns the M58 manual benchmark into a REUSABLE, BOUNDED qualification harness with a
machine-readable, content-safe result. It answers one question deterministically: do
the prompt-prefix / runner-identity invariants still hold, and — when a live server is
present and the host power state is comparable — does the warm path still meet its
threshold profile?

TWO LAYERS
----------
  * a DETERMINISTIC matrix (server-free): a small curated set of cases that assert the
    M59.1 identity invariants — same family shares a prefix, a compact-delta change
    stays compatible, and every runner/prefix change (language, num_ctx, authority,
    scope, tool schema) invalidates. These always run and never need Ollama.
  * an optional LIVE matrix (injected measure fn): a BOUNDED number of real
    generations, compared against a power-appropriate threshold profile. A missing
    server yields INSUFFICIENT_EVIDENCE — never a false PASS.

CONTENT SAFETY
--------------
The artifact carries only allowlisted synthetic fixture IDs, fingerprints, counts,
milliseconds and enum states — never a raw prompt, a generated body, a secret, or a
private path. The harness never downloads a model, writes an Ollama setting, mutates a
semantic collection, or touches git.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

QUALIFICATION_SCHEMA_VERSION = "m59.3.1"


# ══════════════════════════════════════════════════════════════════════════════
#  Fixtures — content-safe synthetic prompts (IDs are all the artifact stores)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class QualFixture:
    """One synthetic qualification prompt. ``prompt`` is used ONLY for a live
    generation and is NEVER serialized into the artifact — the ``fixture_id`` is."""

    fixture_id: str
    language: str
    family: str
    prompt: str


FIXTURES: dict[str, QualFixture] = {
    "GREETING_ES": QualFixture("GREETING_ES", "es", "CONCISE", "hola"),
    "BRIEF_MATH_ES": QualFixture("BRIEF_MATH_ES", "es", "CONCISE",
                                 "como saco la raiz cuadrada de un numero"),
    "STANDARD_PYTHON_ES": QualFixture("STANDARD_PYTHON_ES", "es", "EXPLANATORY",
                                      "explica la herencia en Python brevemente"),
    "GREETING_EN": QualFixture("GREETING_EN", "en", "CONCISE", "hello there"),
}


# ══════════════════════════════════════════════════════════════════════════════
#  Threshold profiles — never compare a cold run against a warm bound
# ══════════════════════════════════════════════════════════════════════════════
class ThresholdProfileName(str, Enum):
    WARM_AC = "WARM_AC"
    COLD_AC = "COLD_AC"
    WARM_BATTERY = "WARM_BATTERY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ThresholdProfile:
    """Bounded, power/warmth-appropriate limits. A cold run is judged against a cold
    profile; a battery run against a battery profile. Slower-because-battery is not a
    failure unless it violates the BATTERY profile's own bound."""

    name: str
    max_dispatch_ms: float
    max_prompt_eval_ms: float
    max_first_content_ms: float
    max_total_ms: float
    max_stable_fp_count: int = 1
    max_num_ctx_count: int = 1

    def snapshot(self) -> dict:
        return {
            "name": self.name, "max_dispatch_ms": self.max_dispatch_ms,
            "max_prompt_eval_ms": self.max_prompt_eval_ms,
            "max_first_content_ms": self.max_first_content_ms,
            "max_total_ms": self.max_total_ms,
            "max_stable_fp_count": self.max_stable_fp_count,
            "max_num_ctx_count": self.max_num_ctx_count,
        }


THRESHOLD_PROFILES: dict[str, ThresholdProfile] = {
    ThresholdProfileName.WARM_AC.value: ThresholdProfile(
        "WARM_AC", max_dispatch_ms=1000.0, max_prompt_eval_ms=3000.0,
        max_first_content_ms=4000.0, max_total_ms=30000.0),
    ThresholdProfileName.COLD_AC.value: ThresholdProfile(
        "COLD_AC", max_dispatch_ms=1500.0, max_prompt_eval_ms=20000.0,
        max_first_content_ms=25000.0, max_total_ms=60000.0),
    ThresholdProfileName.WARM_BATTERY.value: ThresholdProfile(
        "WARM_BATTERY", max_dispatch_ms=1500.0, max_prompt_eval_ms=6000.0,
        max_first_content_ms=8000.0, max_total_ms=45000.0),
    ThresholdProfileName.UNKNOWN.value: ThresholdProfile(
        "UNKNOWN", max_dispatch_ms=1500.0, max_prompt_eval_ms=20000.0,
        max_first_content_ms=25000.0, max_total_ms=60000.0),
}


def select_threshold_profile(*, power_profile: str, warm: bool) -> ThresholdProfile:
    """Pick the profile for the measured power state and warmth. Total."""
    pp = str(power_profile or "UNKNOWN").upper()
    if pp == "AC":
        return THRESHOLD_PROFILES["WARM_AC" if warm else "COLD_AC"]
    if pp == "BATTERY":
        return THRESHOLD_PROFILES["WARM_BATTERY"]
    return THRESHOLD_PROFILES["UNKNOWN"]


# ══════════════════════════════════════════════════════════════════════════════
#  Case results & verdicts
# ══════════════════════════════════════════════════════════════════════════════
class CaseVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    SKIPPED = "SKIPPED"
    DEGRADED = "DEGRADED"


@dataclass
class CaseResult:
    """One qualification case. Content-free."""

    case_id: str
    kind: str                        # "deterministic" | "live"
    verdict: CaseVerdict
    detail: str = ""
    metrics: dict = field(default_factory=dict)

    def snapshot(self) -> dict:
        return {"case_id": self.case_id, "kind": self.kind,
                "verdict": self.verdict.value, "detail": self.detail,
                "metrics": dict(self.metrics)}


# ── The deterministic matrix (server-free) ────────────────────────────────────
def _profiles(fixture: QualFixture, *, model="qwen3:8b", num_ctx=2048, think=False,
              authority_mode="STANDARD", scope_fingerprint="",
              tool_schema_fingerprint="", contract="BRIEF"):
    from core.inference_profile import profiles_for_shape
    from core.response_contract import ContractReason, ResponseContract, ResponseShape, \
        _BASE_SHAPES
    c = ResponseContract(contract)
    shape = ResponseShape(contract=c, reason=ContractReason.GENERAL_EDUCATIONAL,
                          language=fixture.language, **_BASE_SHAPES[c])
    return profiles_for_shape(
        shape, model=model, num_ctx=num_ctx, think=think,
        language=fixture.language, authority_mode=authority_mode,
        scope_fingerprint=scope_fingerprint,
        tool_schema_fingerprint=tool_schema_fingerprint)


def _case(case_id: str, ok: bool, detail: str, metrics: dict | None = None) -> CaseResult:
    return CaseResult(case_id=case_id, kind="deterministic",
                      verdict=CaseVerdict.PASS if ok else CaseVerdict.FAIL,
                      detail=detail, metrics=metrics or {})


def run_deterministic_matrix() -> list[CaseResult]:
    """The curated, bounded deterministic matrix. No server, no Cartesian product —
    one focused case per invariant the release must preserve."""
    from core.inference_profile import profile_compatibility
    out: list[CaseResult] = []
    greet = FIXTURES["GREETING_ES"]

    # 1. same family (INSTANT vs BRIEF) shares the stable prefix identity.
    live_i, _ = _profiles(greet, contract="INSTANT")
    live_b, _ = _profiles(greet, contract="BRIEF")
    out.append(_case("same_family_shares_prefix",
                     live_i.prefix_identity == live_b.prefix_identity,
                     "INSTANT and BRIEF share one prefix identity"))

    # 2. a compact-delta change stays runner+prefix compatible.
    v = profile_compatibility(live_i, live_b)
    out.append(_case("compact_delta_compatible", v.compatible,
                     "different contract, same family, still compatible"))

    # 3. es vs en invalidates the prefix.
    en, _ = _profiles(FIXTURES["GREETING_EN"], contract="INSTANT")
    out.append(_case("language_invalidates",
                     en.prefix_identity != live_i.prefix_identity,
                     "es and en prefixes differ"))

    # 4. same num_ctx is runner-compatible.
    a, _ = _profiles(greet, num_ctx=2048)
    b, _ = _profiles(greet, num_ctx=2048)
    out.append(_case("same_num_ctx_compatible",
                     profile_compatibility(a, b).runner_compatible,
                     "same num_ctx runner-compatible"))

    # 5. a num_ctx change invalidates the runner (fake/deterministic).
    c1024, _ = _profiles(greet, num_ctx=1024)
    out.append(_case("num_ctx_change_invalidates",
                     not profile_compatibility(c1024, a).runner_compatible,
                     "num_ctx 1024 vs 2048 invalidates runner"))

    # 6. an authority change invalidates the prefix (fake).
    auth, _ = _profiles(greet, authority_mode="ELEVATED")
    out.append(_case("authority_change_invalidates",
                     auth.prefix_identity != a.prefix_identity,
                     "authority change invalidates prefix"))

    # 7. a scope change invalidates the prefix (fake).
    scope, _ = _profiles(greet, scope_fingerprint="scope-xyz")
    out.append(_case("scope_change_invalidates",
                     scope.prefix_identity != a.prefix_identity,
                     "scope change invalidates prefix"))

    # 8. a tool-schema change invalidates the prefix (fake).
    tools, _ = _profiles(greet, tool_schema_fingerprint="toolfp-abc")
    out.append(_case("tool_schema_change_invalidates",
                     tools.prefix_identity != a.prefix_identity,
                     "tool-schema change invalidates prefix"))

    # 9. the derived prewarm profile is compatible with its live profile.
    live, prewarm = _profiles(greet, contract="BRIEF")
    out.append(_case("prewarm_derived_compatible",
                     profile_compatibility(prewarm, live).compatible,
                     "derived prewarm is runner+prefix compatible"))
    return out


# ── Live matrix (bounded; needs an injected measure fn) ───────────────────────
# A LiveMeasure runs ONE bounded generation for a fixture and returns a metrics dict
# (dispatch_ms, prompt_eval_ms, first_content_ms, total_ms, load_ms, cache_state,
# stable_fp, num_ctx) or {"error": <reason>} when the server is unreachable.
LiveMeasure = Callable[[QualFixture], dict]

# Documented hard caps on live generations, so a run can never become an unbounded
# benchmark on a 15 W CPU.
QUICK_LIVE_FIXTURES: tuple[str, ...] = ("GREETING_ES", "BRIEF_MATH_ES")
FULL_LIVE_FIXTURES: tuple[str, ...] = (
    "GREETING_ES", "BRIEF_MATH_ES", "STANDARD_PYTHON_ES", "GREETING_EN")
MAX_QUICK_LIVE_GENERATIONS = 4      # each fixture may repeat once to prove reuse
MAX_FULL_LIVE_GENERATIONS = 8


def evaluate_live_case(case_id: str, metrics: dict, profile: ThresholdProfile
                       ) -> CaseResult:
    """Judge one live measurement against a threshold profile. A server error is
    INSUFFICIENT_EVIDENCE, never a PASS or FAIL."""
    if not metrics or metrics.get("error"):
        return CaseResult(case_id, "live", CaseVerdict.INSUFFICIENT_EVIDENCE,
                          detail=str((metrics or {}).get("error") or "no_metrics"),
                          metrics={"error": (metrics or {}).get("error")})
    breaches: list[str] = []
    checks = (
        ("dispatch_ms", profile.max_dispatch_ms),
        ("prompt_eval_ms", profile.max_prompt_eval_ms),
        ("first_content_ms", profile.max_first_content_ms),
        ("total_ms", profile.max_total_ms),
    )
    for key, bound in checks:
        val = metrics.get(key)
        if isinstance(val, (int, float)) and val > bound:
            breaches.append(f"{key}>{bound}")
    verdict = CaseVerdict.FAIL if breaches else CaseVerdict.PASS
    return CaseResult(case_id, "live", verdict,
                      detail=("; ".join(breaches) if breaches
                              else f"within {profile.name}"),
                      metrics={k: metrics.get(k) for k in (
                          "dispatch_ms", "prompt_eval_ms", "first_content_ms",
                          "total_ms", "load_ms", "cache_state", "num_ctx")})


# ══════════════════════════════════════════════════════════════════════════════
#  Verdict aggregation & artifact
# ══════════════════════════════════════════════════════════════════════════════
def aggregate_verdict(cases: list[CaseResult], *, live_requested: bool) -> str:
    """The overall qualification verdict. A missing live server can never PASS the
    live dimension; a deterministic failure is always a FAIL."""
    det = [c for c in cases if c.kind == "deterministic"]
    live = [c for c in cases if c.kind == "live"]
    if any(c.verdict is CaseVerdict.FAIL for c in cases):
        return CaseVerdict.FAIL.value
    if any(c.verdict is CaseVerdict.DEGRADED for c in cases):
        return CaseVerdict.DEGRADED.value
    if live_requested:
        measured = [c for c in live if c.verdict is CaseVerdict.PASS]
        if not measured:
            # Live was asked for but nothing measured (server down) → not a PASS.
            return CaseVerdict.INSUFFICIENT_EVIDENCE.value
    if not det:
        return CaseVerdict.INSUFFICIENT_EVIDENCE.value
    return CaseVerdict.PASS.value


def build_artifact(
    cases: list[CaseResult],
    *,
    mode: str,
    live_requested: bool,
    timestamp: float,
    git: dict | None = None,
    host: dict | None = None,
    power_profile: str = "UNKNOWN",
    ollama_version: str | None = None,
    model_roles: dict | None = None,
    observed_residency: list | None = None,
    thresholds: ThresholdProfile | None = None,
    warnings: list | None = None,
) -> dict:
    """Assemble the bounded, machine-readable, content-safe qualification artifact.

    It contains NO raw prompt (only fixture IDs), NO generated body, and NO secret."""
    verdict = aggregate_verdict(cases, live_requested=live_requested)
    return {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "timestamp": timestamp,
        "mode": mode,
        "git": dict(git or {}),
        "host": dict(host or {}),
        "power_profile": str(power_profile or "UNKNOWN"),
        "ollama_version": ollama_version,
        "model_roles": dict(model_roles or {}),
        "observed_residency": list(observed_residency or []),
        "fixtures": sorted(FIXTURES.keys()),
        "thresholds": thresholds.snapshot() if thresholds is not None else None,
        "cases": [c.snapshot() for c in cases],
        "counts": {
            "passed": sum(1 for c in cases if c.verdict is CaseVerdict.PASS),
            "failed": sum(1 for c in cases if c.verdict is CaseVerdict.FAIL),
            "insufficient_evidence": sum(
                1 for c in cases if c.verdict is CaseVerdict.INSUFFICIENT_EVIDENCE),
            "skipped": sum(1 for c in cases if c.verdict is CaseVerdict.SKIPPED),
            "degraded": sum(1 for c in cases if c.verdict is CaseVerdict.DEGRADED),
        },
        "warnings": list(warnings or []),
        "verdict": verdict,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  M59.6 — release verdict aggregation
# ══════════════════════════════════════════════════════════════════════════════
class ReleaseVerdict(str, Enum):
    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
    FAIL = "FAIL"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


def release_verdict(
    *,
    deterministic_ok: bool,
    regression_ok: bool,
    ruff_ok: bool,
    compile_ok: bool,
    soak_ok: bool,
    security_ok: bool = True,
    bounded_ok: bool = True,
    orphan_ok: bool = True,
    live_verdict: str | None = None,
    warnings: list | None = None,
) -> str:
    """Aggregate the release qualification result. Never conceals a failure.

    PASS requires every mandatory gate green (deterministic tests, regression, ruff,
    compile, soak, no security failure, no unbounded growth, no orphan task). A live
    FAIL is a code regression → FAIL; a live INSUFFICIENT_EVIDENCE / DEGRADED (server
    unavailable or an incomparable power state) yields PASS_WITH_WARNINGS, never a
    silent PASS and never a false FAIL."""
    mandatory = (deterministic_ok, regression_ok, ruff_ok, compile_ok, soak_ok,
                 security_ok, bounded_ok, orphan_ok)
    if not all(mandatory):
        return ReleaseVerdict.FAIL.value
    lv = (live_verdict or "").upper()
    if lv == "FAIL":
        return ReleaseVerdict.FAIL.value
    if lv in ("INSUFFICIENT_EVIDENCE", "DEGRADED"):
        return ReleaseVerdict.PASS_WITH_WARNINGS.value
    if warnings:
        return ReleaseVerdict.PASS_WITH_WARNINGS.value
    return ReleaseVerdict.PASS.value


def host_profile_snapshot() -> dict:
    """A bounded, content-safe host descriptor. No private paths, no environment."""
    import platform
    try:
        import os
        cpu = os.cpu_count()
    except Exception:  # noqa: BLE001
        cpu = None
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "logical_cpus": cpu,
    }
