"""reasoning_distillation — V69 M67A: legacy reasoning traces into a trusted decision corpus.

WHAT THIS PACKAGE DOES
----------------------
Turns historical AI conversations the operator already has into typed, provenance-bearing,
privacy-classified, deduplicated, split, source-verified, quality-gated DECISION PROCEDURES — and
into a small library of explicit reasoning skills backed by evidence from several examples.

WHAT IT DOES NOT DO, AND WILL NOT
---------------------------------
It trains nothing, evaluates nothing, promotes nothing and reads no frozen holdout. There is no
code path in this package that loads a model, binds an adapter, or produces a number that could
imply promotion eligibility. That is M67A's hard freeze (§2), and it is enforced by what the
modules do not contain rather than by a flag they could be run with.

THE CLAIM THIS PACKAGE SUPPORTS
-------------------------------
Historical reasoning traces can be ingested with provenance, normalized, privacy-classified,
deduplicated, split without known leakage, separated from factual truth, transformed into typed
decision procedures, recursively critiqued under bounded rules, source-verified, quality-gated,
turned into evidence-backed reasoning skills, and prepared for a controlled future evaluation.

THE CLAIMS IT DOES NOT SUPPORT
------------------------------
That JARVIS thinks like Claude. That Claude's reasoning was replicated. That reasoning improved.
That the corpus is unbiased. That the traces are ground truth. That training will help. Every one
of those needs evidence this milestone deliberately did not gather.

HOW THE PIECES FIT
------------------
``storage`` owns the local, gitignored corpus root. ``adapters`` read source formats and refuse the
ones nobody has inspected. ``normalization`` attaches identity and an explicit privacy status.
``dedupe`` forms problem families; ``splitting`` places families, never fragments; ``holdout`` is
the firewall every reader passes through. ``operators`` is the closed reasoning taxonomy;
``distiller`` lifts spans and runs the bounded repair loop; ``critic`` hunts for unsupported claims
and ``verifier`` asks which single turn witnesses each field. ``quality`` decides ACCEPT / REVIEW /
REJECT over fifteen dimensions with no aggregate. ``skills`` requires multi-family evidence.
``benchmark`` builds the instrument and runs nothing. ``manifests`` makes a build a comparable
value, and ``pipeline`` composes the stages in the one order that is safe.

The decision vocabulary is JARVIS's own — ``core.epistemic_deliberation`` and
``core.mesh_contracts`` — and the hashing, validation, body-free rendering, privacy scanning and
near-duplicate scoring come from ``training_gym``. Nothing here is a parallel implementation of a
control this repository already has.
"""
from __future__ import annotations

from .config import CONFIG_VERSION, PIPELINE_VERSION, DistillationConfig, default_config
from .models import SCHEMA_VERSION, Disposition, ExportStatus, FactualState, RejectReason

__all__ = [
    "CONFIG_VERSION", "PIPELINE_VERSION", "SCHEMA_VERSION", "Disposition",
    "DistillationConfig", "ExportStatus", "FactualState", "RejectReason", "default_config",
]
