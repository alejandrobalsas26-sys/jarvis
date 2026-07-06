"""
core/dataset_pipeline.py — V64 Milestone 16: failure → curated dataset pipeline.

Turns **evaluation failures** (M14 ``EvalRun``) into training-candidate examples
and forces every candidate through a fail-closed gauntlet before it may ever be
used for fine-tuning:

    eval failure → candidate → dedup → PII/secret scan → injection scan →
    source-trust check → quality filter → verifier review → HUMAN-APPROVAL gate
    → versioned JSONL

This is the *data* half of the "eval-infra-before-training" contract. It exists
so that fine-tuning (M17) can only ever consume vetted, versioned, provenance-
tagged data. The pipeline is deliberately conservative — it reuses the existing
trust primitives rather than inventing parallel ones:

  * PII / secret detection  → ``core.memory_router`` + ``core.dlp_sensor``
  * content-trust of refs   → ``core.source_trust`` (M10)
  * injection screening      → ``core.injection_firewall`` (M12)
  * eval failures            → ``core.eval_harness`` (M14)
  * verifier review          → injectable ``verify_fn`` (prod: ``verify_answer``)

Non-negotiable invariants enforced here:
  * **Nothing auto-approves.** ``evaluate`` can at best mark a candidate
    ``PENDING_REVIEW``; only an explicit human action (``approve``) yields
    ``APPROVED``, and only ``APPROVED`` rows are ever written to a dataset.
  * **Model text is not ground truth.** A ``MODEL_GENERATED`` candidate cannot
    pass review without a verifier verdict *and* trusted corroboration.
  * **No raw-internet training.** Candidates whose only support is an untrusted /
    blocked source are rejected.
  * **No secrets in datasets.** Any secret/PII match quarantines the candidate.
  * **Reproducible + versioned.** IDs are content hashes; timestamps are injected
    (``now_ts``); datasets are written to immutable ``<version>/`` dirs with a
    content-hash manifest. Rejections are never hidden — every gate verdict is
    recorded on the example.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from loguru import logger

PIPELINE_VERSION = "v64.m16"

# An injectable verifier: (prompt, ideal_output) -> verdict. The verdict may be a
# ``VerificationResult``-like object (``.verified`` / ``.confidence``), a dict, or
# a ``(verified, confidence)`` tuple. ``None`` ⇒ no verifier attached.
VerifyFn = Callable[[str, str], Awaitable[object]]


# ── provenance & status ───────────────────────────────────────────────────────
class ExampleProvenance(str, Enum):
    """Where a candidate's *ideal_output* came from — decides how much scrutiny
    it needs. Only ground-truth provenances may pass review without a model
    verifier; ``MODEL_GENERATED`` is never trusted on its own."""

    HUMAN = "human"                       # a human authored/curated the target
    DETERMINISTIC = "deterministic"       # a deterministic tool produced it (e.g. analyzer)
    EVAL_GROUND_TRUTH = "eval_ground_truth"  # the eval case's own recorded ground truth
    VERIFIED_TOOL = "verified_tool"       # a trusted tool result, already verified
    MODEL_GENERATED = "model_generated"   # another model's output — NOT ground truth

    @property
    def is_ground_truth(self) -> bool:
        """True for provenances that may be treated as trustworthy targets."""
        return self is not ExampleProvenance.MODEL_GENERATED


class CandidateStatus(str, Enum):
    """Terminal disposition of a candidate after the automatic gauntlet (+human)."""

    PENDING_REVIEW = "pending_review"       # survived all automatic gates; awaits a human
    DROPPED_DUPLICATE = "dropped_duplicate"  # already present in the corpus/batch
    QUARANTINED = "quarantined"             # secret/PII or injection — never trainable as-is
    REJECTED = "rejected"                   # failed source-trust / quality / verifier
    APPROVED = "approved"                   # a human explicitly approved it (only writable state)
    HUMAN_REJECTED = "human_rejected"       # a human explicitly rejected it

    @property
    def is_blocked(self) -> bool:
        return self in (
            CandidateStatus.DROPPED_DUPLICATE, CandidateStatus.QUARANTINED,
            CandidateStatus.REJECTED, CandidateStatus.HUMAN_REJECTED,
        )


# ── gate result ───────────────────────────────────────────────────────────────
_SEV_INFO = "info"
_SEV_REJECT = "reject"
_SEV_QUARANTINE = "quarantine"
_SEV_DROP = "drop"


@dataclass(frozen=True)
class GateResult:
    """One gate's verdict. ``severity`` drives the terminal status when it fails."""

    gate: str
    passed: bool
    severity: str = _SEV_INFO   # info | reject | quarantine | drop (only meaningful if not passed)
    reason: str = ""

    def to_dict(self) -> dict:
        return {"gate": self.gate, "passed": self.passed,
                "severity": self.severity, "reason": self.reason}


# ── candidate & example schema ────────────────────────────────────────────────
def _norm(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def _content_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(_norm(p).encode("utf-8", "ignore"))
        h.update(b"\x1f")
    return h.hexdigest()


@dataclass(frozen=True)
class TrainingCandidate:
    """A proposed training example, pre-vetting. Built from an eval failure."""

    prompt: str
    ideal_output: str
    domain: str = "general"
    provenance: ExampleProvenance = ExampleProvenance.MODEL_GENERATED
    source_refs: tuple[str, ...] = ()
    failure_ref: str = ""          # eval case id this failure came from (traceability)
    failure_reason: str = ""       # what the model got wrong (never hide the regression)
    tags: tuple[str, ...] = ()

    def content_key(self) -> str:
        """Deterministic dedup key over (domain, prompt, ideal_output)."""
        return _content_hash(self.domain, self.prompt, self.ideal_output)

    def to_dict(self) -> dict:
        return {
            "prompt": self.prompt, "ideal_output": self.ideal_output,
            "domain": self.domain, "provenance": self.provenance.value,
            "source_refs": list(self.source_refs), "failure_ref": self.failure_ref,
            "failure_reason": self.failure_reason, "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrainingCandidate":
        prov = d.get("provenance", ExampleProvenance.MODEL_GENERATED.value)
        try:
            provenance = ExampleProvenance(prov)
        except ValueError:
            provenance = ExampleProvenance.MODEL_GENERATED
        return cls(
            prompt=str(d.get("prompt", "")), ideal_output=str(d.get("ideal_output", "")),
            domain=str(d.get("domain", "general")), provenance=provenance,
            source_refs=tuple(d.get("source_refs", ())), failure_ref=str(d.get("failure_ref", "")),
            failure_reason=str(d.get("failure_reason", "")), tags=tuple(d.get("tags", ())),
        )


@dataclass(frozen=True)
class TrainingExample:
    """A vetted candidate: content + provenance + full gate audit + disposition.

    ``id`` is a content hash, so the same (prompt, output) always produces the
    same id — datasets dedup and diff deterministically."""

    id: str
    prompt: str
    ideal_output: str
    domain: str
    provenance: str
    status: CandidateStatus
    gates: tuple[GateResult, ...]
    source_refs: tuple[str, ...] = ()
    failure_ref: str = ""
    failure_reason: str = ""
    tags: tuple[str, ...] = ()
    review: dict = field(default_factory=dict)   # human audit trail: approver/ts/notes
    version: str = ""
    created_ts: float = 0.0

    @property
    def approved(self) -> bool:
        return self.status is CandidateStatus.APPROVED

    @property
    def passed_automatic(self) -> bool:
        """Survived every automatic gate and is awaiting a human decision."""
        return self.status is CandidateStatus.PENDING_REVIEW

    def to_dict(self) -> dict:
        return {
            "id": self.id, "prompt": self.prompt, "ideal_output": self.ideal_output,
            "domain": self.domain, "provenance": self.provenance, "status": self.status.value,
            "gates": [g.to_dict() for g in self.gates], "source_refs": list(self.source_refs),
            "failure_ref": self.failure_ref, "failure_reason": self.failure_reason,
            "tags": list(self.tags), "review": self.review, "version": self.version,
            "created_ts": self.created_ts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrainingExample":
        return cls(
            id=str(d["id"]), prompt=str(d.get("prompt", "")),
            ideal_output=str(d.get("ideal_output", "")), domain=str(d.get("domain", "general")),
            provenance=str(d.get("provenance", ExampleProvenance.MODEL_GENERATED.value)),
            status=CandidateStatus(d.get("status", CandidateStatus.REJECTED.value)),
            gates=tuple(GateResult(**g) for g in d.get("gates", [])),
            source_refs=tuple(d.get("source_refs", ())), failure_ref=str(d.get("failure_ref", "")),
            failure_reason=str(d.get("failure_reason", "")), tags=tuple(d.get("tags", ())),
            review=d.get("review", {}) or {}, version=str(d.get("version", "")),
            created_ts=float(d.get("created_ts", 0.0)),
        )


# ── pipeline configuration ────────────────────────────────────────────────────
@dataclass(frozen=True)
class PipelineConfig:
    """Deterministic thresholds. Conservative by default."""

    min_prompt_len: int = 4
    min_output_len: int = 2
    max_output_len: int = 20_000
    min_verifier_confidence: float = 0.6
    # A MODEL_GENERATED target must be corroborated by a trusted source to pass.
    require_trusted_source_for_model: bool = True
    # Degenerate/refusal markers that disqualify a target.
    reject_markers: tuple[str, ...] = (
        "as an ai language model", "i cannot help with that", "i can't help with that",
        "traceback (most recent call last)", "internalservererror",
    )


# ── verdict normalization ─────────────────────────────────────────────────────
def _verdict_fields(v: object) -> tuple[bool, float, str]:
    """Coerce any verifier return shape into (verified, confidence, reason)."""
    if v is None:
        return (False, 0.0, "no verdict")
    if isinstance(v, tuple):
        verified = bool(v[0])
        conf = float(v[1]) if len(v) > 1 else (1.0 if verified else 0.0)
        return verified, conf, ""
    if isinstance(v, dict):
        return (bool(v.get("verified")), float(v.get("confidence", 0.0) or 0.0),
                str(v.get("reasoning", "") or v.get("reason", "")))
    return (bool(getattr(v, "verified", False)), float(getattr(v, "confidence", 0.0) or 0.0),
            str(getattr(v, "reasoning", "") or ""))


# ── individual gates (pure) ───────────────────────────────────────────────────
def _gate_dedup(candidate: TrainingCandidate, seen_keys: frozenset[str]) -> GateResult:
    if candidate.content_key() in seen_keys:
        return GateResult("dedup", False, _SEV_DROP, "duplicate of an existing/earlier example")
    return GateResult("dedup", True)


def _gate_secret_pii(candidate: TrainingCandidate) -> GateResult:
    """Quarantine if prompt or target carries a secret / PII. Reuses the existing
    detectors — never a re-implementation."""
    from core.dlp_sensor import classify as dlp_classify
    from core.memory_router import contains_secret

    blob = f"{candidate.prompt}\n{candidate.ideal_output}"
    if contains_secret(blob):
        return GateResult("secret_pii", False, _SEV_QUARANTINE, "credential/secret pattern present")
    findings = dlp_classify(blob, source=candidate.failure_ref or "candidate")
    if findings:
        kinds = sorted({f["type"] for f in findings})
        return GateResult("secret_pii", False, _SEV_QUARANTINE, f"pii/secret: {','.join(kinds)}")
    return GateResult("secret_pii", True)


def _gate_injection(candidate: TrainingCandidate) -> GateResult:
    """Screen the training text itself for prompt-injection, so untrusted content
    can never be trained into policy. Uses the M12 firewall."""
    from core.injection_firewall import TrustOrigin, assess

    origin = (TrustOrigin.MODEL_GENERATED
              if candidate.provenance is ExampleProvenance.MODEL_GENERATED
              else TrustOrigin.FILE_UNTRUSTED)
    for label, text in (("prompt", candidate.prompt), ("ideal_output", candidate.ideal_output)):
        a = assess(text, origin)
        if a.detected and a.quarantine_required:
            return GateResult("injection", False, _SEV_QUARANTINE,
                              f"{label}: {a.attack_type.value} (conf={a.confidence:.2f})")
    return GateResult("injection", True)


def _gate_source_trust(candidate: TrainingCandidate, config: PipelineConfig, policy=None) -> GateResult:
    """Enforce content-trust on supporting sources (M10). A BLOCKED source is
    fatal. A MODEL_GENERATED target with no trusted corroboration is rejected —
    we do not train on raw-internet / unverified model text."""
    from core.source_trust import SourceTrustTier, classify_source

    tiers = []
    for ref in candidate.source_refs:
        if not str(ref).strip():
            continue
        rec = classify_source(str(ref), policy=policy)
        tiers.append(rec.tier)
        if rec.tier is SourceTrustTier.BLOCKED:
            return GateResult("source_trust", False, _SEV_REJECT, f"blocked source: {rec.domain}")

    if candidate.provenance.is_ground_truth:
        # Human/deterministic/eval targets are trusted on their own provenance.
        return GateResult("source_trust", True,
                          reason=f"tiers={[t.value for t in tiers]}" if tiers else "ground_truth")

    # MODEL_GENERATED: require at least one trusted (>= COMMUNITY) corroborating source.
    if config.require_trusted_source_for_model:
        has_trust = any(t.meets(SourceTrustTier.COMMUNITY) for t in tiers)
        if not has_trust:
            return GateResult("source_trust", False, _SEV_REJECT,
                              "model-generated target lacks a trusted corroborating source")
    return GateResult("source_trust", True, reason=f"tiers={[t.value for t in tiers]}")


def _gate_quality(candidate: TrainingCandidate, config: PipelineConfig) -> GateResult:
    prompt = candidate.prompt.strip()
    out = candidate.ideal_output.strip()
    if len(prompt) < config.min_prompt_len:
        return GateResult("quality", False, _SEV_REJECT, "prompt too short")
    if len(out) < config.min_output_len:
        return GateResult("quality", False, _SEV_REJECT, "target output empty/too short")
    if len(out) > config.max_output_len:
        return GateResult("quality", False, _SEV_REJECT, "target output exceeds max length")
    if _norm(out) == _norm(prompt):
        return GateResult("quality", False, _SEV_REJECT, "degenerate: target echoes prompt")
    low = out.lower()
    hit = next((m for m in config.reject_markers if m in low), None)
    if hit:
        return GateResult("quality", False, _SEV_REJECT, f"refusal/error marker: {hit!r}")
    return GateResult("quality", True)


# ── curation report ───────────────────────────────────────────────────────────
@dataclass
class CurationReport:
    """The outcome of curating a batch. Buckets are disjoint and total to input."""

    version: str
    examples: list[TrainingExample] = field(default_factory=list)

    def _by(self, status: CandidateStatus) -> list[TrainingExample]:
        return [e for e in self.examples if e.status is status]

    @property
    def pending_review(self) -> list[TrainingExample]:
        return self._by(CandidateStatus.PENDING_REVIEW)

    @property
    def quarantined(self) -> list[TrainingExample]:
        return self._by(CandidateStatus.QUARANTINED)

    @property
    def rejected(self) -> list[TrainingExample]:
        return self._by(CandidateStatus.REJECTED)

    @property
    def duplicates(self) -> list[TrainingExample]:
        return self._by(CandidateStatus.DROPPED_DUPLICATE)

    def summary(self) -> dict:
        return {
            "version": self.version, "total": len(self.examples),
            "pending_review": len(self.pending_review), "quarantined": len(self.quarantined),
            "rejected": len(self.rejected), "duplicates": len(self.duplicates),
        }


# ── the pipeline ──────────────────────────────────────────────────────────────
@dataclass
class DatasetPipeline:
    """Runs the fail-closed gauntlet. Deterministic except for the optional async
    verifier; fully offline-testable when ``verify_fn`` is injected."""

    config: PipelineConfig = field(default_factory=PipelineConfig)
    verify_fn: VerifyFn | None = None
    policy: object | None = None   # SourcePolicy | None — None ⇒ module default

    async def _review_gate(self, candidate: TrainingCandidate) -> GateResult:
        """Verifier review. Ground-truth provenance passes without a model (already
        trusted); MODEL_GENERATED is fail-closed — no verifier ⇒ reject."""
        if candidate.provenance.is_ground_truth and self.verify_fn is None:
            return GateResult("verifier", True, reason="trusted provenance; no model review needed")
        if self.verify_fn is None:
            return GateResult("verifier", False, _SEV_REJECT,
                              "model-generated target requires a verifier (none attached)")
        try:
            verdict = await self.verify_fn(candidate.prompt, candidate.ideal_output)
        except Exception as e:  # noqa: BLE001 — verifier failure fails closed, never crashes
            logger.warning(f"M16: verifier error for {candidate.failure_ref}: {e}")
            return GateResult("verifier", False, _SEV_REJECT, f"verifier error: {e}")
        verified, confidence, reason = _verdict_fields(verdict)
        if not verified or confidence < self.config.min_verifier_confidence:
            return GateResult("verifier", False, _SEV_REJECT,
                              f"unverified (verified={verified}, conf={confidence:.2f}) {reason}".strip())
        return GateResult("verifier", True, reason=f"conf={confidence:.2f}")

    async def evaluate(
        self, candidate: TrainingCandidate, *,
        seen_keys: frozenset[str] = frozenset(), version: str = "", now_ts: float = 0.0,
    ) -> TrainingExample:
        """Run the full automatic gauntlet. Returns a graded ``TrainingExample``
        whose best-case status is ``PENDING_REVIEW`` — never ``APPROVED``."""
        gates: list[GateResult] = []

        # Ordered gauntlet; stop at the first blocking failure but keep the audit.
        gates.append(_gate_dedup(candidate, seen_keys))
        status = _status_for(gates[-1])
        if status is None:
            for gate in (
                _gate_secret_pii(candidate),
                _gate_injection(candidate),
                _gate_source_trust(candidate, self.config, self.policy),
                _gate_quality(candidate, self.config),
            ):
                gates.append(gate)
                status = _status_for(gate)
                if status is not None:
                    break
        if status is None:
            review = await self._review_gate(candidate)
            gates.append(review)
            status = _status_for(review)
        final = status or CandidateStatus.PENDING_REVIEW

        return TrainingExample(
            id=candidate.content_key(), prompt=candidate.prompt,
            ideal_output=candidate.ideal_output, domain=candidate.domain,
            provenance=candidate.provenance.value, status=final, gates=tuple(gates),
            source_refs=candidate.source_refs, failure_ref=candidate.failure_ref,
            failure_reason=candidate.failure_reason, tags=candidate.tags,
            version=version, created_ts=now_ts,
        )

    async def curate(
        self, candidates: list[TrainingCandidate], *,
        existing: tuple[str, ...] | frozenset[str] = (), version: str = "v0", now_ts: float = 0.0,
    ) -> CurationReport:
        """Curate a batch. Dedups within the batch *and* against ``existing``
        (content keys of the current corpus). Order-stable and deterministic."""
        seen: set[str] = set(existing)
        out: list[TrainingExample] = []
        for cand in candidates:
            ex = await self.evaluate(cand, seen_keys=frozenset(seen), version=version, now_ts=now_ts)
            # Only occupy the dedup namespace with things that actually advanced.
            if ex.status is CandidateStatus.PENDING_REVIEW:
                seen.add(ex.id)
            out.append(ex)
        report = CurationReport(version=version, examples=out)
        logger.info(f"M16 curate[{version}]: {report.summary()}")
        return report


def _status_for(gate: GateResult) -> CandidateStatus | None:
    """Map a failed gate's severity to a terminal status (None ⇒ gate passed)."""
    if gate.passed:
        return None
    return {
        _SEV_DROP: CandidateStatus.DROPPED_DUPLICATE,
        _SEV_QUARANTINE: CandidateStatus.QUARANTINED,
        _SEV_REJECT: CandidateStatus.REJECTED,
    }.get(gate.severity, CandidateStatus.REJECTED)


# ── HUMAN-APPROVAL gate (the only path to APPROVED) ───────────────────────────
def approve(example: TrainingExample, approver: str, *, now_ts: float = 0.0, note: str = "") -> TrainingExample:
    """Human approval. Only a ``PENDING_REVIEW`` example may be approved — you
    cannot approve something the automatic gauntlet rejected/quarantined without
    re-curating it. Records an immutable audit entry."""
    if example.status is not CandidateStatus.PENDING_REVIEW:
        raise ValueError(f"cannot approve example {example.id} in status {example.status.value}")
    if not approver or not str(approver).strip():
        raise ValueError("approval requires a non-empty approver identity")
    review = {**example.review, "approver": approver, "approved_ts": now_ts, "note": note,
              "decision": "approved"}
    return _replace_status(example, CandidateStatus.APPROVED, review)


def reject(example: TrainingExample, approver: str, *, now_ts: float = 0.0, note: str = "") -> TrainingExample:
    """Human rejection of a pending example. Recorded, never silently dropped."""
    if not approver or not str(approver).strip():
        raise ValueError("rejection requires a non-empty approver identity")
    review = {**example.review, "approver": approver, "rejected_ts": now_ts, "note": note,
              "decision": "rejected"}
    return _replace_status(example, CandidateStatus.HUMAN_REJECTED, review)


def _replace_status(example: TrainingExample, status: CandidateStatus, review: dict) -> TrainingExample:
    d = example.to_dict()
    d["status"] = status.value
    d["review"] = review
    return TrainingExample.from_dict(d)


# ── versioned dataset writer (APPROVED-only, immutable versions) ───────────────
@dataclass(frozen=True)
class DatasetManifest:
    version: str
    count: int
    content_sha256: str
    provenance_counts: dict
    domain_counts: dict
    skipped_unapproved: int
    pipeline_version: str
    created_ts: float

    def to_dict(self) -> dict:
        return asdict(self)


def _counts(examples: list[TrainingExample], key: Callable[[TrainingExample], str]) -> dict:
    out: dict[str, int] = {}
    for e in examples:
        out[key(e)] = out.get(key(e), 0) + 1
    return dict(sorted(out.items()))


def write_dataset(
    examples: list[TrainingExample], out_dir: str | Path, *,
    version: str, now_ts: float = 0.0, allow_overwrite: bool = False,
) -> DatasetManifest:
    """Write **only APPROVED** examples to an immutable ``<out_dir>/<version>/``
    directory (``train.jsonl`` + ``manifest.json``). Refuses to clobber an
    existing version unless ``allow_overwrite``. Non-approved rows are skipped
    and *counted* in the manifest (never silently hidden)."""
    approved = sorted((e for e in examples if e.approved), key=lambda e: e.id)
    skipped = len(examples) - len(approved)

    dest = Path(out_dir) / version
    train_path = dest / "train.jsonl"
    if train_path.exists() and not allow_overwrite:
        raise FileExistsError(f"dataset version already exists (immutable): {train_path}")
    dest.mkdir(parents=True, exist_ok=True)

    lines = [json.dumps(e.to_dict(), ensure_ascii=False, sort_keys=True) for e in approved]
    train_path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")

    content_sha = hashlib.sha256("\n".join(f"{e.id}:{_norm(e.ideal_output)}" for e in approved)
                                 .encode("utf-8", "ignore")).hexdigest()
    manifest = DatasetManifest(
        version=version, count=len(approved), content_sha256=content_sha,
        provenance_counts=_counts(approved, lambda e: e.provenance),
        domain_counts=_counts(approved, lambda e: e.domain),
        skipped_unapproved=skipped, pipeline_version=PIPELINE_VERSION, created_ts=now_ts,
    )
    (dest / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    logger.info(f"M16 write_dataset[{version}]: wrote {len(approved)} approved "
                f"(skipped {skipped}) sha={content_sha[:12]}")
    return manifest


def load_dataset(dataset_dir: str | Path) -> list[TrainingExample]:
    """Load a written dataset version's ``train.jsonl`` back into examples."""
    p = Path(dataset_dir) / "train.jsonl"
    if not p.exists():
        p = Path(dataset_dir)
    examples: list[TrainingExample] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            examples.append(TrainingExample.from_dict(json.loads(line)))
    return examples


# ── candidate construction from an eval run ───────────────────────────────────
# resolve_ideal(case, result) -> (ideal_output, provenance, source_refs) | None.
IdealResolver = Callable[[object, object], "tuple[str, ExampleProvenance, tuple[str, ...]] | None"]


def default_ideal_resolver(case, result) -> "tuple[str, ExampleProvenance, tuple[str, ...]] | None":
    """Derive a training target for a failed case *only* from trustworthy sources
    already attached to the case — never fabricated. Prefers the case's recorded
    ``ground_truth`` (EVAL_GROUND_TRUTH), then an operator-supplied
    ``context.ideal`` / ``context.ideal_output``. Returns ``None`` when no
    trustworthy target exists (the failure is logged for human authoring, not
    invented)."""
    ground_truth = getattr(case, "ground_truth", "") or ""
    if ground_truth.strip():
        return (ground_truth, ExampleProvenance.EVAL_GROUND_TRUTH, ())
    context = getattr(case, "context", {}) or {}
    ideal = context.get("ideal") or context.get("ideal_output")
    if ideal and str(ideal).strip():
        prov_raw = context.get("ideal_provenance", ExampleProvenance.HUMAN.value)
        try:
            provenance = ExampleProvenance(prov_raw)
        except ValueError:
            provenance = ExampleProvenance.HUMAN
        refs = tuple(context.get("source_refs", ()) or ())
        return (str(ideal), provenance, refs)
    return None


def candidates_from_eval_run(run, cases, *, resolve_ideal: IdealResolver | None = None) -> list[TrainingCandidate]:
    """Build training candidates from the **failed** results of an ``EvalRun``.

    Each failure becomes a candidate whose target comes from ``resolve_ideal``
    (default: the case's own ground truth / operator-supplied ideal). Failures
    for which no trustworthy target exists are skipped and logged — never
    fabricated. ``run``/``cases`` are duck-typed against ``eval_harness`` to keep
    this module import-light and offline-testable."""
    resolve = resolve_ideal or default_ideal_resolver
    by_id = {getattr(c, "id", ""): c for c in cases}
    candidates: list[TrainingCandidate] = []
    skipped = 0
    for result in getattr(run, "results", []):
        if getattr(result, "passed", True):
            continue
        case = by_id.get(getattr(result, "case_id", ""))
        if case is None:
            skipped += 1
            continue
        resolved = resolve(case, result)
        if resolved is None:
            skipped += 1
            continue
        ideal, provenance, source_refs = resolved
        failures = getattr(result, "failures", []) or []
        candidates.append(TrainingCandidate(
            prompt=getattr(case, "prompt", ""), ideal_output=ideal,
            domain=getattr(case, "domain", "general"), provenance=provenance,
            source_refs=source_refs, failure_ref=getattr(result, "case_id", ""),
            failure_reason="; ".join(str(f) for f in failures)[:500],
            tags=tuple(getattr(case, "tags", ())),
        ))
    if skipped:
        logger.info(f"M16: {skipped} failure(s) had no trustworthy target — left for human authoring")
    return candidates


# ── production factory ────────────────────────────────────────────────────────
def from_llm(llm_client, *, config: PipelineConfig | None = None, policy=None) -> DatasetPipeline:
    """Wire the real VERIFIER-role model (``core.verification.verify_answer``) as
    the review gate. The pipeline stays fail-closed: any verifier error or low
    confidence rejects the candidate."""
    from core.verification import verify_answer

    async def verify_fn(prompt: str, ideal_output: str):
        return await verify_answer(llm_client, prompt, ideal_output)

    return DatasetPipeline(config=config or PipelineConfig(), verify_fn=verify_fn, policy=policy)
