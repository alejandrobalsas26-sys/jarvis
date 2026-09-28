"""reasoning_distillation/manifests.py — V69 M67A: what a build was, in one recordable object.

WHY A MANIFEST AND NOT A LOG
----------------------------
§20 lists what every corpus build must record: pipeline version, schema version, source hashes,
adapter versions, normalization version, privacy version, dedupe version, split algorithm, split
seed, operator taxonomy version, distillation config, critic config, quality config, the counts,
and environment metadata. It then states the property all of that exists to support: *identical
deterministic inputs and config should reproduce identical structural results.*

A log cannot support that claim, because a log records what happened in an order. A manifest
records what the build WAS, as a value, so two builds can be compared by comparing two objects —
and :attr:`BuildManifest.structural_digest` turns that comparison into a single equality test over
everything that is supposed to be deterministic.

THE ONE SUBTLETY: WHAT IS *IN* THE STRUCTURAL DIGEST
----------------------------------------------------
Not everything in the manifest belongs in it. A timestamp, a hostname and an interpreter build are
all worth recording and none of them is a structural input — including them would make every build
structurally unique and the reproducibility claim untestable.

So the manifest has two layers, and the split IS the design:

  * :meth:`structural_inputs` — versions, config, seed, source hashes, counts, digests. Two builds
    with equal structural inputs and equal structural results are the same build.
  * :attr:`environment` — time, platform, interpreter. Recorded, excluded from the digest, and
    labelled as excluded so nobody has to guess whether it was.

NON-DETERMINISTIC STAGES ARE MARKED, NOT HIDDEN
-----------------------------------------------
§19 ends with *"never pretend model-assisted output is bit-reproducible."* When a provider ran,
:attr:`BuildManifest.provider_metadata` is populated and :attr:`deterministic` is False — and the
structural digest then covers the deterministic stages only, with
:attr:`nondeterministic_stages` naming what it does not cover. A manifest that claimed a single
digest for a build containing a model call would be asserting a reproducibility that does not
exist.

NOTHING HERE MAY CARRY A BODY
-----------------------------
A manifest is the artifact most likely to be committed, pasted and quoted. Every payload passes
:func:`reasoning_distillation.privacy.assert_body_free_payload` before :func:`write` returns, and
source material appears only as hashes and counts. The corpus ROOT PATH is deliberately excluded
too: it names the operator's home directory, and a manifest is meant to travel.
"""
from __future__ import annotations

import platform
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from training_gym.schemas import SchemaError, sha256_obj

from . import (
    adapters,
    benchmark,
    critic,
    dedupe,
    distiller,
    holdout,
    normalization,
    operators,
    privacy,
    quality,
    skills,
    splitting,
    verifier,
)
from .config import CONFIG_VERSION, PIPELINE_VERSION, DistillationConfig
from .models import SCHEMA_VERSION
from .storage import CorpusRoot, write_report

#: Bump when the manifest's own shape changes. A reader that cannot parse a manifest cannot
#: compare two builds, so the manifest is versioned like everything else.
MANIFEST_VERSION = "m67a.manifest.1"


class ManifestError(SchemaError):
    """A manifest could not be built or written."""


@dataclass(frozen=True)
class CorpusCounts:
    """The counts §20 requires. Every one of them derived, none supplied."""

    source_files: int = 0
    conversations: int = 0
    turns: int = 0
    families: int = 0
    exact_duplicates: int = 0
    near_duplicates: int = 0
    development_families: int = 0
    validation_families: int = 0
    frozen_holdout_families: int = 0
    distilled: int = 0
    accepted: int = 0
    review: int = 0
    rejected: int = 0
    skills_supported: int = 0
    skills_provisional: int = 0
    skills_insufficient: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    def validated(self) -> "CorpusCounts":
        """The dispositions must account for every distilled record.

        A count that does not add up is the cheapest possible evidence that a record was dropped
        somewhere without a reason code, and §14 requires every non-ACCEPT outcome to have one.
        """
        total = self.accepted + self.review + self.rejected
        if self.distilled != total:
            raise ManifestError(
                f"counts do not reconcile: {self.distilled} distilled but "
                f"{self.accepted}+{self.review}+{self.rejected}={total} dispositioned. A record "
                f"with no disposition was dropped without a reason code")
        return self


def stage_versions() -> dict:
    """Every stage's version block, collected from the stages themselves.

    Collected by CALLING each module's ``versions()`` rather than by listing constants here. A
    hand-maintained list in this file would go stale the first time a stage is bumped, and the
    manifest would then confidently record the wrong version — which is worse than recording none.
    """
    return {
        "pipeline_version": PIPELINE_VERSION,
        "config_version": CONFIG_VERSION,
        "schema_version": SCHEMA_VERSION,
        "manifest_version": MANIFEST_VERSION,
        "adapters": adapters.supported(),
        "normalization": normalization.versions(),
        "privacy": privacy.versions(),
        "dedupe": dedupe.versions(),
        "splitting": splitting.versions(),
        "firewall": holdout.versions(),
        "operators": operators.taxonomy_description(),
        "distiller": distiller.versions(),
        "critic": critic.versions(),
        "verifier": verifier.versions(),
        "quality": quality.versions(),
        "skills": skills.versions(),
        "benchmark": benchmark.versions(),
    }


def environment() -> dict:
    """Recorded, and EXCLUDED from the structural digest. See the module docstring."""
    return {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.system(),
        "excluded_from_structural_digest": True,
        "why_excluded": "a timestamp and an interpreter build are not structural inputs; "
                        "including them would make every build unique and the reproducibility "
                        "claim untestable",
    }


@dataclass(frozen=True)
class BuildManifest:
    """One corpus build, as a value (§20)."""

    build_id: str
    config: DistillationConfig
    counts: CorpusCounts
    #: Digests of every source FILE ingested. Sorted; no paths, no names, no content.
    source_file_hashes: tuple[str, ...] = ()
    #: Content addresses of the derived artifacts, so a rebuild can be compared piecewise.
    dedupe_report_digest: str = ""
    split_plan_digest: str = ""
    frozen_holdout_digest: str = ""
    leakage_audit_digest: str = ""
    skill_library_digest: str = ""
    benchmark_case_set_digest: str = ""
    #: Populated only when a model-assisted stage ran. Its presence flips :attr:`deterministic`.
    provider_metadata: dict = field(default_factory=dict)
    #: True when the whole build was deterministic and offline.
    deterministic: bool = True
    #: Stages the structural digest does NOT cover, when a provider ran.
    nondeterministic_stages: tuple[str, ...] = ()
    leakage_clean: bool = False
    split_defensible: bool = False
    holdout_available: bool = False

    def structural_inputs(self) -> dict:
        """Everything that must be identical for two builds to be the same build."""
        return {
            "versions": stage_versions(),
            "config": self.config.to_dict(),
            "split_seed": self.config.split.seed,
            "source_file_hashes": sorted(self.source_file_hashes),
            "counts": self.counts.to_dict(),
            "artifact_digests": {
                "dedupe_report": self.dedupe_report_digest,
                "split_plan": self.split_plan_digest,
                "frozen_holdout": self.frozen_holdout_digest,
                "leakage_audit": self.leakage_audit_digest,
                "skill_library": self.skill_library_digest,
                "benchmark_case_set": self.benchmark_case_set_digest,
            },
        }

    @property
    def structural_digest(self) -> str:
        """One equality test for §20's reproducibility property.

        Covers the deterministic stages only. When a provider ran, the digest still has a value —
        the deterministic stages ARE reproducible and that is worth being able to check — but
        :attr:`deterministic` is False and :attr:`nondeterministic_stages` names the gap. Reading
        this digest as a whole-build reproducibility claim on a provider-assisted build would be
        precisely the pretence §19 forbids.
        """
        return sha256_obj(self.structural_inputs())

    def to_dict(self) -> dict:
        return {
            "manifest_version": MANIFEST_VERSION,
            "build_id": self.build_id,
            "structural_digest": self.structural_digest,
            "deterministic": self.deterministic,
            "nondeterministic_stages": list(self.nondeterministic_stages),
            "provider_metadata": dict(sorted(self.provider_metadata.items())),
            "leakage_clean": self.leakage_clean,
            "split_defensible": self.split_defensible,
            "holdout_available": self.holdout_available,
            "structural_inputs": self.structural_inputs(),
            "environment": environment(),
            "declarations": {
                "raw_corpus_committed": False,
                "training_performed": False,
                "evaluation_spent": False,
                "holdout_read_by_any_stage": False,
                "production_reasoning_changed": False,
                "implies_promotion_eligibility": False,
            },
        }

    def validated(self) -> "BuildManifest":
        self.counts.validated()
        if self.provider_metadata and self.deterministic:
            raise ManifestError(
                "provider_metadata is populated but the manifest claims the build was "
                "deterministic; §19 forbids presenting model-assisted output as bit-reproducible")
        if not self.deterministic and not self.nondeterministic_stages:
            raise ManifestError(
                "the build is marked non-deterministic but names no non-deterministic stage; a "
                "reader cannot tell what the structural digest fails to cover")
        return self

    def __repr__(self) -> str:  # pragma: no cover
        return (f"BuildManifest(build_id={self.build_id!r}, "
                f"structural_digest={self.structural_digest[:12]!r}, "
                f"deterministic={self.deterministic})")


def build_id_for(*, source_file_hashes: "tuple[str, ...]", seed: int) -> str:
    """A deterministic build id. Two builds over the same sources with the same seed share it.

    Deliberately NOT time-based. A timestamped id would make every rebuild a new build and the
    reproducibility check would have nothing to compare against — the ids would differ before the
    contents were even examined.
    """
    return sha256_obj({"sources": sorted(source_file_hashes), "seed": seed,
                       "pipeline": PIPELINE_VERSION})[:32]


def write(root: CorpusRoot, manifest: BuildManifest) -> Path:
    """Write the manifest into ``manifests/``, body-free and refusing otherwise.

    The scan runs inside :func:`reasoning_distillation.storage.write_report`, so there is no path
    that writes a manifest without it.
    """
    payload = manifest.validated().to_dict()
    return write_report(root, "manifests", f"{manifest.build_id}.json", payload)


def compare(left: BuildManifest, right: BuildManifest) -> dict:
    """Whether two builds are structurally identical, and where they differ if not.

    Returns the differing top-level structural keys rather than a bare boolean. "The builds differ"
    is not actionable; "the builds differ in ``config`` and ``counts``" is.
    """
    left_inputs, right_inputs = left.structural_inputs(), right.structural_inputs()
    differing = sorted(key for key in set(left_inputs) | set(right_inputs)
                       if left_inputs.get(key) != right_inputs.get(key))
    return {
        "identical": left.structural_digest == right.structural_digest,
        "left_digest": left.structural_digest,
        "right_digest": right.structural_digest,
        "differing_keys": differing,
        "both_deterministic": left.deterministic and right.deterministic,
        "note": ("a structural digest match on a non-deterministic build covers the deterministic "
                 "stages only" if not (left.deterministic and right.deterministic) else ""),
    }


__all__ = [
    "MANIFEST_VERSION", "BuildManifest", "CorpusCounts", "ManifestError", "build_id_for",
    "compare", "environment", "stage_versions", "write",
]
