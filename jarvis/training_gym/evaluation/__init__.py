"""training_gym/evaluation — V69 M62 S3C: does this adapter actually help, and is it still safe?

WHAT THIS SUBSYSTEM ANSWERS
---------------------------
S3B can build an adapter and prove the *artefact* is well-formed. That proves nothing
about the model. This package answers exactly one question, and refuses to answer any
broader one:

    Does this exact adapter, on this exact base model and tokenizer, improve approved
    target behaviour without causing unacceptable security or capability regressions?

The comparison is BASELINE (the pinned base model) against CANDIDATE (the same pinned
base model plus the exact validated adapter). Every input either side receives is
identical by construction, and the plan digest covers the construction — a comparison
where the two arms were not asked the same question measures the harness, not the model.

WHAT THIS SUBSYSTEM IS NOT
--------------------------
It does not promote, activate, merge, convert or register anything. The Model Registry
bridge produces a *proposal document* and writes no registry. The strongest verdict this
package can reach on its own is ``ELIGIBLE_FOR_HUMAN_REVIEW``, and a synthetic run can
never reach even that.

DISCIPLINE
----------
  * no ML framework is imported anywhere in this package at module scope — the
    production backend's imports live inside one function, behind every preflight gate;
  * no network, no download, no install, no subprocess, no shell;
  * a proposed tool call is read and scored, never executed;
  * held-out target answers live in a separate store that the backend request object has
    no field to reach — the isolation is structural, not a convention.

STATUS AS SHIPPED
-----------------
The production evaluation backend has never been run against a model. Every report this
package can currently produce carries ``EmpiricalStatus.SYNTHETIC_ONLY``.
"""
from __future__ import annotations

#: Bumped when any S3C record's shape changes.
EVALUATION_SCHEMA_VERSION = "m62.evaluation.1"

#: The implementation's own version, recorded so a verdict is attributable to code.
#:
#: V69 M62 S3Q.0 moved it from ``m62.s3c.1``. The measurement did not change — scoring,
#: metrics, statistics, gates and the generation policy are untouched and their digests
#: are unmoved — but the code that PRODUCES a verdict did: a plan now binds the exact
#: pack, answer key and execution order it authorises, a held-out corpus is durably
#: committed before the first model-facing call, and a completed run whose terminal
#: evidence failed to persist no longer reports success. A verdict attributable to the
#: earlier implementation carried none of those guarantees, so it is not the same
#: implementation.
EVALUATOR_VERSION = "m62.s3q0.1"

__all__ = ["EVALUATION_SCHEMA_VERSION", "EVALUATOR_VERSION"]
