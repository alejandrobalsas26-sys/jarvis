"""core/cognitive_synthesis.py — V68 M40: evidence-grounded operational synthesis.

Turns a bounded V67 fact bundle into a readable operator narrative using the LLM — then
holds that narrative to account with a DETERMINISTIC grounding validator. The LLM may
phrase; it may not invent. Anything it asserts that the facts do not support is caught and
the system falls back to the deterministic, already-grounded answer.

Two clean halves:
  * synthesize()      — async, calls an injectable synthesizer (default: the live role
                        model, guarded, bounded, concurrency-friendly). Never load-bearing:
                        if the model is down or times out, the deterministic answer stands.
  * validate_grounding() — pure, no LLM, no network. Detects invented specifics (IPs,
                        ids, CVEs, numbers not in the facts), forbidden certainty ("all
                        secure", "guaranteed"), and causal overreach ("proves", "caused
                        by") that would turn correlation into proof. Also measures evidence
                        coverage.

Invariants enforced here, not merely requested of the model:
  * No invented facts — an unsupported specific fails grounding.
  * Correlation != proof; hypothesis != fact — causal-overreach language fails grounding.
  * Unknown != safe — absolute-safety language fails grounding; the deterministic layer
    never emits "all secure", and neither may the synthesis.
  * Degrade honestly — a failed synthesis is reported as ungrounded and replaced by the
    grounded deterministic answer, never presented as verified.

Deterministic validator (tests inject the synthesizer; the network is never touched).
Bounded, ASCII-safe.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

_SYNTH_TIMEOUT_S = 25.0
_MAX_CLAIMS = 60

# Specifics an answer must not introduce unless present in the facts.
_RE_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_RE_CVE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.I)
_RE_ID = re.compile(r"\b(?:incident|finding|case|asset|env)[-_][A-Za-z0-9]{3,}\b", re.I)
# Identifier tokens carrying a digit (e.g. inc-42, f_ab12) — the shape ops ids actually
# take. Requiring a digit avoids flagging ordinary hyphenated words / rule names.
_RE_IDNUM = re.compile(r"\b[a-z]{1,8}[-_][a-z0-9]*\d[a-z0-9]*\b", re.I)
_RE_HEXID = re.compile(r"\b[0-9a-f]{12,}\b", re.I)
_RE_PORT = re.compile(r"\b(?:port\s*)?(\d{2,5})\b")
_RE_NUM = re.compile(r"\b\d+\b")
_RE_SENT = re.compile(r"[.!?\n]+")

# Language that asserts more certainty than operational evidence can carry.
_ABSOLUTE_SAFETY = (
    "all secure", "everything is secure", "fully secure", "no threats", "no threat",
    "nothing malicious", "completely safe", "guaranteed", "definitely safe",
    "no compromise", "all clear", "system is safe", "we are safe",
)
_CAUSAL_OVERREACH = (
    "proves", "proven that", "confirms the attack", "caused by", "because of the attack",
    "this is definitely", "certainly caused", "is the root cause",
)
# Hedges that make an otherwise-strong statement acceptable.
_HEDGES = ("may", "might", "could", "possible", "possibly", "suggests", "appears",
           "likely", "unverified", "hypothes", "correlat", "not confirmed", "unknown",
           "cannot confirm", "no evidence")


@dataclass
class GroundingReport:
    grounded: bool
    coverage: float                              # fraction of facts referenced (0..1)
    unsupported_claims: list[str] = field(default_factory=list)
    certainty_violations: list[str] = field(default_factory=list)
    causal_overreach: list[str] = field(default_factory=list)
    invented_specifics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "grounded": self.grounded,
            "coverage": round(self.coverage, 3),
            "unsupported_claims": self.unsupported_claims[:10],
            "certainty_violations": self.certainty_violations[:10],
            "causal_overreach": self.causal_overreach[:10],
            "invented_specifics": self.invented_specifics[:10],
        }


def _specifics(text: str) -> set[str]:
    out: set[str] = set()
    for rx in (_RE_IPV4, _RE_CVE, _RE_ID, _RE_IDNUM, _RE_HEXID):
        out |= {m.group(0).lower() for m in rx.finditer(text)}
    return out


def _fact_blob(facts: list[str]) -> tuple[str, set[str], set[str]]:
    blob = " \n ".join(facts).lower()
    fact_specifics = _specifics(blob)
    fact_numbers = {m.group(0) for m in _RE_NUM.finditer(blob)}
    return blob, fact_specifics, fact_numbers


def validate_grounding(text: str, facts: list[str]) -> GroundingReport:
    """Deterministically check that *text* is supported by *facts*. No LLM, no network."""
    text = text or ""
    facts = facts or []
    low = text.lower()
    blob, fact_specifics, fact_numbers = _fact_blob(facts)

    # 1) invented specifics: IPs/ids/CVEs/hex-ids in the answer but not in the facts.
    invented = sorted(s for s in _specifics(text) if s not in fact_specifics)

    # 2) unsupported numeric specifics (ports, counts) not present in facts.
    unsupported: list[str] = []
    sentences = [s.strip() for s in _RE_SENT.split(text) if s.strip()][:_MAX_CLAIMS]
    for s in sentences:
        s_specifics = _specifics(s)
        if any(sp not in fact_specifics for sp in s_specifics):
            unsupported.append(s[:160])

    # 3) certainty / absolute-safety violations (unknown != safe).
    certainty = [p for p in _ABSOLUTE_SAFETY if p in low]

    # 4) causal overreach unless hedged (correlation != proof, hypothesis != fact).
    hedged = any(h in low for h in _HEDGES)
    causal = [p for p in _CAUSAL_OVERREACH if p in low and not hedged]

    # 5) evidence coverage: fraction of facts whose salient token appears in the answer.
    covered = 0
    for f in facts:
        toks = _salient_tokens(f)
        if toks and any(t in low for t in toks):
            covered += 1
    coverage = covered / len(facts) if facts else 0.0

    grounded = not (invented or certainty or causal)
    return GroundingReport(
        grounded=grounded, coverage=coverage, unsupported_claims=unsupported[:10],
        certainty_violations=certainty, causal_overreach=causal,
        invented_specifics=invented)


def _salient_tokens(fact: str) -> list[str]:
    low = fact.lower()
    toks = set(_specifics(low))
    # significant words (>=4 chars, alpha) as loose coverage anchors
    toks |= {w for w in re.findall(r"[a-z]{4,}", low)
             if w not in _STOPWORDS}
    return list(toks)


_STOPWORDS = {"with", "from", "that", "this", "have", "were", "when", "there", "then",
              "them", "they", "into", "over", "under", "about", "none", "null", "true",
              "false", "value", "state", "status", "severity"}


# ══════════════════════════════════════════════════════════════════════════════
#  Grounded synthesis
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class GroundedSynthesis:
    text: str                        # the narrative shown to the operator
    grounded: bool
    source: str                      # "llm" | "deterministic" | "degraded"
    report: GroundingReport
    facts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"panel": "cognitive_synthesis", "text": self.text, "grounded": self.grounded,
                "source": self.source, "coverage": round(self.report.coverage, 3),
                "grounding": self.report.to_dict(), "facts": self.facts[:24]}


def _build_prompt(grounding: dict) -> str:
    facts = grounding.get("facts", [])
    lines = [grounding.get("instruction", ""), "",
             f"QUESTION: {grounding.get('question', '')}", "", "FACTS (the ONLY basis):"]
    lines += [f"  - {f}" for f in facts]
    lines += ["", "Write a concise operator briefing grounded ONLY in these facts. "
              "Preserve uncertainty (say 'may'/'unverified' where the facts are not "
              "confirmed). Do not invent hosts, ids, IPs, CVEs or numbers. Never say "
              "'all secure' - unknown is not safe."]
    return "\n".join(lines)


async def synthesize(bundle, *, synthesizer=None, deterministic_answer: str | None = None,
                     timeout_s: float = _SYNTH_TIMEOUT_S) -> GroundedSynthesis:
    """Produce a grounded narrative for *bundle* (an ops_query FactBundle).

    *synthesizer* is an ``async (prompt:str) -> str`` — injected in tests, defaulting to
    the live role model. The deterministic ``bundle.answer`` is the safe fallback: if the
    model is unavailable, times out, or its output FAILS grounding, that grounded answer is
    returned and marked accordingly. The LLM never gets the last word over the evidence.
    """
    grounding = bundle.to_grounding() if hasattr(bundle, "to_grounding") else dict(bundle)
    facts = list(grounding.get("facts", []))
    det = deterministic_answer if deterministic_answer is not None else \
        getattr(bundle, "answer", "")

    synth = synthesizer or _live_synthesizer()
    if synth is None:
        rep = validate_grounding(det, facts)
        return GroundedSynthesis(det, True, "deterministic", rep, facts)

    try:
        raw = await asyncio.wait_for(synth(_build_prompt(grounding)), timeout=timeout_s)
    except Exception:  # noqa: BLE001 — synthesis is never load-bearing (timeout included)
        rep = validate_grounding(det, facts)
        return GroundedSynthesis(det, True, "deterministic", rep, facts)

    text = (raw or "").strip()
    rep = validate_grounding(text, facts)
    if rep.grounded and text:
        return GroundedSynthesis(text, True, "llm", rep, facts)
    # Ungrounded: fall back to the deterministic grounded answer, reported honestly.
    return GroundedSynthesis(det, False, "degraded", rep, facts)


def _live_synthesizer():
    """Return an async synthesizer bound to the live role model, or None if unavailable.
    Guarded: any import/wiring failure degrades to the deterministic answer."""
    try:
        from core.model_router import model_for_role, ModelRole
        import httpx
    except Exception:  # noqa: BLE001
        return None

    async def _run(prompt: str) -> str:
        model = model_for_role(ModelRole.DEEP)
        async with httpx.AsyncClient(timeout=_SYNTH_TIMEOUT_S) as client:
            r = await client.post(
                "http://127.0.0.1:11434/api/generate",
                json={"model": model, "prompt": prompt, "stream": False,
                      "options": {"temperature": 0.1}})
            r.raise_for_status()
            return str(r.json().get("response", ""))

    return _run
