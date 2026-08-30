"""scripts/build_quality_training_config.py — V69 M62 S3G: the candidate's configuration.

WHAT THIS IS
------------
The tracked generator for the first QUALITY-oriented training candidate's configuration
and its dry-run plan. The configuration document itself is a runtime artefact and stays
untracked, like every plan, token and adapter in this milestone; what is tracked is the
code that produces it, so a future session reproduces the configuration instead of
finding a JSON file nobody can re-derive.

It offers three complete options and recommends one. The options differ only in adapter
capacity, optimisation and how many passes are taken over the corpus — the model, the
revision, the corpus, the precision, the device and the artefact policy are identical
across all three, because those are the parts that are already qualified and changing
them would put a second variable into the first quality measurement.

WHAT IT NEVER DOES
------------------
  * It does not train. ``plan_training`` is a pure dry run: it creates no directory,
    opens no file for writing, imports no training framework and contacts no network.
  * It does not consume authority. A ``TRAIN:<plan-hash>`` token is DERIVED from a plan,
    not issued by one; nothing is spent by computing a plan, and this script never
    passes ``--execute`` to anything.
  * It does not print the token. The plan hash is printed and the token is derivable
    from it by whoever holds the plan; printing it here would put a single-use
    authorisation into console scrollback for no benefit.
  * It writes no absolute path into its output.

THE CANDIDATE IS NOT RUN-004
----------------------------
Operator ruling **H5** placed ``qwen3-06b-lora-smoke-live-004`` permanently outside
quality promotion. This configuration names a NEW run id, a NEW corpus and a NEW output
directory. Nothing here resumes, continues, mutates or reads run-004.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # pragma: no cover - layout shim, as the sibling CLIs do
    sys.path.insert(0, str(_ROOT))

NOW = "2026-08-12T00:00:00Z"

#: The new candidate's identity. Never ``smoke``, never ``active``, never ``promoted``.
RUN_ID = "qwen3-06b-lora-quality-live-001"
EXPERIMENT_NAME = "m62-s3g-defensive-quality-001"
RUN_INTENT = "QUALITY_CANDIDATE"

#: Pinned by §5 of PROGRESS.md and by the S3D.1 smoke record. An immutable commit sha,
#: never a branch or tag: a moving reference makes every downstream digest a claim about
#: whatever the reference pointed at that day.
BASE_MODEL_ID = "Qwen/Qwen3-0.6B"
BASE_MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
BASE_MODEL_PARAMETERS_B = 0.6

TRAINING_DATASET_ID = "m62-defensive-quality-train"
TRAINING_DATASET_VERSION = "v1"

# ══════════════════════════════════════════════════════════════════════════════
#  V69 M62 S3J — the SECOND quality candidate
# ══════════════════════════════════════════════════════════════════════════════
#  Candidate 001 is `EVALUATED_NOT_ELIGIBLE` and is now history: its run id, its corpus
#  version, its options and its config hash are frozen below and are not touched. This
#  section adds a second, independent candidate identity. Nothing here retrains,
#  resumes, reuses or relabels 001, and no adapter exists for 002.
RUN_ID_002 = "qwen3-06b-lora-quality-live-002"
EXPERIMENT_NAME_002 = "m62-s3j-defensive-quality-002"
TRAINING_DATASET_VERSION_002 = "v2"

# ══════════════════════════════════════════════════════════════════════════════
#  V69 M62 S3O — the THIRD quality candidate, a controlled single-axis experiment
# ══════════════════════════════════════════════════════════════════════════════
#  Candidates 001 and 002 are both `EVALUATED_NOT_ELIGIBLE`, terminal, and untouched
#  here. Candidate 003 is a NEW identity, never a relabelling or a resume of either.
#
#  It exists to answer ONE preregistered question (S3N §0), and it is built so that the
#  question cannot be confounded:
#
#      candidate 002 trains rendered as MODEL_DEFAULT   (the legacy implicit template
#                                                        default -- NOT "thinking off")
#      candidate 003 trains rendered as DISABLED        (the SAME representation
#                                                        evaluation has always generated
#                                                        under -- defect D37)
#
#  Everything else is held: the same corpus version `v2`, the same option `S3J`, and so
#  the same rank, alpha, dropout, learning rate, epochs, max_steps, warmup, batch,
#  gradient accumulation, seed, sequence length, device, precision and validation
#  cadence. The dials are shared by REFERENCE (`CANDIDATE_OPTION["003"] is the same
#  string as ["002"]`), not restated as a second copy that could drift.
#
#  What S3O does NOT claim: that DISABLED will restore 9/9 structured output, repair
#  stopping, or change any measured behaviour at all. D37's historical causality is
#  NOT_ESTABLISHED. This is a controlled experiment, not a predicted fix.
RUN_ID_003 = "qwen3-06b-lora-quality-live-003"
EXPERIMENT_NAME_003 = "m62-s3o-defensive-quality-003"
#: Unchanged from candidate 002 ON PURPOSE. `m62-defensive-quality-train v2`, the same
#: 182 promoted records, the same 154 TRAIN and 12 VALIDATION rows. There is no
#: `train-v3` and S3O creates none: a corpus change would be a second axis.
TRAINING_DATASET_VERSION_003 = TRAINING_DATASET_VERSION_002

# ══════════════════════════════════════════════════════════════════════════════
#  V69 M62 S3U — the FOURTH quality candidate, one dial: the learning rate
# ══════════════════════════════════════════════════════════════════════════════
#  Candidate 003 is `EVALUATED_NOT_ELIGIBLE`, terminal, and untouched here. Candidate
#  004 is a NEW identity: never a relabelling, a resume, a re-seed or a retrain of 003.
#
#  It exists because of a HUMAN OPERATOR RULING, not because this file recommends it.
#  The distinction matters and is kept everywhere: the sealed analysis (S3S.1 §10)
#  RANKED lower update magnitude as the best remaining hypothesis; a human then RULED
#  that candidate 004 may move that one dial. Standing generation-8 authority forbade
#  "any learning-rate, epoch, rank, alpha or dropout change", and the ruling supersedes
#  exactly the learning-rate clause, prospectively, for THIS candidate only. Epochs,
#  rank, alpha and dropout stay forbidden and stay unchanged below.
#
#  The preregistered question:
#
#      Does a SMALLER update preserve candidate 003's measured behavioural improvements
#      while reducing how far it redistributes the base model's stopping failure mode?
#
#  It is a HYPOTHESIS. `TRAINING_ROOT_CAUSE_CONFIDENCE` is `NOT_ESTABLISHED` and this
#  file predicts no outcome. S3S.1 §7.2 established body-free that BOTH observed ceiling
#  phenotypes already occur in the base model at rank 0: the adapter cured the base
#  model's two and produced six of its own. It did not invent the failure family; it
#  moved which inputs trigger it. That is what makes "how far the update moves the base
#  model" a more plausible knob than "how much capacity the adapter has" -- and it is
#  also why the risk is real in the other direction: a weaker update may weaken the
#  three security findings candidate 003 fixed. Both outcomes are informative.
RUN_ID_004 = "qwen3-06b-lora-quality-live-004"
EXPERIMENT_NAME_004 = "m62-s3u-defensive-quality-004"
#: The candidate 004 is measured AGAINST. Every dial below is inherited from it.
CANDIDATE_004_REFERENCE_KEY = "003"
#: Unchanged from candidate 003, which took it unchanged from 002. `train-v2`, the same
#: 182 promoted records, the same 154 TRAIN and 12 VALIDATION rows. S3U creates no
#: `train-v3`, adds no row, deletes none and rebalances nothing: a corpus change would
#: be a second axis, and it stays forbidden.
TRAINING_DATASET_VERSION_004 = TRAINING_DATASET_VERSION_003

#: THE axis. One field name, written once, and read by the generator, the control-plane
#: verifier and the tests rather than restated in each of them.
CANDIDATE_004_PRIMARY_AXIS = "learning_rate"
#: What the operator ruled. Not a default, not a recommendation, and not adjustable by
#: this file: `verify_single_axis` refuses any other value, including the reference's.
CANDIDATE_004_LEARNING_RATE = 5e-5

#: ``candidate`` -> the identity and material it trains on. A candidate this map does
#: not name cannot be configured, so a typo is a refusal rather than a run under an
#: authoritative-looking id.
#: ``notes`` is one of the fields ``TrainingConfig.config_hash()`` covers, so candidate
#: 001's string is reproduced here BYTE FOR BYTE. Rewording it to read more naturally
#: alongside the second candidate would silently re-identify the configuration S3H
#: actually trained under, which is the one thing this file may never do.
CANDIDATES: dict[str, dict] = {
    "001": {"run_id": RUN_ID, "experiment_name": EXPERIMENT_NAME,
            "dataset_version": TRAINING_DATASET_VERSION, "milestone": "S3G",
            "notes_prefix": "M62 S3G first quality candidate"},
    "002": {"run_id": RUN_ID_002, "experiment_name": EXPERIMENT_NAME_002,
            "dataset_version": TRAINING_DATASET_VERSION_002, "milestone": "S3J",
            "notes_prefix": "M62 S3J second quality candidate"},
    "003": {"run_id": RUN_ID_003, "experiment_name": EXPERIMENT_NAME_003,
            "dataset_version": TRAINING_DATASET_VERSION_003, "milestone": "S3O",
            "notes_prefix": "M62 S3O third quality candidate"},
    "004": {"run_id": RUN_ID_004, "experiment_name": EXPERIMENT_NAME_004,
            "dataset_version": TRAINING_DATASET_VERSION_004, "milestone": "S3U",
            "notes_prefix": "M62 S3U fourth quality candidate"},
}
DEFAULT_CANDIDATE = "001"

#: ``candidate`` -> how TRAINING renders the reasoning representation (defect D37).
#:
#: The values are SYMBOLIC because this module keeps every ``training_gym`` import
#: inside a function; :func:`candidate_reasoning_policy` resolves them against the
#: production enum.
#:
#: ``LEGACY_MODEL_DEFAULT`` is what candidates 001 and 002 ACTUALLY trained under, and
#: it is not a statement that thinking was off -- it is the implicit template default,
#: whatever the reviewed Jinja does when the keyword is not passed at all. Naming it
#: here re-identifies nothing: ``MODEL_DEFAULT`` is value-gated out of the canonical
#: config body, so both candidates keep the exact ``config_hash`` S3H and S3K sealed.
#:
#: ``TRAIN_EVAL_PARITY`` resolves to the SHARED production constant rather than to a
#: literal ``ReasoningPolicy.DISABLED``, so candidate 003's training representation
#: cannot drift away from the one evaluation generates under. They are the same object.
CANDIDATE_REASONING: dict[str, str] = {
    "001": "LEGACY_MODEL_DEFAULT",
    "002": "LEGACY_MODEL_DEFAULT",
    "003": "TRAIN_EVAL_PARITY",
}

#: S3U. Candidate 004 INHERITS candidate 003's render representation, by ASSIGNMENT from
#: the reference rather than by a second literal that happens to spell the same thing.
#: D37 is FIXED and is not reopened: re-typing `"TRAIN_EVAL_PARITY"` here would make
#: train/eval parity a value two edits could separate, and the whole point of D37's fix
#: is that they are one thing.
CANDIDATE_REASONING["004"] = CANDIDATE_REASONING[CANDIDATE_004_REFERENCE_KEY]


# ══════════════════════════════════════════════════════════════════════════════
#  The three options
# ══════════════════════════════════════════════════════════════════════════════
#  Shared across all three, and deliberately not a variable:
#
#    method                SFT_LORA         the only method with a live-proven backend
#    precision             fp32             what run-004 actually trained at; explicit
#                                           rather than auto_safe so the plan records an
#                                           input, not a host-dependent outcome
#    device                cpu              this host has no accelerator, and auto_safe
#                                           would make the plan hash depend on a probe
#    batch_size            1                run-004's shape; with a per-batch collator,
#                                           batch 1 means no padding and no wasted compute
#    max_sequence_length   512              measured p95 sequence is ~180 estimated
#                                           tokens, so 512 truncates nothing; it is a cap,
#                                           not a pad width
#    checkpoint_strategy   no               D16: epoch/steps write pickle-shaped trainer
#                                           state the adapter artefact policy refuses
#    gradient_checkpointing false           it trades ~25% more compute for memory this
#                                           host does not need for a 0.6B fp32 adapter run
#    logging               local_jsonl      the only non-phone-home target in the schema
#    download policy       deny             offline by invariant
#    validation_strategy   epoch            S3G.2: the promoted VALIDATION split (9 rows)
#                                           is handed to the trainer as an eval arm and
#                                           measured once per epoch. Three evaluations
#                                           over three epochs -- enough to see a
#                                           train/eval divergence, few enough that the
#                                           cost is negligible. It steers NOTHING
#                                           automatically: no early stopping, no
#                                           load_best_model_at_end, no checkpoint
#
#  ``optimizer`` and ``lr_scheduler`` are NOT fields of this schema. The backend passes
#  ``TrainingArguments`` without overriding either, so the installed transformers
#  defaults apply: ``adamw_torch`` and a linear decay schedule with the configured
#  warmup ratio. That is recorded here as an OBSERVATION about the backend, not as a
#  setting this file controls.
OPTIONS: dict[str, dict] = {
    "A": {
        "label": "conservative",
        "rationale":
            "smallest change that could show a signal: half the adapter capacity of B "
            "and two passes. Cheapest to run and the least likely to overfit 107 rows; "
            "also the most likely to move nothing measurable.",
        "lora_rank": 8, "lora_alpha": 16, "lora_dropout": 0.05,
        "learning_rate": 1e-4, "weight_decay": 0.0, "warmup_ratio": 0.1,
        "epochs": 2, "max_steps": 27, "gradient_accumulation_steps": 8,
        "overfitting_risk": "low",
        "signal_expectation": "weak; may not move any gate",
    },
    "B": {
        "label": "recommended",
        "rationale":
            "enough adapter capacity to change formatting and refusal phrasing, enough "
            "passes to converge on 107 rows, and a runtime that fits inside one local "
            "session. Rank 16 over the seven Qwen3 linear projections is ~10M trainable "
            "parameters against 601M frozen — large enough to learn a response style, "
            "far too small to learn new knowledge, which is exactly the intended scope.",
        "lora_rank": 16, "lora_alpha": 32, "lora_dropout": 0.05,
        "learning_rate": 2e-4, "weight_decay": 0.0, "warmup_ratio": 0.1,
        "epochs": 3, "max_steps": 40, "gradient_accumulation_steps": 8,
        "overfitting_risk": "moderate and monitored by the validation split",
        "signal_expectation":
            "the format and refusal-phrasing gates are reachable; the security gates "
            "are veto conditions, not targets",
    },
    "C": {
        "label": "aggressive",
        "rationale":
            "double the rank again and six passes. On a 107-row corpus this is the "
            "option most likely to memorise the corpus rather than learn from it, which "
            "would show up as a good validation loss and a worse held-out result — the "
            "failure mode that is hardest to detect and easiest to believe.",
        "lora_rank": 32, "lora_alpha": 64, "lora_dropout": 0.1,
        "learning_rate": 2e-4, "weight_decay": 0.01, "warmup_ratio": 0.1,
        "epochs": 6, "max_steps": 80, "gradient_accumulation_steps": 8,
        "overfitting_risk": "high on a corpus this size",
        "signal_expectation":
            "largest movement, least trustworthy attribution",
    },
    # ── V69 M62 S3J: the second candidate's option ────────────────────────────
    "S3J": {
        "label": "gentler, wider curriculum",
        "rationale":
            "Option B is held constant everywhere it can be, because comparability "
            "between the two candidates is worth more than a speculative improvement: "
            "same rank 16, same alpha 32, same dropout 0.05, same seven projections, "
            "same fp32/CPU, same seed 42, same batch 1 x 8. Two things move, and only "
            "two. (1) The learning rate halves, 2e-4 -> 1e-4. Candidate 001 proved the "
            "corpus can move behaviour at 2e-4 -- required refusal went 1/12 -> 9/12 -- "
            "and it also drifted far enough to break a structured-output contract the "
            "BASE model already satisfied 9/9. A smaller step is the least speculative "
            "way to keep the first effect while reducing the second. (2) Passes fall "
            "from ~3 to exactly 2, because the corpus grew from 107 to 154 TRAIN rows: "
            "the model now sees MORE distinct examples in FEWER passes, which is the "
            "shape that trades memorisation for coverage. Optimizer steps land at 40, "
            "the same bounded budget S3H actually ran, so the compute class is known "
            "rather than estimated.",
        "lora_rank": 16, "lora_alpha": 32, "lora_dropout": 0.05,
        "learning_rate": 1e-4, "weight_decay": 0.0, "warmup_ratio": 0.1,
        "epochs": 2, "max_steps": 40, "gradient_accumulation_steps": 8,
        "overfitting_risk":
            "lower than 001: 44% more rows, one third fewer passes, half the step size",
        "signal_expectation":
            "the over-refusal and structured-output gates are the ones this is aimed "
            "at; the security vetoes are conditions to HOLD, never targets to move",
    },
}

RECOMMENDED_OPTION = "B"

#: The option each candidate uses. Kept separate from :data:`RECOMMENDED_OPTION` so the
#: S3G recommendation is not silently restated as the S3J one.
#:
#: Candidate 003 REUSES candidate 002's option verbatim. That is the whole controlled
#: design: it is not a new option whose numbers happen to match, because a copy is a
#: thing that can drift. Sharing the key makes "every hyperparameter is identical" a
#: structural property one equality can assert, rather than eight comparisons that
#: could each be edited independently.
CANDIDATE_OPTION: dict[str, str] = {"001": "B", "002": "S3J", "003": "S3J"}

# ── V69 M62 S3U: the fourth candidate's option, DERIVED and not copied ────────────────
#: The option candidate 004's is built FROM. Candidate 003 shares candidate 002's option
#: key by reference, so `S3J` is literally the dial set that trained the reference
#: adapter -- not a transcription of it.
S3U_REFERENCE_OPTION = CANDIDATE_OPTION[CANDIDATE_004_REFERENCE_KEY]

#: The dials an option controls. Every one of them is a hyperparameter a candidate could
#: differ on, and the single-axis proof is a set difference over exactly this tuple. The
#: remaining keys of an option -- `label`, `rationale`, `overfitting_risk`,
#: `signal_expectation` -- are prose about the dials and are deliberately excluded: they
#: describe an experiment, they do not configure one.
OPTION_DIALS = (
    "lora_rank", "lora_alpha", "lora_dropout", "learning_rate", "weight_decay",
    "warmup_ratio", "epochs", "max_steps", "gradient_accumulation_steps",
)

#: Candidate 004's dials, DERIVED from the reference by dictionary expansion.
#:
#: This construction is the single-axis guarantee, not a comment claiming one. Rank,
#: alpha, dropout, weight decay, warmup, epochs, optimizer steps and gradient
#: accumulation are not re-typed here, so there is no second place for them to drift to
#: and no edit that can move one by accident. Exactly one key is overridden, and
#: `verify_single_axis` refuses the configuration if that stops being true.
#:
#: alpha is deliberately NOT slaved. Unlike the rank hypothesis, which would have needed
#: alpha moved with it to hold `alpha/r` constant, a learning-rate change requires no
#: compensating adjustment: `alpha/r` stays 32/16 = 2.0 because neither term moves. A
#: "compensating" second dial would be a second axis wearing a justification.
OPTIONS["S3U"] = {
    **OPTIONS[S3U_REFERENCE_OPTION],
    "label": "reduced update magnitude",
    "rationale":
        "Candidate 003's dials, with the learning rate halved and nothing else touched. "
        "The reference improved task success 24/36 -> 25/36 and reward 0.5461 -> 0.5903 "
        "against its own simultaneously-measured baseline and fixed three security "
        "findings, while producing six output ceilings where the baseline produced two. "
        "S3S.1 established body-free that both observed ceiling phenotypes already exist "
        "in the base model at rank 0, so the adapter redistributed a pre-existing failure "
        "mode rather than inventing one. The knob that governs how far a fitted adapter "
        "moves the base model is the size of the update, which is the one dial never "
        "varied across three candidates. Halving it is the smallest step that produces a "
        "dose-response reading at all: 2e-4 (001) -> 1e-4 (002, 003) -> 5e-5 gives three "
        "points on one line, and the two existing points are already measured.",
    CANDIDATE_004_PRIMARY_AXIS: CANDIDATE_004_LEARNING_RATE,
    "overfitting_risk":
        "lower than candidate 003 on the same corpus and the same two passes; the "
        "opposite risk is the live one -- an update too small to retain the reference's "
        "measured safety and quality gains",
    "signal_expectation":
        "UNKNOWN and deliberately unpredicted. Retaining the reference's improvements "
        "with fewer ceilings, losing the improvements, and moving nothing measurable are "
        "all live outcomes, and all three are informative about the same hypothesis",
}

#: Candidate 004 is the ONLY user of `S3U`. Candidates 001-003 keep the option keys they
#: were measured under, so adding this one re-identifies nothing.
CANDIDATE_OPTION["004"] = "S3U"

#: ``candidate -> (reference candidate, the dials that are ALLOWED to differ)``.
#:
#: A candidate that declares a single-axis relation has its claim CHECKED rather than
#: believed -- by `verify_single_axis` here, and again by the control-plane verifier
#: against the same declaration. Candidates 001 and 002 declare none: 001 is the first
#: quality candidate and has no in-lineage reference, and 002 moved several dials at
#: once against 001, which its own milestone recorded openly.
#:
#: An EMPTY dial set is the strongest form and means the option is shared BY KEY: that
#: is candidate 003, whose axis was the render policy and whose dials were required to be
#: the very same object as its control's.
CANDIDATE_003_REFERENCE_KEY = "002"
CANDIDATE_SINGLE_AXIS: dict[str, tuple[str, frozenset[str]]] = {
    "003": (CANDIDATE_003_REFERENCE_KEY, frozenset()),
    "004": (CANDIDATE_004_REFERENCE_KEY, frozenset({CANDIDATE_004_PRIMARY_AXIS})),
}

#: The learning rate each candidate that declares a learning-rate axis is PINNED to by
#: operator ruling. A candidate whose axis is the learning rate may not pick its own
#: value: the value is the ruling.
RULED_LEARNING_RATE: dict[str, float] = {"004": CANDIDATE_004_LEARNING_RATE}

# ══════════════════════════════════════════════════════════════════════════════
#  V69 M63 S4B — the FIFTH quality candidate, the same dial, one step further
# ══════════════════════════════════════════════════════════════════════════════
#  Candidate 004 is `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` and the human decision on it
#  is HOLD. Both facts stay exactly as they are: candidate 005 does not supersede 004,
#  does not promote it, does not re-open its measurement and does not reinterpret its
#  eligibility. A held candidate is retained evidence, and this is a NEW identity built
#  against it, never a second attempt at it.
#
#  It exists because of a HUMAN OPERATOR RULING, not because this file recommends it.
#  The ruling names one axis, one reference and one value; the generator's job is to
#  refuse anything else, which `verify_single_axis` does below.
#
#  The preregistered question:
#
#      Holding candidate 004's reviewed training configuration constant, what artefact
#      results from reducing the learning rate 5e-5 -> 2.5e-5 for exactly one controlled
#      training experiment?
#
#  WHAT THIS FILE DOES NOT CLAIM, and the distinction is load-bearing:
#
#    * that 2.5e-5 is better than 5e-5. It is a third point on the dose-response line
#      2e-4 -> 1e-4 -> 5e-5, and "moves nothing", "keeps the gains" and "loses the gains"
#      are all live outcomes.
#    * that training is the indicated remedy at all. The repository's standing body-free
#      conclusion is `RECOMMENDED_REMEDY = TOOLING`, and this candidate is
#      TRAINING_EXPERIMENTALLY_ALLOWED_NOT_PROVEN_NECESSARY. Both statements remain true
#      and neither is weakened by an experiment being permitted.
#
#  Written as assignments against the existing tables rather than as new literals inside
#  them, exactly as S3U wrote candidate 004's option and reasoning policy: a value that
#  exists in one place cannot drift from itself, and the whole experiment then reads as
#  one auditable block instead of six edits scattered through the file.
RUN_ID_005 = "qwen3-06b-lora-quality-live-005"
EXPERIMENT_NAME_005 = "m62-s4b-defensive-quality-005"
#: The candidate 005 is measured AGAINST. Every dial below is inherited from it.
CANDIDATE_005_REFERENCE_KEY = "004"
#: Unchanged from candidate 004, which took it unchanged from 003, which took it
#: unchanged from 002. `train-v2`, the same 182 promoted records, the same 154 TRAIN and
#: 12 VALIDATION rows. S4B creates no `train-v3`, adds no row, deletes none, reorders
#: none and rebalances nothing: a corpus change would be a second axis.
TRAINING_DATASET_VERSION_005 = TRAINING_DATASET_VERSION_004

#: THE axis. The same field name candidate 004 moved, moved again -- which is what makes
#: this a dose-response reading rather than a new hypothesis.
CANDIDATE_005_PRIMARY_AXIS = CANDIDATE_004_PRIMARY_AXIS
#: What the operator ruled. Not a default, not a recommendation, not halving-by-habit,
#: and not adjustable by this file: `verify_single_axis` refuses any other value.
CANDIDATE_005_LEARNING_RATE = 2.5e-5

CANDIDATES["005"] = {
    "run_id": RUN_ID_005, "experiment_name": EXPERIMENT_NAME_005,
    "dataset_version": TRAINING_DATASET_VERSION_005, "milestone": "S4B",
    "notes_prefix": "M63 S4B fifth quality candidate"}

#: Candidate 005 INHERITS candidate 004's render representation by ASSIGNMENT from the
#: reference, which inherited it the same way from candidate 003. D37 is FIXED and is not
#: reopened; re-typing the symbol here would give train/eval parity a second place to
#: drift from.
CANDIDATE_REASONING["005"] = CANDIDATE_REASONING[CANDIDATE_005_REFERENCE_KEY]

#: The option candidate 005's is built FROM -- `S3U`, the dial set that actually trained
#: the reference adapter, resolved through the table rather than named as a literal.
S4B_REFERENCE_OPTION = CANDIDATE_OPTION[CANDIDATE_005_REFERENCE_KEY]

#: Candidate 005's dials, DERIVED from the reference by dictionary expansion.
#:
#: This construction is the single-axis guarantee, not a comment claiming one. Rank,
#: alpha, dropout, weight decay, warmup, epochs, optimizer steps and gradient
#: accumulation are not re-typed, so there is no second place for them to drift to.
#: Exactly one key is overridden.
#:
#: alpha is deliberately NOT slaved, for the reason S3U gave and which has not changed: a
#: learning-rate change needs no compensating adjustment, `alpha/r` stays 32/16 = 2.0
#: because neither term moves, and a "compensating" second dial would be a second axis
#: wearing a justification.
OPTIONS["S4B"] = {
    **OPTIONS[S4B_REFERENCE_OPTION],
    "label": "further reduced update magnitude",
    "rationale":
        "Candidate 004's dials, with the learning rate halved again and nothing else "
        "touched. 2e-4 (001) -> 1e-4 (002, 003) -> 5e-5 (004) -> 2.5e-5 (005) is a "
        "four-point dose-response line on the one dial the lineage has varied, and the "
        "first three points are already measured. The operator ruled this value; this "
        "file did not choose it and may not change it.",
    CANDIDATE_005_PRIMARY_AXIS: CANDIDATE_005_LEARNING_RATE,
    "overfitting_risk":
        "lower again than candidate 004 on the same corpus and the same two passes; the "
        "opposite risk is the live one -- an update too small to move anything at all",
    "signal_expectation":
        "UNKNOWN and deliberately unpredicted. This candidate is "
        "TRAINING_EXPERIMENTALLY_ALLOWED_NOT_PROVEN_NECESSARY: the standing body-free "
        "conclusion remains RECOMMENDED_REMEDY = TOOLING, and a permitted experiment is "
        "not an indicated one",
}

#: Candidate 005 is the ONLY user of `S4B`. Candidates 001-004 keep the option keys they
#: were measured under, so adding this one re-identifies nothing.
CANDIDATE_OPTION["005"] = "S4B"

#: One axis, declared and therefore checkable.
CANDIDATE_SINGLE_AXIS["005"] = (
    CANDIDATE_005_REFERENCE_KEY, frozenset({CANDIDATE_005_PRIMARY_AXIS}))

#: Pinned by operator ruling. A candidate whose axis is the learning rate may not pick
#: its own value: the value is the ruling.
RULED_LEARNING_RATE["005"] = CANDIDATE_005_LEARNING_RATE


#: How many fractional mantissa digits the prose notation may spend. ONE, and the bound
#: is the point: `1e-4` and `5e-5` still render exactly as they always did, `2.5e-5`
#: becomes representable, and `1.25e-4` is still a refusal. S4B widened this from a
#: hard-coded zero because the operator ruled candidate 005 to 2.5e-5, a rate the old
#: notation could not print -- it rounded to `3e-5` and then correctly refused its own
#: output. Widening the NOTATION is not widening the science: no rate this function can
#: now render is a rate any candidate may be configured at, which stays the exclusive
#: business of `RULED_LEARNING_RATE` and `verify_single_axis`.
MAX_LEARNING_RATE_MANTISSA_DIGITS = 1


def format_learning_rate(value: float) -> str:
    """``1e-4``, ``5e-5``, ``2.5e-5`` — how this repository writes a rate in prose.

    Derived from the number, never typed beside it, so a document or a control-plane
    string that quotes a rate cannot drift away from the rate the generator configures.
    Round-tripping is checked: a value this notation cannot represent exactly is a
    refusal rather than a rounded-off claim.

    The SHORTEST faithful rendering wins. Precision is tried from zero fractional digits
    upward and the first one that round-trips is returned, so every rate that rendered
    before this function grew a second precision renders byte-identically now: `1e-4` is
    still `1e-4`, and no sealed prose, document or control-plane string moves.
    """
    for digits in range(MAX_LEARNING_RATE_MANTISSA_DIGITS + 1):
        mantissa, exponent = f"{value:.{digits}e}".split("e")
        text = f"{mantissa}e{int(exponent)}"
        if float(text) == float(value):
            return text
    raise ValueError(
        f"learning rate {value!r} does not round-trip through this notation at "
        f"{MAX_LEARNING_RATE_MANTISSA_DIGITS} fractional digit(s); refusing to print a "
        f"rate that is not the rate")


def single_axis_diff(candidate: str) -> frozenset[str]:
    """The option dials that differ between *candidate* and its declared reference.

    Compared over :data:`OPTION_DIALS` only, and by value. A dial the reference declares
    and *candidate* does not (or the reverse) counts as a difference rather than being
    skipped, so deleting a key is not a way to make a diff smaller.
    """
    try:
        reference_key, _ = CANDIDATE_SINGLE_AXIS[candidate]
    except KeyError:
        raise ValueError(
            f"candidate {candidate!r} declares no single-axis relation, so there is "
            f"nothing to diff it against") from None
    mine = OPTIONS[CANDIDATE_OPTION[candidate]]
    theirs = OPTIONS[CANDIDATE_OPTION[reference_key]]
    sentinel = object()
    return frozenset(dial for dial in OPTION_DIALS
                     if mine.get(dial, sentinel) != theirs.get(dial, sentinel))


def verify_single_axis(candidate: str) -> None:
    """Refuse a candidate whose option moved anything but its declared single axis.

    Called by :func:`build_config`, so a second dial cannot reach a configuration
    document, a plan, a plan hash or a training run: the refusal happens while the
    configuration is being built, which is the last moment it is cheap.

    Three separate ways a learning-rate experiment can be worthless, each refused here:

      * **A second dial moved.** Then the measurement cannot attribute anything, and a
        fresh holdout would be spent producing an uninterpretable fourth run.
      * **The axis did not move.** A candidate whose learning rate equals its
        reference's is not an experiment, it is a re-run under a new name.
      * **The axis moved somewhere the operator did not rule.** The value 5e-5 is a human
        decision recorded at a control-plane generation, not a knob this file may turn.
    """
    if candidate not in CANDIDATE_SINGLE_AXIS:
        return
    reference_key, declared = CANDIDATE_SINGLE_AXIS[candidate]

    # The RULED value first. Checked before the dial diff on purpose: an axis set back
    # to its reference's value produces an EMPTY diff, so a diff-first order would
    # report "no axis moved" for a candidate whose real defect is that it tests nothing,
    # and the clearer refusal would be unreachable.
    ruled = RULED_LEARNING_RATE.get(candidate)
    if ruled is not None:
        mine = OPTIONS[CANDIDATE_OPTION[candidate]]["learning_rate"]
        theirs = OPTIONS[CANDIDATE_OPTION[reference_key]]["learning_rate"]
        if mine == theirs:
            raise ValueError(
                f"candidate {candidate!r} carries its reference's learning rate "
                f"{format_learning_rate(theirs)}, so it tests nothing. A candidate that "
                f"moves no dial is a re-run under a new identity, not an experiment")
        if mine != ruled:
            # `repr` for the offending value, not the prose formatter: an unruled rate
            # is exactly the kind of number that formatter refuses to render, and a
            # refusal that raises while composing its own message is a crash, not a
            # refusal.
            raise ValueError(
                f"candidate {candidate!r} is configured at learning rate {mine!r}; the "
                f"operator ruling for it is {format_learning_rate(ruled)}. The value is "
                f"a recorded human decision and this generator may not choose a "
                f"different one")

    moved = single_axis_diff(candidate)
    if moved != declared:
        raise ValueError(
            f"candidate {candidate!r} moves {sorted(moved) or 'no'} dial(s) against "
            f"candidate {reference_key!r}, but its declared single axis is "
            f"{sorted(declared) or 'none (the option is shared by key)'}. A second "
            f"experimental axis produces a measurement nothing can attribute; refusing "
            f"to build it")
    if not declared and CANDIDATE_OPTION[candidate] != CANDIDATE_OPTION[reference_key]:
        raise ValueError(
            f"candidate {candidate!r} declares its option shared with candidate "
            f"{reference_key!r}, but the two name different options "
            f"({CANDIDATE_OPTION[candidate]!r} vs {CANDIDATE_OPTION[reference_key]!r}). "
            f"Dials that agree today are not dials that are the same")

#: Measured on this host in S3G, from the promoted corpus:
#: 107 TRAIN rows, mean sequence ~470 characters (~105-135 estimated tokens plus the
#: chat template's ~30). Cost model: a LoRA forward+backward is ~4 x parameters x tokens
#: floating-point operations, and this CPU sustains an estimated 40-100 GFLOP/s in fp32.
#: Calibrated against the ONE datapoint that exists: run-004's 32 micro-batches of ~51
#: tokens completed in the "minutes" category, which the same model predicts as 40-100s.
#: These are RANGES derived from a model, not measurements of a run that happened.
RUNTIME_SECONDS_PER_MICROBATCH = (3.5, 9.0)

#: What an operator should refuse to let a first candidate exceed. Not enforced here —
#: nothing here runs — and deliberately well above the estimate, so it catches a wrong
#: cost model rather than ordinary variance.
HARD_RUNTIME_CEILING_HOURS = 4


def compute_budget(option: str, train_rows: int) -> dict:
    """Micro-batch count, realised epochs and an estimated runtime RANGE."""
    spec = OPTIONS[option]
    micro_batches = spec["max_steps"] * spec["gradient_accumulation_steps"]
    low, high = RUNTIME_SECONDS_PER_MICROBATCH
    return {
        "optimizer_steps": spec["max_steps"],
        "effective_batch_size": spec["gradient_accumulation_steps"] * 1,
        "micro_batches": micro_batches,
        "realised_epochs": round(micro_batches / max(1, train_rows), 2),
        "estimated_runtime_minutes": [round(micro_batches * low / 60),
                                      round(micro_batches * high / 60)],
        "estimate_basis":
            "~4 x params x tokens FLOPs per micro-batch at 40-100 GFLOP/s fp32 CPU; "
            "calibrated against run-004's recorded duration category, not measured",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  The dataset reference, re-derived from the promoted artefacts
# ══════════════════════════════════════════════════════════════════════════════
def candidate_spec(candidate: str) -> dict:
    """The identity block for one candidate, or a refusal naming the ones that exist."""
    try:
        return CANDIDATES[candidate]
    except KeyError:
        raise ValueError(
            f"unknown candidate {candidate!r}; this generator configures "
            f"{sorted(CANDIDATES)}. A new candidate needs a new identity and its own "
            f"corpus version, not a new label on an existing run") from None


def candidate_reasoning_policy(candidate: str):
    """The reasoning representation TRAINING renders under, for one candidate (D37).

    Resolved against the production enum rather than stored as one, so this module
    keeps its lazy-import discipline. An unrecognised symbol is a refusal: a candidate
    whose render policy cannot be resolved must not fall back to a default, because the
    default is precisely the legacy behaviour candidate 003 exists to move away from.
    """
    from training_gym.training.chat_render import (
        TRAIN_EVAL_PARITY_REASONING_POLICY,
        ReasoningPolicy,
    )

    symbol = CANDIDATE_REASONING[candidate]
    if symbol == "TRAIN_EVAL_PARITY":
        return TRAIN_EVAL_PARITY_REASONING_POLICY
    if symbol == "LEGACY_MODEL_DEFAULT":
        return ReasoningPolicy.MODEL_DEFAULT
    raise ValueError(
        f"candidate {candidate!r} names reasoning symbol {symbol!r}, which this "
        f"generator cannot resolve. Refusing rather than defaulting: silently falling "
        f"back to MODEL_DEFAULT would train the legacy representation under a "
        f"configuration that claims otherwise")


def dataset_reference(root: Path, dataset_version: str = TRAINING_DATASET_VERSION):
    """Fill every digest from the artefacts on disk. Nothing here is hand-copied.

    ``load_manifest`` is the only loader used: it re-hashes every shard from disk,
    refuses symlinks and hard links, and re-derives each record's digest. A reference
    assembled from a weaker reading of the same files would bind a claim rather than a
    fact.
    """
    from training_gym.datasets.candidate import DatasetSplit
    from training_gym.datasets.export import verify_sft_export
    from training_gym.datasets.manifests import load_manifest
    from training_gym.training.dataset_reference import (
        ExportFormat,
        TrainingDatasetReference,
        split_digests,
    )

    manifest = load_manifest(root=root, dataset_id=TRAINING_DATASET_ID,
                            dataset_version=dataset_version)
    export = verify_sft_export(out_root=root, dataset_id=TRAINING_DATASET_ID,
                               dataset_version=dataset_version)
    if not export.ok or export.manifest is None:
        raise RuntimeError(f"the SFT export does not verify: {export.to_dict()}")

    train = split_digests(manifest, DatasetSplit.TRAIN)
    validation = split_digests(manifest, DatasetSplit.VALIDATION)
    hidden = split_digests(manifest, DatasetSplit.HIDDEN_EVALUATION)
    security = split_digests(manifest, DatasetSplit.SECURITY_REGRESSION)
    for name, value in (("train", train), ("validation", validation),
                        ("hidden_evaluation", hidden),
                        ("security_regression", security)):
        if value is None:
            raise RuntimeError(f"the promoted version declares no {name} shard")

    return TrainingDatasetReference(
        dataset_id=TRAINING_DATASET_ID, dataset_version=dataset_version,
        dataset_manifest_hash=manifest.manifest_hash(),
        export_format=ExportFormat.SFT_JSONL,
        export_manifest_hash=export.manifest.export_hash(),
        source_dataset_content_hash=export.manifest.row_hashes_hash,
        train_split_manifest_hash=train[0], train_shard_hash=train[1],
        validation_split_manifest_hash=validation[0],
        validation_shard_hash=validation[1],
        hidden_evaluation_manifest_hash=hidden[0],
        security_regression_manifest_hash=security[0],
        record_count=manifest.total_records,
        dataset_schema_version=manifest.dataset_manifest_version)


def validation_export(root: Path,
                      dataset_version: str = TRAINING_DATASET_VERSION):
    """The verified VALIDATION export's manifest, or a refusal.

    Checked here because the configuration this script emits sets
    ``validation_strategy=epoch``, and a config that asks for train-time validation
    against an export that does not verify is a config whose plan would be refused at
    preflight. Better to fail while producing the document than to hand over one that
    cannot be executed.
    """
    from training_gym.datasets.export import verify_sft_validation_export

    verified = verify_sft_validation_export(
        out_root=root, dataset_id=TRAINING_DATASET_ID,
        dataset_version=dataset_version)
    if not verified.ok or verified.manifest is None:
        raise RuntimeError(
            f"the validation export does not verify: {verified.to_dict()}. Rebuild the "
            f"corpus with build_training_corpus.py --root <the same dataset root>; the "
            f"build is deterministic and must reproduce the promoted manifest hash")
    return verified.manifest


def build_config(option: str, *, dataset_root: Path, output_root: Path,
                 candidate: str = DEFAULT_CANDIDATE):
    """One complete, strictly-validated :class:`TrainingConfig`.

    *candidate* selects the identity and the corpus version. It defaults to ``001`` so
    every S3G/S3G.1/S3G.2 caller keeps the exact configuration — and the exact
    ``config_hash`` — it had before the second candidate existed.
    """
    from training_gym.training.config import (
        CheckpointStrategy,
        DependencyProfile,
        DevicePolicy,
        LoggingTarget,
        LoRABias,
        LoRAConfig,
        LoRATargetPolicy,
        ModelDownloadPolicy,
        PrecisionPolicy,
        TrainingConfig,
        TrainingMethod,
        ValidationStrategy,
    )
    from training_gym.training.plan import output_root_id

    spec = OPTIONS[option]
    identity = candidate_spec(candidate)
    # S3U. The single-axis claim is checked HERE, at the last cheap moment: before a
    # configuration document exists, before a plan hash is derived from one and long
    # before a token could be spent on it. Candidates that declare no single-axis
    # relation pass through untouched, so 001's and 002's frozen `config_hash` values
    # are unaffected by the existence of this call.
    #
    # Scoped to the candidate's OWN option, and the limit is stated rather than hidden:
    # `--option` and `--all-options` deliberately build a candidate under other options
    # to compare budgets and config hashes, and refusing those would remove a working
    # comparison tool to guard a path nothing can train from. The configuration a plan,
    # a token or a run can ever be derived for is the one built under
    # `CANDIDATE_OPTION[candidate]`, and that one is checked every time.
    if option == CANDIDATE_OPTION.get(candidate):
        verify_single_axis(candidate)
    return TrainingConfig(
        run_id=identity["run_id"], experiment_name=identity["experiment_name"],
        method=TrainingMethod.SFT_LORA,
        base_model_id=BASE_MODEL_ID, base_model_revision=BASE_MODEL_REVISION,
        base_model_parameters_b=BASE_MODEL_PARAMETERS_B,
        tokenizer_id=BASE_MODEL_ID, tokenizer_revision=BASE_MODEL_REVISION,
        dataset_reference=dataset_reference(dataset_root,
                                            identity["dataset_version"]),
        output_root_id=output_root_id(output_root),
        created_at_utc=NOW,
        seed=42,
        epochs=spec["epochs"], max_steps=spec["max_steps"],
        batch_size=1,
        gradient_accumulation_steps=spec["gradient_accumulation_steps"],
        learning_rate=spec["learning_rate"], weight_decay=spec["weight_decay"],
        warmup_ratio=spec["warmup_ratio"],
        max_sequence_length=512,
        # Off deliberately: recomputing activations costs roughly a quarter more compute
        # to save memory that a 0.6B fp32 adapter run on this host does not need.
        gradient_checkpointing=False,
        precision_policy=PrecisionPolicy.FP32,
        device_policy=DevicePolicy.CPU,
        lora=LoRAConfig(
            rank=spec["lora_rank"], alpha=spec["lora_alpha"],
            dropout=spec["lora_dropout"], bias=LoRABias.NONE,
            # Explicit, not the ``all-linear`` sentinel. run-004 established what the
            # sentinel resolves to on this model — the seven projections below, 392 LoRA
            # tensors over 28 layers — so naming them makes the adapted module set an
            # INPUT to the plan rather than an outcome discovered after training, and
            # D15's exact-match rule then has something real to check.
            target_policy=LoRATargetPolicy.ATTENTION_AND_MLP,
            task_type="CAUSAL_LM"),
        dataloader_workers=0,
        checkpoint_strategy=CheckpointStrategy.NO,
        max_checkpoints=1,
        # S3G.2. Once per epoch, over the 9 promoted VALIDATION rows, for visibility into
        # eval_loss and train/eval divergence. `epoch` rather than `steps` because 40
        # optimizer steps over 9 rows would re-measure the same rows 40 times and produce
        # a curve made of sampling noise. It is DIAGNOSTIC: the held-out eligibility
        # corpus is still `m62-defensive-eval v2`, post-training and separately
        # authorised, and no gate in the S3G acceptance set reads this number.
        validation_strategy=ValidationStrategy.EPOCH,
        logging_target=LoggingTarget.LOCAL_JSONL, logging_interval_steps=5,
        model_download_policy=ModelDownloadPolicy.DENY,
        dependency_profile=DependencyProfile.TRAINING,
        trust_remote_code=False,
        base_model_family="qwen3",
        # D37. The ONE primary experimental axis between candidate 002 and candidate
        # 003, and the only field that separates them behaviourally. It is value-gated
        # out of the canonical body when it is MODEL_DEFAULT, so passing it explicitly
        # here leaves candidates 001 and 002 hashing exactly as they always did.
        reasoning_policy=candidate_reasoning_policy(candidate),
        notes=(f"{identity['notes_prefix']}, option {option} "
               f"({spec['label']}). RUN_INTENT={RUN_INTENT}. Not run-004; nothing is "
               f"resumed, continued or promoted."))


def plan_for(option: str, *, dataset_root: Path, output_root: Path,
             model_cache_root: Path | None, candidate: str = DEFAULT_CANDIDATE):
    """The dry run. Creates nothing, imports no training framework, spends nothing."""
    from training_gym.training.planner import plan_training

    config = build_config(option, dataset_root=dataset_root, output_root=output_root,
                          candidate=candidate)
    return config, plan_training(config, dataset_root=dataset_root,
                                 output_root=output_root,
                                 model_cache_root=model_cache_root,
                                 export_root=dataset_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build an M62 quality candidate's training configuration and its "
                    "dry-run plan. Never trains and never spends a token.")
    parser.add_argument("--dataset-root", required=True,
                        help="root holding the promoted training corpus and its export")
    parser.add_argument("--output-root", required=True,
                        help="where a FUTURE run would write; nothing is created here")
    parser.add_argument("--model-cache-root", default="",
                        help="the reviewed model cache; omitted means the plan reports "
                             "the weights as unverified rather than guessing a location")
    parser.add_argument("--candidate", default=DEFAULT_CANDIDATE,
                        choices=sorted(CANDIDATES),
                        help="001 is the frozen S3G candidate (corpus v1); 002 is the "
                             "S3J candidate (corpus v2); 003 is the S3O candidate "
                             "(corpus v2, same option as 002, reasoning policy DISABLED "
                             "-- the single controlled axis); 004 is the S3U candidate "
                             "(corpus v2, candidate 003's dials with the learning rate "
                             "as the single axis); 005 is the S4B candidate (corpus v2, "
                             "candidate 004's dials with the learning rate as the single "
                             "axis, ruled to 2.5e-5). Defaults to 001 so every existing "
                             "caller is unchanged")
    parser.add_argument("--option", default="", choices=["", *sorted(OPTIONS)],
                        help="defaults to the option the chosen candidate declares: "
                             f"{CANDIDATE_OPTION}")
    parser.add_argument("--all-options", action="store_true",
                        help="summarise every option's hyperparameters and budget")
    parser.add_argument("--emit", default="",
                        help="write the configuration document to this path; it is a "
                             "runtime artefact and must stay outside the repository")
    parser.add_argument("--plan", action="store_true",
                        help="produce the dry-run plan and report its blockers")
    args = parser.parse_args(argv)

    try:
        dataset_root = Path(args.dataset_root)
        output_root = Path(args.output_root)
        cache_root = Path(args.model_cache_root) if args.model_cache_root else None
        identity = candidate_spec(args.candidate)
        dataset_version = identity["dataset_version"]
        option = args.option or CANDIDATE_OPTION[args.candidate]

        if args.all_options:
            reference = dataset_reference(dataset_root, dataset_version)
            train_rows = 0
            from training_gym.datasets.candidate import DatasetSplit
            from training_gym.datasets.manifests import load_manifest
            manifest = load_manifest(root=dataset_root, dataset_id=TRAINING_DATASET_ID,
                                     dataset_version=dataset_version)
            train_rows = manifest.counts().get(DatasetSplit.TRAIN.value, 0)
            payload = {
                "candidate": args.candidate,
                "dataset_version": dataset_version,
                "recommended_option": CANDIDATE_OPTION[args.candidate],
                "train_rows": train_rows,
                "hard_runtime_ceiling_hours": HARD_RUNTIME_CEILING_HOURS,
                "dataset_reference_hash": reference.reference_hash(),
                "options": {
                    name: {**{k: v for k, v in spec.items()},
                           "config_hash": build_config(
                               name, dataset_root=dataset_root,
                               output_root=output_root,
                               candidate=args.candidate).config_hash(),
                           "budget": compute_budget(name, train_rows)}
                    for name, spec in sorted(OPTIONS.items())},
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        config = build_config(option, dataset_root=dataset_root,
                              output_root=output_root, candidate=args.candidate)
        payload: dict = {
            "candidate": args.candidate, "option": option,
            "run_id": identity["run_id"],
            "training_dataset_version": dataset_version,
            "run_intent": RUN_INTENT,
            "config_hash": config.config_hash(),
            "dataset_reference_hash": config.dataset_reference.reference_hash(),
            "effective_batch_size": config.effective_batch_size,
            "validation_strategy": config.validation_strategy.value,
            "validation_split": config.validation_split.value,
            "train_time_validation_enabled": config.train_time_validation_enabled,
            "early_stopping": False,
            "load_best_model_at_end": False,
            "checkpoint_strategy": config.checkpoint_strategy.value,
            "trained_anything": False, "wrote_adapter": False,
        }
        if config.train_time_validation_enabled:
            manifest = validation_export(dataset_root, dataset_version)
            payload.update({
                "validation_export_rows": manifest.record_count,
                "validation_export_hash": manifest.export_hash(),
                "validation_export_file_sha256": manifest.sha256_file,
                "validation_export_source_split": manifest.source_split,
            })

        if args.emit:
            destination = Path(args.emit)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                json.dumps(config.to_dict(), indent=2, sort_keys=True),
                encoding="utf-8")
            payload["config_written"] = destination.name

        if args.plan:
            _config, result = plan_for(option, dataset_root=dataset_root,
                                       output_root=output_root,
                                       model_cache_root=cache_root,
                                       candidate=args.candidate)
            plan = result.plan
            payload.update({
                "plan_hash": plan.plan_hash(),
                "plan_is_executable": plan.is_executable,
                "plan_blockers": list(plan.blockers),
                "plan_warnings": list(plan.warnings),
                "feasibility_verdict": result.feasibility.verdict.value,
                "selected_device": plan.selected_device,
                "selected_precision": plan.selected_precision,
                "dataset_evidence": result.dataset.evidence.value,
                "dataset_problems": list(result.dataset.problems),
                "dataset_missing": list(result.dataset.missing),
                "dependency_ready": result.dependencies.ready,
                "dependency_blockers": list(result.dependencies.blockers()),
                "model_cache_status": result.identity.cache_status.value,
                "memory_peak_estimate_gb": result.feasibility.memory.estimated_peak_gb,
                "adapter_estimate_gb": result.feasibility.disk.adapter_gb,
                "disk_estimate_gb": result.feasibility.disk.estimated_total_gb,
                "disk_required_free_gb": result.feasibility.disk.required_free_gb,
                # The token is DERIVED from the plan hash above by whoever holds the
                # plan. It is not printed: a single-use authorisation does not belong in
                # console scrollback, and nothing here consumes one.
                "train_token_created": False,
                "train_token_consumed": False,
                "training_executed": False,
            })
    except Exception as exc:  # noqa: BLE001 — the refusal IS the answer
        print(json.dumps({"status": "refused",
                          "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1
    print(json.dumps({"status": "ok", **payload}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
