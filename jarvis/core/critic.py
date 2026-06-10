"""
core/critic.py — V58.0 COGNITIVE CORE self-evaluation / quality gates.

The CriticEngine scores plans and results across safety / correctness /
completeness / tool-necessity / evidence / reversibility / approval /
production-readiness dimensions and recommends repairs. It is strictly an
evaluator — it NEVER executes tools, touches the ToolExecutor, or performs I/O.
"""
from __future__ import annotations

import re

from core.cognitive_types import (
    CognitivePlan,
    ExecutionTrace,
    PlanStep,
    RiskLevel,
    CompletionStatus,
    risk_rank,
)

# Destructive / irreversible intent markers (defensive posture: flag, never run).
_DESTRUCTIVE_RE = re.compile(
    r"\b(rm\s+-rf|del\s+/|format\b|mkfs|fdisk|dd\s+if=|shutdown|reboot|halt|"
    r"drop\s+table|truncate\s+table|wipe|nuke|kill\s+-9|taskkill\s+/f|"
    r"reg\s+delete|rmdir\s+/s|remove-item.*-recurse|disable.*firewall|"
    r"erase|overwrite)\b",
    re.IGNORECASE,
)

# Tools considered high-risk / irreversible without explicit operator approval.
_HIGH_RISK_TOOLS = frozenset({
    "offensive_rpc", "run_shell_command", "network_scan", "forensic_capture",
    "network_quarantine", "process_governor", "windows_hardener",
})


class CriticEngine:
    """Read-only evaluator of cognitive plans and execution results."""

    # ── Plan scoring ──────────────────────────────────────────────────────────

    def score_plan(self, plan: CognitivePlan) -> dict:
        """Score a plan's quality dimensions and surface blocking concerns."""
        flags: list[str] = []
        safety = 1.0
        reversibility = 1.0
        approval_required = False

        for step in plan.plan_steps:
            blob = f"{step.action} {step.tool or ''} {step.tool_input}"
            if _DESTRUCTIVE_RE.search(blob):
                flags.append(f"destructive_action:step_{step.index}")
                safety = min(safety, 0.0)
                reversibility = min(reversibility, 0.1)
                approval_required = True
            if step.tool in _HIGH_RISK_TOOLS:
                flags.append(f"high_risk_tool:{step.tool}")
                safety = min(safety, 0.4)
                reversibility = min(reversibility, 0.3)
                approval_required = True
            if risk_rank(step.risk_level) >= risk_rank(RiskLevel.HIGH):
                approval_required = True
            if step.tool and not step.requires_approval and step.tool in _HIGH_RISK_TOOLS:
                flags.append(f"missing_approval_gate:step_{step.index}")

        tool_necessity = self._tool_necessity(plan.plan_steps)
        completeness = 1.0 if plan.plan_steps else 0.0
        if not any(s.action.lower().startswith(("verify", "validate", "confirm"))
                   for s in plan.plan_steps):
            flags.append("no_verification_step")
            completeness = min(completeness, 0.7)

        scores = {
            "safety": round(safety, 2),
            "completeness": round(completeness, 2),
            "tool_necessity": round(tool_necessity, 2),
            "reversibility": round(reversibility, 2),
            "operator_approval_required": approval_required,
            "production_readiness": round(min(safety, completeness, tool_necessity), 2),
        }
        overall = round(
            (scores["safety"] + scores["completeness"]
             + scores["tool_necessity"] + scores["reversibility"]) / 4.0,
            2,
        )
        approved = overall >= 0.5 and safety > 0.0
        return {
            "scores": scores,
            "flags": flags,
            "overall": overall,
            "approved": approved,
        }

    @staticmethod
    def _tool_necessity(steps: list[PlanStep]) -> float:
        """Penalize plans that invoke tools redundantly (same tool back-to-back)."""
        tool_steps = [s for s in steps if s.tool]
        if not tool_steps:
            return 1.0
        seen: list[str] = []
        redundant = 0
        for s in tool_steps:
            if seen and seen[-1] == s.tool:
                redundant += 1
            seen.append(s.tool)
        return max(0.0, 1.0 - redundant / max(1, len(tool_steps)))

    # ── Result scoring ──────────────────────────────────────────────────────-

    def score_result(self, objective: str, result: dict) -> dict:
        """Score a finished task result for correctness/completeness/evidence."""
        flags: list[str] = []
        traces = result.get("traces", []) or []
        errors = result.get("errors", []) or []
        status = result.get("status") or result.get("completion_status")

        n = max(1, len(traces))
        failed = sum(
            1 for t in traces
            if (isinstance(t, dict) and (t.get("error") or t.get("status") == "failed"))
        )
        correctness = max(0.0, 1.0 - failed / n)
        evidence_quality = min(1.0, len(traces) / 3.0) if traces else 0.0
        completeness = 1.0 if status in ("completed", CompletionStatus.COMPLETED.value) else 0.4

        if errors:
            flags.append(f"errors_present:{len(errors)}")
        if not traces:
            flags.append("no_evidence")
        if failed:
            flags.append(f"failed_steps:{failed}")

        overall = round((correctness + evidence_quality + completeness) / 3.0, 2)
        return {
            "scores": {
                "correctness": round(correctness, 2),
                "evidence_quality": round(evidence_quality, 2),
                "completeness": round(completeness, 2),
            },
            "flags": flags,
            "overall": overall,
            "passed": overall >= 0.5 and not failed,
        }

    # ── Failure-mode analysis ──────────────────────────────────────────────────

    def detect_failure_modes(self, trace: ExecutionTrace) -> list[str]:
        """Classify the failure modes present in a single execution trace."""
        modes: list[str] = []
        err = (trace.error or "").lower()
        obs = str(trace.observation or "").lower()

        if trace.status == CompletionStatus.BLOCKED:
            modes.append("blocked_by_guardrail")
        if "timeout" in err or "timed out" in err:
            modes.append("timeout")
        if "cancel" in err or "denegada" in obs or "denied" in obs or "cancelada" in obs:
            modes.append("operator_denied")
        if "no implementada" in obs or "not implemented" in err or "unknown" in err:
            modes.append("missing_tool")
        if "permission" in err or "access denied" in err or "privilege" in err:
            modes.append("insufficient_privilege")
        if "error" in obs and not modes:
            modes.append("tool_error")
        if trace.error and not modes:
            modes.append("unhandled_exception")
        return modes

    def recommend_repair(self, failures: list[str]) -> list[str]:
        """Map detected failure modes to concrete, non-executing repair advice."""
        advice: dict[str, str] = {
            "blocked_by_guardrail": "Revise tool_input to satisfy guardrails; do not bypass.",
            "timeout": "Reduce scope or raise the per-step timeout; consider chunking.",
            "operator_denied": "Escalate for explicit operator approval before retrying.",
            "missing_tool": "Select a registered tool or decompose into supported steps.",
            "insufficient_privilege": "Defer to an operator-run step; never auto-escalate.",
            "tool_error": "Inspect inputs/preconditions and retry once with corrections.",
            "unhandled_exception": "Add input validation; capture trace and retry bounded.",
        }
        out: list[str] = []
        for f in failures:
            if advice.get(f) and advice[f] not in out:
                out.append(advice[f])
        return out
