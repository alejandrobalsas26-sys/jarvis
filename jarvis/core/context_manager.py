"""
core/context_manager.py — V58.0 COGNITIVE CORE context compression + redaction.

Token-saver by design: trims low-signal noise from long conversations and tool
outputs while preserving high-signal security facts (indicators, severity,
hosts/users, blocked commands, tool errors, containment status). No external
dependencies — pure stdlib regex/heuristics so it runs on the CPU-bound host
without choking the event loop.
"""
from __future__ import annotations

import re

from core.cognitive_types import ContextPacket

# ── High-signal security vocabulary ───────────────────────────────────────────
# Lines/observations containing these tokens are preserved verbatim during
# compression even when the budget is tight.
_HIGH_SIGNAL = (
    "indicator", "ioc", "severity", "sev", "critical", "high", "alert",
    "host", "hostname", "user", "account", "ip", "domain", "hash", "sha256",
    "md5", "cve", "blocked", "denied", "quarantine", "contain", "isolate",
    "error", "fail", "exception", "timeout", "malware", "ransomware",
    "exfil", "c2", "beacon", "persistence", "privilege", "escalation",
    "timestamp", "mitre", "att&ck", "tactic", "technique",
)

# Low-signal lines safe to drop when over budget.
_LOW_SIGNAL = (
    "debug", "trace", "heartbeat", "keepalive", "polling", "tick",
    "verbose", "stack trace", "at line", "  file \"",
)

# ── Secret redaction patterns ─────────────────────────────────────────────────
_REDACTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Bearer / Authorization headers
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]{8,}=*"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)\bauthorization\s*[:=]\s*\S+"), "authorization: [REDACTED]"),
    # key/token/secret/password = value  (json, kv, env)
    (re.compile(
        r'(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key'
        r'|client[_-]?secret|private[_-]?key)\b\s*["\']?\s*[:=]\s*["\']?'
        r'([^\s"\',}]{4,})'
    ), r"\1=[REDACTED]"),
    # AWS access key id
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    # Generic long high-entropy hex/base64 secrets (>=32) — be conservative
    (re.compile(r"(?i)\b(?:sk|pk|ghp|gho|xox[baprs])[-_][A-Za-z0-9]{16,}"),
     "[REDACTED_TOKEN]"),
    # PEM private key blocks
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.DOTALL), "[REDACTED_PRIVATE_KEY]"),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
     "[REDACTED_JWT]"),
]


class ContextManager:
    """Compresses + redacts context before it reaches an LLM prompt."""

    def __init__(self, default_msg_budget: int = 12000,
                 default_tool_budget: int = 6000) -> None:
        self.default_msg_budget = default_msg_budget
        self.default_tool_budget = default_tool_budget

    # ── Redaction ─────────────────────────────────────────────────────────────

    def redact_secrets(self, text: str) -> str:
        """Strip API keys, tokens, passwords and private keys from free text."""
        if not text:
            return text
        out = text
        for pattern, repl in _REDACTION_PATTERNS:
            out = pattern.sub(repl, out)
        return out

    # ── Signal scoring ──────────────────────────────────────────────────────-

    @staticmethod
    def _signal_score(text: str) -> int:
        low = text.lower()
        score = sum(1 for kw in _HIGH_SIGNAL if kw in low)
        score -= sum(1 for kw in _LOW_SIGNAL if kw in low)
        return score

    def prioritize_context(self, items: list[dict]) -> list[dict]:
        """Stable-sort items by descending security signal (high-signal first)."""
        indexed = list(enumerate(items))

        def _key(pair):
            idx, item = pair
            text = item.get("content") or item.get("text") or str(item)
            # explicit numeric severity dominates keyword heuristics
            sev = 0.0
            for fld in ("severity", "sev", "score"):
                v = item.get(fld)
                if isinstance(v, (int, float)):
                    sev = max(sev, float(v))
            return (-(sev * 10 + self._signal_score(text)), idx)

        return [item for _, item in sorted(indexed, key=_key)]

    # ── Message compression ───────────────────────────────────────────────────

    def compress_messages(self, messages: list[dict],
                          budget_chars: int = 12000) -> dict:
        """
        Compress a chat history to fit ``budget_chars`` while keeping system
        prompts and the most recent turns intact. Drops low-signal middle turns
        and de-duplicates repeated observations.

        Returns a dict: {messages, dropped, char_count, summary}.
        """
        if not messages:
            return {"messages": [], "dropped": 0, "char_count": 0, "summary": ""}

        def _len(m: dict) -> int:
            return len(str(m.get("content", "")))

        total = sum(_len(m) for m in messages)
        if total <= budget_chars:
            return {
                "messages": messages,
                "dropped": 0,
                "char_count": total,
                "summary": "",
            }

        # Always preserve system messages + the last turn(s).
        system = [m for m in messages if m.get("role") == "system"]
        body = [m for m in messages if m.get("role") != "system"]

        # De-duplicate identical consecutive observations.
        deduped: list[dict] = []
        seen: set[str] = set()
        dup_dropped = 0
        for m in body:
            sig = f"{m.get('role')}:{str(m.get('content',''))[:200]}"
            if sig in seen and m.get("role") in ("tool", "assistant"):
                dup_dropped += 1
                continue
            seen.add(sig)
            deduped.append(m)

        kept: list[dict] = []
        running = sum(_len(m) for m in system)
        dropped = dup_dropped
        # Walk newest → oldest, keep while under budget; prefer high-signal.
        for m in reversed(deduped):
            mlen = _len(m)
            if running + mlen <= budget_chars or self._signal_score(
                str(m.get("content", ""))
            ) > 0 and running + mlen <= budget_chars * 1.1:
                kept.append(m)
                running += mlen
            else:
                dropped += 1
        kept.reverse()

        summary = ""
        if dropped:
            summary = (
                f"[CONTEXT COMPRESSED] {dropped} low-signal/duplicate message(s) "
                f"elided to fit the {budget_chars}-char budget."
            )

        out_messages = system + kept
        return {
            "messages": out_messages,
            "dropped": dropped,
            "char_count": sum(_len(m) for m in out_messages),
            "summary": summary,
        }

    # ── Tool-result summarization ──────────────────────────────────────────────

    def summarize_tool_results(self, results: list[dict],
                               budget_chars: int = 6000) -> str:
        """
        Condense a list of tool result dicts into a compact, redacted, high-signal
        text block. Preserves errors, severity, indicators, containment status;
        drops repetitive log noise.
        """
        if not results:
            return ""

        lines: list[str] = []
        for i, r in enumerate(results):
            if not isinstance(r, dict):
                lines.append(f"[{i}] {self.redact_secrets(str(r))[:300]}")
                continue
            # Errors are always high-signal.
            if "error" in r:
                lines.append(f"[{i}] ERROR: {self.redact_secrets(str(r['error']))[:300]}")
                continue
            # Pull high-signal fields if present.
            kept_fields = {}
            for k, v in r.items():
                kl = k.lower()
                if any(sig in kl for sig in _HIGH_SIGNAL) or kl in (
                    "status", "result", "action", "tool", "blocked"
                ):
                    kept_fields[k] = v
            if not kept_fields:
                # Fall back to a short preview of the whole dict.
                preview = self.redact_secrets(str(r))
                lines.append(f"[{i}] {preview[:200]}")
            else:
                preview = self.redact_secrets(str(kept_fields))
                lines.append(f"[{i}] {preview[:400]}")

        text = "\n".join(lines)
        if len(text) > budget_chars:
            text = text[:budget_chars] + "\n[…tool output truncated for token budget]"
        return self.redact_secrets(text)

    # ── Context packet ──────────────────────────────────────────────────────-

    def build_context_packet(self, inputs: dict) -> ContextPacket:
        """
        Assemble a compressed, redacted ContextPacket from raw inputs:
          {objective, facts[], constraints[], observations[]}
        """
        objective = self.redact_secrets(str(inputs.get("objective", "")))
        facts = [self.redact_secrets(str(f)) for f in inputs.get("facts", [])]
        constraints = [str(c) for c in inputs.get("constraints", [])]

        raw_obs = inputs.get("observations", [])
        prioritized = self.prioritize_context(
            [o if isinstance(o, dict) else {"content": str(o)} for o in raw_obs]
        )
        recent = [
            self.redact_secrets(str(o.get("content") or o.get("text") or o))[:400]
            for o in prioritized[:10]
        ]

        packet = ContextPacket(
            objective=objective,
            facts=facts,
            constraints=constraints,
            recent_observations=recent,
            redacted=True,
        )
        packet.char_count = (
            len(objective)
            + sum(len(f) for f in facts)
            + sum(len(o) for o in recent)
        )
        return packet
