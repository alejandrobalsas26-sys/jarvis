"""
core/eval_harness.py — V64 Milestone 14: JARVIS evaluation harness.

The measurement layer that MUST exist before any fine-tuning. It runs versioned
``EvalCase`` datasets against a *target* (a turn, a research run, the firewall,
the security analyzer — anything that conforms to the small output contract),
scores each case **deterministically wherever possible** (model-graded only when
a rubric demands it), and produces reproducible JSON/JSONL results with baseline
comparison and regression detection.

Design (mission M14):
  * ``EvalCase`` mirrors ``assemble_task_decision`` inputs (prompt/domain/context)
    plus an ``Expect`` block of structured, deterministic assertions.
  * ``EvalResult`` is a superset aligned to ``VerificationResult`` +
    ``CriticEngine.score_result`` — it never redefines a second scorer.
  * Only the dimensions a case *specifies* are scored, so a case with no
    ground truth simply skips the correctness dimension (no false failures).
  * Runs are reproducible: ``EvalRun`` carries run metadata, JSONL round-trips,
    and ``compare_runs`` flags regressions per metric + pass-rate.
  * Timeouts fail closed to a failed result; a target exception is a failure, not
    a crash — one bad case never aborts the suite.

Target contract — an async callable ``target_fn(case) -> dict`` returning any of:
  answer, tools_used, domain, confidence, verified, citations (objects with a
  ``.valid``/``fetched`` field or dicts with ``fetched``), injection_detected,
  injection_quarantined, agent_count, graph_depth, tokens. Missing keys are
  treated as "not provided" and their dimensions are skipped.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from loguru import logger

HARNESS_VERSION = "v64.m14"

TargetFn = Callable[["EvalCase"], Awaitable[dict]]
GraderFn = Callable[["EvalCase", dict], Awaitable[float]]


@dataclass(frozen=True)
class Expect:
    """Structured, deterministic expectations for a case. Only set fields are
    scored (unset ⇒ that dimension is skipped, not failed)."""

    contains: tuple[str, ...] = ()
    not_contains: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    domain: str | None = None
    min_confidence: float | None = None
    verified: bool | None = None
    injection_detected: bool | None = None
    injection_quarantined: bool | None = None
    must_cite: bool | None = None          # every claim/citation must be validly fetched
    max_latency_s: float | None = None

    def to_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v)
                for k, v in asdict(self).items() if v not in ((), None)}

    @classmethod
    def from_dict(cls, d: dict) -> "Expect":
        d = d or {}
        return cls(
            contains=tuple(d.get("contains", ())),
            not_contains=tuple(d.get("not_contains", ())),
            required_tools=tuple(d.get("required_tools", ())),
            forbidden_tools=tuple(d.get("forbidden_tools", ())),
            domain=d.get("domain"),
            min_confidence=d.get("min_confidence"),
            verified=d.get("verified"),
            injection_detected=d.get("injection_detected"),
            injection_quarantined=d.get("injection_quarantined"),
            must_cite=d.get("must_cite"),
            max_latency_s=d.get("max_latency_s"),
        )


@dataclass(frozen=True)
class EvalCase:
    """One evaluation case. Mirrors the per-turn decision inputs + expectations."""

    id: str
    domain: str
    prompt: str
    expect: Expect = field(default_factory=Expect)
    context: dict = field(default_factory=dict)
    ground_truth: str = ""
    rubric: str = ""
    tags: tuple[str, ...] = ()
    timeout_s: float = 30.0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "domain": self.domain, "prompt": self.prompt,
            "expect": self.expect.to_dict(), "context": self.context,
            "ground_truth": self.ground_truth, "rubric": self.rubric,
            "tags": list(self.tags), "timeout_s": self.timeout_s,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EvalCase":
        return cls(
            id=str(d["id"]), domain=str(d.get("domain", "general")),
            prompt=str(d.get("prompt", "")), expect=Expect.from_dict(d.get("expect", {})),
            context=d.get("context", {}) or {}, ground_truth=str(d.get("ground_truth", "")),
            rubric=str(d.get("rubric", "")), tags=tuple(d.get("tags", ())),
            timeout_s=float(d.get("timeout_s", 30.0)),
        )


@dataclass
class EvalResult:
    """Per-case outcome. ``metrics`` maps dimension → {passed, detail/value}."""

    case_id: str
    domain: str
    passed: bool
    score: float
    metrics: dict = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    latency_s: float = 0.0
    error: str | None = None
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id, "domain": self.domain, "passed": self.passed,
            "score": round(self.score, 4), "metrics": self.metrics,
            "failures": list(self.failures), "latency_s": round(self.latency_s, 4),
            "error": self.error, "timestamp": self.timestamp,
        }


# ── deterministic dimension scoring ───────────────────────────────────────────
def _citations_valid(citations) -> bool:
    """Every citation must be validly fetched (no invented citations)."""
    if not citations:
        return False
    for c in citations:
        ok = getattr(c, "valid", None)
        if ok is None and isinstance(c, dict):
            ok = c.get("fetched", False) and c.get("tier", "untrusted") != "blocked"
        if not ok:
            return False
    return True


def evaluate_output(case: EvalCase, output: dict, latency_s: float) -> EvalResult:
    """Score *output* against *case*.expect deterministically. Pure."""
    ex = case.expect
    metrics: dict = {}
    failures: list[str] = []
    answer = str(output.get("answer", "") or "")

    def record(dim: str, ok: bool, detail: str = "", value=None) -> None:
        metrics[dim] = {"passed": bool(ok)}
        if detail:
            metrics[dim]["detail"] = detail
        if value is not None:
            metrics[dim]["value"] = value
        if not ok:
            failures.append(f"{dim}:{detail}" if detail else dim)

    if ex.contains:
        missing = [s for s in ex.contains if s.lower() not in answer.lower()]
        record("correctness", not missing, f"missing={missing}" if missing else "")
    if ex.not_contains:
        present = [s for s in ex.not_contains if s.lower() in answer.lower()]
        record("forbidden_output", not present, f"present={present}" if present else "")
    if case.ground_truth:
        ok = case.ground_truth.lower() in answer.lower()
        record("ground_truth", ok, "" if ok else "ground_truth not reflected")

    tools_used = [str(t) for t in output.get("tools_used", []) or []]
    if ex.required_tools:
        missing = [t for t in ex.required_tools if t not in tools_used]
        record("tool_choice", not missing, f"missing_tools={missing}" if missing else "")
    if ex.forbidden_tools:
        used = [t for t in ex.forbidden_tools if t in tools_used]
        record("tool_safety", not used, f"forbidden_tools_used={used}" if used else "")

    if ex.domain is not None:
        got = str(output.get("domain", ""))
        record("domain_routing", got == ex.domain, f"got={got} want={ex.domain}", value=got)
    if ex.min_confidence is not None:
        conf = float(output.get("confidence", 0.0) or 0.0)
        record("confidence", conf >= ex.min_confidence, f"conf={conf:.2f}<{ex.min_confidence}", value=conf)
    if ex.verified is not None:
        got = output.get("verified")
        record("verification", got == ex.verified, f"got={got} want={ex.verified}", value=got)

    if ex.injection_detected is not None:
        got = bool(output.get("injection_detected", False))
        record("injection_detection", got == ex.injection_detected,
               f"got={got} want={ex.injection_detected}", value=got)
    if ex.injection_quarantined is not None:
        got = bool(output.get("injection_quarantined", False))
        record("injection_resistance", got == ex.injection_quarantined,
               f"got={got} want={ex.injection_quarantined}", value=got)

    if ex.must_cite:
        ok = _citations_valid(output.get("citations"))
        record("citation_validity", ok, "" if ok else "invalid/invented citation")
    if ex.max_latency_s is not None:
        record("latency", latency_s <= ex.max_latency_s, f"latency={latency_s:.2f}s", value=round(latency_s, 3))

    scored = [m["passed"] for m in metrics.values()]
    score = (sum(scored) / len(scored)) if scored else 1.0
    passed = all(scored) if scored else True
    return EvalResult(
        case_id=case.id, domain=case.domain, passed=passed, score=score,
        metrics=metrics, failures=failures, latency_s=latency_s,
        timestamp=output.get("_ts", 0.0),
    )


@dataclass
class EvalRun:
    """A whole suite run: metadata + per-case results + aggregate summary."""

    run_id: str
    results: list[EvalResult] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def pass_rate(self) -> float:
        return sum(r.passed for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def mean_score(self) -> float:
        return sum(r.score for r in self.results) / len(self.results) if self.results else 0.0

    def metric_pass_rates(self) -> dict[str, float]:
        """Per-dimension pass rate across cases that exercised that dimension."""
        totals: dict[str, list[bool]] = {}
        for r in self.results:
            for dim, m in r.metrics.items():
                totals.setdefault(dim, []).append(bool(m.get("passed")))
        return {d: round(sum(v) / len(v), 4) for d, v in totals.items() if v}

    def domain_pass_rates(self) -> dict[str, float]:
        totals: dict[str, list[bool]] = {}
        for r in self.results:
            totals.setdefault(r.domain, []).append(r.passed)
        return {d: round(sum(v) / len(v), 4) for d, v in totals.items()}

    def summary(self) -> dict:
        return {
            "run_id": self.run_id, "cases": len(self.results),
            "pass_rate": round(self.pass_rate, 4), "mean_score": round(self.mean_score, 4),
            "passed": sum(r.passed for r in self.results),
            "failed": sum(not r.passed for r in self.results),
            "errors": sum(1 for r in self.results if r.error),
            "metric_pass_rates": self.metric_pass_rates(),
            "domain_pass_rates": self.domain_pass_rates(),
        }

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "metadata": self.metadata,
            "summary": self.summary(), "results": [r.to_dict() for r in self.results],
        }

    def to_jsonl(self) -> str:
        """One JSON object per line: header (summary+metadata) then each result."""
        lines = [json.dumps({"_type": "run_header", "run_id": self.run_id,
                             "metadata": self.metadata, "summary": self.summary()},
                            ensure_ascii=False)]
        lines += [json.dumps({"_type": "result", **r.to_dict()}, ensure_ascii=False)
                  for r in self.results]
        return "\n".join(lines) + "\n"

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_jsonl(), encoding="utf-8")
        return p


class EvalRunner:
    """Runs cases against a target and produces an ``EvalRun``. Target- and
    model-agnostic; deterministic scoring by default, optional model grader."""

    def __init__(self, target_fn: TargetFn, *, grader_fn: GraderFn | None = None) -> None:
        self.target_fn = target_fn
        self.grader_fn = grader_fn

    async def run_case(self, case: EvalCase) -> EvalResult:
        started = time.time()
        try:
            output = await asyncio.wait_for(self.target_fn(case), timeout=case.timeout_s)
        except asyncio.TimeoutError:
            return EvalResult(case_id=case.id, domain=case.domain, passed=False, score=0.0,
                              failures=["timeout"], error="timeout",
                              latency_s=time.time() - started, timestamp=started)
        except Exception as e:  # noqa: BLE001 — one bad case must not abort the suite
            logger.warning(f"EVAL: case {case.id} target error: {e}")
            return EvalResult(case_id=case.id, domain=case.domain, passed=False, score=0.0,
                              failures=[f"target_error:{e}"], error=str(e),
                              latency_s=time.time() - started, timestamp=started)
        latency = time.time() - started
        if not isinstance(output, dict):
            output = {"answer": str(output)}
        result = evaluate_output(case, output, latency)
        # Optional model-graded dimension (only if the case has a rubric).
        if self.grader_fn is not None and case.rubric:
            try:
                grade = float(await self.grader_fn(case, output))
                result.metrics["rubric"] = {"passed": grade >= 0.5, "value": round(grade, 3)}
                scored = [m["passed"] for m in result.metrics.values()]
                result.score = sum(scored) / len(scored)
                result.passed = all(scored)
            except Exception as e:  # noqa: BLE001 — grader failure never crashes the run
                logger.debug(f"EVAL: grader failed for {case.id}: {e}")
        return result

    async def run_suite(
        self, cases: list[EvalCase], *, run_id: str, model: str = "", notes: str = "",
        now_ts: float | None = None,
    ) -> EvalRun:
        results = [await self.run_case(c) for c in cases]
        meta = {
            "harness_version": HARNESS_VERSION, "model": model, "notes": notes,
            "case_count": len(cases), "created_at": now_ts if now_ts is not None else time.time(),
        }
        return EvalRun(run_id=run_id, results=results, metadata=meta)


# ── dataset loading / persistence ─────────────────────────────────────────────
def load_cases(path: str | Path) -> list[EvalCase]:
    """Load an EvalCase JSONL dataset (skips blank lines and run headers)."""
    p = Path(path)
    cases: list[EvalCase] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        obj = json.loads(line)
        if obj.get("_type") in ("run_header", "result"):
            continue
        cases.append(EvalCase.from_dict(obj))
    return cases


def save_cases(cases: list[EvalCase], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(c.to_dict(), ensure_ascii=False) for c in cases) + "\n",
                 encoding="utf-8")
    return p


# ── baseline comparison / regression detection ────────────────────────────────
@dataclass
class RegressionReport:
    baseline_run: str
    candidate_run: str
    pass_rate_delta: float
    mean_score_delta: float
    metric_deltas: dict[str, float]
    domain_deltas: dict[str, float]
    regressions: list[str]
    improvements: list[str]

    @property
    def has_regression(self) -> bool:
        return bool(self.regressions)

    def to_dict(self) -> dict:
        return {
            "baseline_run": self.baseline_run, "candidate_run": self.candidate_run,
            "pass_rate_delta": round(self.pass_rate_delta, 4),
            "mean_score_delta": round(self.mean_score_delta, 4),
            "metric_deltas": {k: round(v, 4) for k, v in self.metric_deltas.items()},
            "domain_deltas": {k: round(v, 4) for k, v in self.domain_deltas.items()},
            "regressions": list(self.regressions), "improvements": list(self.improvements),
            "has_regression": self.has_regression,
        }


def compare_runs(
    baseline: EvalRun, candidate: EvalRun, *, tolerance: float = 0.001,
) -> RegressionReport:
    """Compare two runs. A metric that drops by more than *tolerance* is a
    regression; one that rises is an improvement. Overall pass-rate/mean-score
    deltas are always reported so a model is never promoted on one metric alone."""
    b_metrics, c_metrics = baseline.metric_pass_rates(), candidate.metric_pass_rates()
    b_domains, c_domains = baseline.domain_pass_rates(), candidate.domain_pass_rates()

    metric_deltas: dict[str, float] = {}
    regressions: list[str] = []
    improvements: list[str] = []
    for dim in sorted(set(b_metrics) | set(c_metrics)):
        delta = c_metrics.get(dim, 0.0) - b_metrics.get(dim, 0.0)
        metric_deltas[dim] = delta
        if delta < -tolerance:
            regressions.append(f"{dim}:{delta:+.3f}")
        elif delta > tolerance:
            improvements.append(f"{dim}:{delta:+.3f}")

    domain_deltas = {
        d: candidate.domain_pass_rates().get(d, 0.0) - b_domains.get(d, 0.0)
        for d in sorted(set(b_domains) | set(c_domains))
    }
    pass_delta = candidate.pass_rate - baseline.pass_rate
    if pass_delta < -tolerance:
        regressions.append(f"overall_pass_rate:{pass_delta:+.3f}")

    return RegressionReport(
        baseline_run=baseline.run_id, candidate_run=candidate.run_id,
        pass_rate_delta=pass_delta, mean_score_delta=candidate.mean_score - baseline.mean_score,
        metric_deltas=metric_deltas, domain_deltas=domain_deltas,
        regressions=regressions, improvements=improvements,
    )


# ── real caller: evaluate the M12 firewall over an injection-resistance set ────
def firewall_eval_target() -> TargetFn:
    """A target that runs the M12 injection firewall on each case's untrusted
    content (``context.content`` from ``context.origin``) and reports whether it
    detected/quarantined the attack. Lets M14 measure firewall resistance."""
    from core.injection_firewall import TrustOrigin, assess

    async def target(case: EvalCase) -> dict:
        content = str(case.context.get("content", case.prompt))
        origin_name = str(case.context.get("origin", "web_untrusted"))
        try:
            origin = TrustOrigin(origin_name)
        except ValueError:
            origin = TrustOrigin.WEB_UNTRUSTED
        a = assess(content, origin)
        return {
            "answer": a.notes,
            "injection_detected": a.detected,
            "injection_quarantined": a.quarantine_required,
            "domain": "prompt_injection",
            "confidence": a.confidence,
            "tools_used": [],
            "_ts": time.time(),
        }

    return target
