# V69 M62 S3Q.0.1 — Portable evaluation evidence, closed before the irreversible act

**Status:** COMPLETE. Pre-live hardening only.
**Grants:** nothing. No EVAL authority, no plan consumption, no promotion.
**Subject:** the portable EVALUATION receipt, its schema, its verifier and the control
plane's acceptance of it.
**Not touched:** the live measurement machinery qualified by S3Q.0.

---

## 1. The one question this milestone had to answer

`eval-v4` can be spent exactly once. Everything the run produces — the task pack, the
per-arm results, the scores, the comparisons, the report — lives in a **gitignored**
runtime tree. A fresh clone has none of it.

So the question is not "will the evaluation work". It is:

> If `eval-v4` is spent tomorrow, can this repository later prove, from tracked
> **body-free** evidence alone, exactly which candidate was measured, which trained
> adapter bytes it used, which training run produced them, which base model was the
> simultaneous baseline, what was approved, what durably happened, how much completed,
> and **why** the final `EVALUATED_*` state follows — without the runtime tree, and
> without trusting a snapshot merely because another writable constant agrees?

S3Q.0 built `m62.eval_receipt.1` to answer it. This milestone audited that answer and
found it insufficient in nine ways, four of which are contract changes. The result is
`m62.eval_receipt.2`.

**No evaluation was performed. No model was loaded. No token was generated. `eval-v4`
remains `FROZEN_UNUSED` with `spent_by: null`.**

---

## 2. The findings, each reproduced against `.1` before it was fixed

Every finding below was reproduced from the source as it stood, on synthetic evidence,
and each reproduction is now a test. A test that only asserts the fix passes on a
repository where the defect never existed.

| # | Finding | Status | Evidence |
|---|---------|--------|----------|
| A | `candidate.adapter_sha256` and `adapter_manifest_hash` were emitted as the **empty string**, and the `.1` schema explicitly permitted `""`. The single fact an auditor most needs — which weights were measured — was a blank. | **CONFIRMED** | `test_finding_a_v1_emitted_empty_adapter_identities` |
| B | `authority.creations` counted a durable **token-creation event that does not exist**. The ledger owns `started` and the holdout commit; nothing records a creation. | **CONFIRMED** | `test_finding_b_v1_counted_an_authority_creation_event_that_does_not_exist` |
| C | Standalone `--verify` called `verify_receipt()` and nothing else. A document that was **not a receipt at all** — unknown schema version, arbitrary keys, no adapter identity — printed `"status": "verified"` once its digest was recomputed over its own nonsense. | **CONFIRMED** | `test_finding_c_v1_standalone_verify_skipped_schema_validation` |
| D | `--generation-directory`, `--candidate` and `--evaluation-source-commit` were `required=True` in **every** mode, so a read-only verification demanded three dummy build arguments the verifier then ignored. | **CONFIRMED** | `test_finding_d_v2_verify_mode_needs_no_build_arguments` |
| E | The candidate identity was a **caller string checked against nothing**. Passing `--candidate anything-at-all` relabelled a valid generation and the result still verified. The `.1` docstring claimed the identity was "CHECKED against the adapter reference the report bound"; the code performed no such check. | **CONFIRMED** | `test_finding_e_a_caller_may_not_rename_evidence` |
| F | `evaluation_source_commit` was written verbatim from the caller. Any syntactically valid SHA — including one that never existed — entered the receipt and verification had no opinion. | **CONFIRMED** | `test_finding_f_a_wrong_source_assertion_is_refused` |
| G | Terminal detection was `elif event != "started": terminal = event`. An unrecognised future ledger line therefore became the terminal witness — and, arriving last, **overwrote the real one**. | **CONFIRMED** | `test_finding_g_v1_let_an_unknown_event_become_the_terminal_witness` |
| H | Ledger plan hashes were collected into a set and the **set** was bound. A run whose `started` line named plan A and whose holdout commit named plan B produced a receipt that verified. | **CONFIRMED** | `test_finding_h_v1_accepted_ledger_lines_naming_different_plans` |
| I | `outcome.eligibility` was **copied from the report**. The strongest statement a clean clone could make about the one irreversible act in the milestone was *"the receipt says so."* | **CONFIRMED** | `test_finding_i_v1_copied_the_verdict_instead_of_evidencing_it` |

### 2.1 Finding J — found while closing the others, and it would have blocked the live run

`.1` bound `holdout_commit.first_task_id`. On a live `eval-v4` run that field holds one of
the 36 frozen task ids. `check_evaluation_receipt` **refuses any tracked receipt that
names an eval-v4 task**, so a live `.1` receipt could never have been accepted by the very
control plane it was written for. The holdout firewall and the receipt contract
contradicted each other, and the contradiction would have surfaced only after the holdout
was already spent.

`.2` binds `first_task_hash` instead. That is strictly stronger — a digest identifies the
task without naming it — and the whole commit line is additionally bound by
`holdout_commit_event_hash`.

---

## 3. What `m62.eval_receipt.2` binds

```
                    candidate003 snapshot
                              |
                              v
                 TRACKED S3P TRAINING RECEIPT           <- identity ROOT
              (candidate_id, adapter sha256,
               manifest hash, artifact_set hash)
                              |
              re-verified against the adapter bytes
              by the production verify_completed_run
                              |
                              v
                 AdapterEvaluationReference             <- reference_hash()
                              |
        must equal plan == report == holdout-commit reference hash
                              |
                              v
                        EXACT EVAL PLAN
              (config hash, pack, hidden targets,
               order assignment, policy digests)
                              |
                              v
                  durable plan-start event              <- digest bound
                              |
                              v
              holdout-model-facing-commit event         <- digest bound
                              |
                              v
                   recognised terminal event            <- digest bound
                              |
                              v
              body-free result counts + gate evidence
                              |
                              v
                  PORTABLE EVAL RECEIPT v2
                              |
                              v
                Control Plane EVALUATED_* state
```

No writable layer substitutes for a missing previous one. Runtime presence is **not**
required forever: once the receipt exists, the snapshot, the training receipt and the
evaluation receipt cross-bind the adapter by three independent digests, and history stays
true after the gitignored tree is deleted.

### 3.1 Section-by-section

| Section | What it establishes |
|---------|--------------------|
| `source` | Commit and tree oid **derived from the repository HEAD** at build time, plus an explicit `evidence_level` sentence saying what that does and does not prove |
| `candidate` | Four **mandatory 64-hex** adapter identities, and `identity_source: training_receipt` |
| `training_receipt` | Path, **`training_receipt_sha256`**, candidate, training plan hash, training source commit |
| `baseline` | `model_id` and `revision` **in the clear**, beside the reference and identity digests |
| `holdout` | Dataset identity, pack hash, hidden-target store hash, shard and split digests, counts |
| `plan` | Plan hash, config hash, order assignment, `performs_inference`, exact pack binding, expected task count |
| `policies` | Eight policy digests **plus the whole canonical generation policy**, so the digest is re-derivable rather than quoted; `configured_timeout_s` and `timeout_enforced` separately |
| `authority` | Form, bound plan hash, `plan_consumption_count`, `holdout_commit_count`, and `human_authorization: external_milestone_authority` |
| `ledger` | One start, one crossing, one **recognised** terminal event, one plan hash, three **event digests**, and any unrecognised events named as explicitly non-terminal |
| `holdout_commit` | Pack identity, order, first task **hash**, first arm, request parity hash, backend |
| `execution` | `report_serialization_state` (renamed from `run_state`), empirical status, backends, artefact verification |
| `results` | Baseline / candidate / paired / score counts, total model results, measured and missing pairs, wins-ties-losses |
| `evidence` | Report, manifest, `evaluation_artifact_tree_hash`, comparison, metrics, pack, gate and bootstrap digests, per-file evidence |
| `decision_evidence` | The **body-free inputs the decision was made from**, the canonical decision, its digest, and the function that rederives it |
| `outcome` | Eligibility, human review, the three "grants nothing" flags, blockers, warnings, limitations |

---

## 4. Finding I, closed: the verdict is rederived, not repeated

This is the change that matters most, and the obvious fix was the wrong one.

Reimplementing `decide_eligibility()` inside the receipt verifier would create **two**
eligibility algorithms, and the day they disagree is the day the audit is worth less than
no audit at all.

So a pure, body-free helper was added **beside the decision logic it verifies**:

```
training_gym/evaluation/reports.py
    gate_report_from_evidence(payload)      -> GateReport
    bootstrap_report_from_evidence(payload) -> BootstrapReport
    decision_from_evidence(...)             -> EligibilityDecision
```

`decision_from_evidence` rebuilds the serialised inputs **strictly** — every field
required, every unknown field refused, and the derived members (`passed`,
`blocking_count`, `security_blocking_count`, `warning_count`, `observed_improvement`,
`excludes_regression_margin`, `indicates_regression`, `claim`) re-derived and required to
match — then calls the **production** `decide_eligibility`. One algorithm, two callers.

It decides nothing live: `build_report` does not call it, no runner reaches it, and it
cannot change a gate, a metric, a policy digest or a report. It reads a gate report, a
bootstrap report, a status word and a state word, and nothing else.

`_BootstrapCarrier` exists because `decide_eligibility` reads exactly `summary.bootstrap`
from its summary argument. Reconstructing a full `ComparisonSummary` would require every
per-task comparison — which means every model response — which is precisely the material
a body-free receipt must never carry. If that ever stops being the only member read, the
failure is a loud `AttributeError`, not a quiet wrong verdict.

### 4.1 Non-vacuity

The refusals are exercised through the canonical algorithm, on receipts made
**internally consistent** about the wrong verdict — every downstream field agreed, only
the evidence left telling the truth:

* a security blocking finding + a claim of `eligible_for_human_review` → REFUSED
* `empirical_status: synthetic_only` + that claim → REFUSED
* `empirical_status: partial_live` + that claim → REFUSED
* a deterministic gate blocker + that claim → REFUSED
* a bootstrap verdict that supports no direction + that claim → REFUSED
* gate evidence whose summary counts contradict its own findings → REFUSED
* and, in the other direction, the same evidence with a sufficient sample and no blocker
  **does** reach `eligible_for_human_review` — so none of the refusals above is
  unconditional.

No new threshold was invented. No eligibility criterion was added, removed or reweighted.

---

## 5. What the receipt still does not prove

`receipt_hash` proves **payload integrity relative to the canonical bytes**. That is all.

It does not prove human identity, human authorisation, a commit signature, PKI
authenticity, or who physically ran the command. Authenticity comes from the receipt
being **tracked** and from the control plane's own hash chain — not from SHA-256.

The receipt therefore carries `authority.human_authorization:
"external_milestone_authority"` and **not** `human_authorized: true`. A builder that could
write the second field proves nothing by writing it. Human authorisation lives in the
milestone document; the machine receipt proves that the approved-form plan was consumed,
that the holdout boundary was crossed, that the run terminated, and that the measured
evidence produces the claimed result.

`source.evidence_level` says the same thing about the commit: the receipt was built in a
worktree at that commit, and *"it does not by itself prove which bytes executed."* S3Q's
execution freeze is what makes HEAD the evaluated source; that is an architectural
guarantee, not a cryptographic one, and the receipt says so rather than implying more.

---

## 6. D33 stays open, and stays truthful

The live run configures `timeout_s: 300`. **Nothing enforces it.** No watchdog exists.

`.2` records both facts separately — `configured_timeout_s: 300` and
`timeout_enforced: false` — and the verifier **refuses** a receipt that claims the timeout
was enforced. Recording only the number is how "300" quietly reads as a watchdog.

**D33: OPEN_UNCHANGED.**

---

## 7. Atomic durability

Portable evidence is written **after** an irreversible act. There is no second holdout, so
a truncated file that parses far enough to look plausible is worse than no file at all.

`emit_receipt` therefore:

1. refuses a **symlink** destination rather than following it;
2. refuses an **existing** destination rather than overwriting it;
3. writes to a temporary file in the **same directory** (same filesystem), flushes and
   `fsync`s it;
4. **re-reads those bytes from disk**, strictly schema-validates them, re-derives the
   digest and runs the semantic verifier;
5. only then `os.replace`s it into position, and `fsync`s the parent directory;
6. re-reads the **final** bytes and verifies them again;
7. on any failure, removes only the exact temporary file this invocation created — or, if
   the final re-read fails, the destination this invocation created. Never a directory,
   never a glob.

A failure before step 5 leaves **no file at the destination**. A serialiser or write
defect cannot emit `status: ok` for bytes the verifier would refuse.

---

## 8. Build and verify are two modes now

```
build_m62_eval_receipt.py build   --generation-directory ... --training-receipt ...
                                  --adapter-run-directory ... --evaluation-config ...
                                  [--ledger ...] [--repo-root ...]
                                  [--expected-candidate ...]
                                  [--expected-evaluation-source-commit ...]
                                  [--emit ...]

build_m62_eval_receipt.py verify  <receipt.json>
```

`verify` requires **nothing else**. It creates no directory, writes no cache, modifies no
receipt, touches no control plane, emits no confirmation token, and needs neither the
runtime adapter nor `eval-v4`. It reads one file — refusing a symlink, a non-regular file
or one over the 1 MiB body-free ceiling — decodes it, selects the known version, validates
the strict schema **first**, then re-derives the digest, then runs the semantic verifier,
then scans for token literals, private paths, eval-v4 body symbols, eval-v4 task ids and
non-ASCII bytes. An unknown version is REFUSED; an unexpected property is REFUSED; a
missing mandatory property is REFUSED.

The two caller strings that remain are **assertions**: `--expected-candidate` and
`--expected-evaluation-source-commit` are compared against the derived values and a
mismatch is a refusal. A caller can state what it believes and be told it is wrong; it
cannot state what the receipt will say.

The `.1` flat invocation is gone. `.1` receipts are still **read** by the standalone
verifier — history stays verifiable — but a candidate measured from now on may only
present `.2`.

---

## 9. Control-plane acceptance

For a modern (non-legacy) `EVALUATED_*` candidate, `check_evaluation_receipt` now requires
all of: the receipt exists, is tracked, is a regular file, declares a **modern** version,
validates strictly, re-hashes, names this candidate and this state, carries no token
literal / private path / eval-v4 symbol / eval-v4 task id and is ASCII — **plus** the
S3Q.0.1 battery, which reaches outside the receipt:

* the terminal vocabulary is **re-derived from `EvaluationRunState.is_terminal`** and the
  restated constant must equal it;
* the training receipt the snapshot names is the one the evaluation receipt bound, and its
  **tracked bytes hash to the digest the receipt recorded**;
* the adapter weights digest and manifest digest are agreed by **three** independent
  surfaces — snapshot, training receipt, evaluation receipt — and the artefact-set digest
  by two;
* the baseline `model_id` and `revision` are the ones the control plane names;
* the evaluation source commit **is an object here and is an ancestor of HEAD**;
* the canonical eligibility is **rederived from the receipt's own body-free evidence** and
  must support the claimed state.

Legacy candidates 001 and 002 are untouched and the set stays closed. Nothing may be added
to it, because a candidate evaluated after S3Q.0 has the machinery available by
definition.

---

## 10. Scope discipline

The live measurement machinery qualified by S3Q.0 was **not edited**:
`runner.py`, `store.py`, `execution.py`, `preflight.py`, `generation.py`, `gates.py`,
`policy.py`, `backends/` and the eval corpus builders are byte-identical to
`b928f9d485a49b90a3be9eee7fd5de5a50e54230` — asserted by a test, not by a claim.

The only production module touched is `reports.py`, and only additively: three pure
verification functions and one small carrier dataclass. `decide_eligibility` itself,
`build_report`, the report schema and every policy digest are unchanged.

Unchanged and re-asserted:

```
GENERATION_POLICY_HASH  c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7
METRIC_POLICY_HASH      e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a
GATE_POLICY_HASH        e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5
MAX_NEW_TOKENS          512
PLAN_SCHEMA_VERSION     m62.evaluation_plan.2
EVALUATOR_VERSION       m62.s3q0.1
D28 OPEN_UNCHANGED   D33 OPEN_UNCHANGED   D38 FIXED_UNCHANGED (IS_GATE: NO)   D39 OPEN_UNCHANGED
```

Only the portable receipt version moved.

---

## 11. `spent_by`, prospectively

`eval-v4` is **not** being marked spent. It stays `FROZEN_UNUSED` with `spent_by: null`.

What S3Q.0.1 defines is what `spent_by` must be able to bind when the live run happens: a
stable body-free identity of the actual evaluation — the receipt's `evaluation_id` and
generation, cross-checkable against the receipt's `holdout` block and the candidate's
`evaluation_corpus`. The control plane already cross-checks the receipt's holdout against
the dataset entry, requires that entry to be `USED_IMMUTABLE`, and requires the candidate's
`evaluation_corpus` to name the same dataset.

The self-asserted boolean `holdout.spent_by_this_evaluation: True` that `.1` wrote as a
constant is **gone**. A builder writing `True` is not evidence. The durable
`holdout_model_facing_committed` event is, and it is bound by count, by body-free content
and by digest.

---

## 12. What this milestone did NOT do

* No live preflight against real `eval-v4`
* No `EVAL:` confirmation string materialised
* No plan consumed
* No model weights loaded, no generation, zero response tokens
* No baseline run, no candidate 003 run
* No eligibility measured for candidate 003
* No promotion, no activation, no registry mutation
* No candidate 004, no eval-v5
* No eval-v4 body read, semantically or programmatically

---

## 13. NEXT

A **new** Claude Code session, at the exact final HEAD and generation-5 authority produced
here, performing the one-shot live evaluation of candidate 003 against a simultaneously
measured baseline on frozen `eval-v4` — **only** after a new, explicit human EVAL
authorisation.

**S3Q.0.1 grants no live evaluation authority.**
