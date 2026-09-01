# V69 · M62 · S4E — the reference-adapter execution path, qualified and frozen unrun

**This milestone built an instrument and measured nothing with it.**

Zero base-model weight loads. Zero adapter loads. Zero generations. Zero evaluation
attempts. Zero holdout spends. Zero EVAL authority created, requested or consumed.
`eval-v7` ends this milestone exactly as it began it: `FROZEN_UNUSED`, `spent_by` null.
Candidate 004 keeps `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` under its HOLD; candidate 005
stays `TRAINED_UNEVALUATED` with `evaluation_corpus` and `evaluation_receipt` both null.

Everything below is body-free. No `eval-v7` prompt, target or task body appears in this
document, in `PROGRESS.md`, in the control plane, in any test failure message or in any
commit message.

---

## 0 — Why this milestone had to exist

S4D built Protocol V4 and said plainly what it was: a **representation layer, not an
executor**. `protocol_v4.py` had no production caller anywhere in the repository. The
comparison the science asks for — candidate 004 against candidate 005, one fresh corpus,
one attempt — could be *described* and could not be *run*.

Three independent structures blocked it, all of them correct:

| Where | What it refuses | Why it is right |
|---|---|---|
| `backend.py:241` | a BASELINE-role request carrying an adapter | the old protocol's whole claim is "this arm had no adapter"; a runner that could quietly attach one would make that claim unfalsifiable |
| `runner.py:358` | — | builds arm 0 as `adapter=None`, literally |
| `transformers_peft.py:202` | — | runs adapter verification only `if request.is_candidate` |

**Not one of those was weakened.** The frozen execution machinery — `runner.py`,
`store.py`, `execution.py`, `preflight.py`, `generation.py`, `gates.py`, `policy.py` — is
byte-identical to the S3Q.0 subject commit `b928f9d4`, and the live guard that asserts so
is green.

---

## 1 — Starting authority, recovered from Git rather than from memory

| | |
|---|---|
| Start HEAD | `4b74ddaa7c28ef885945ec12a3b257faf35026a1` |
| `master` | `3705114228edef2f665be349c5c4429b7b16777a` — untouched |
| Origin divergence | `0 0` · worktree CLEAN |
| Control plane at entry | V3, generation 22, verifier PASS, PROBLEMS 0 |

Every expected scientific value matched exactly and nothing was repaired: candidate 004
`EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW`/HOLD, candidate 005 `TRAINED_UNEVALUATED`, `eval-v7`
`FROZEN_UNUSED` with `spent_by` null and 36 tasks, `eval-v6` `USED_IMMUTABLE`, `eval-v5`
`FROZEN_UNUSED` and RETIRED, and TRAIN/EVAL/promotion authority all
`NONE_OBSERVED_IN_REPOSITORY`.

**A holdout author is never its evaluator.** S4D authored `eval-v7`; S4E runs from a
separate branch, `jarvis-v69-eval-v7-run`, declared by governance-only generation 23.

---

## 2 — The one structural idea

Under Protocol V4 **both** arms are adapter-bearing. At the backend protocol layer that is
not two kinds of request — it is the same kind twice. So both arms are built as
`EvaluationRequest(role=CANDIDATE, adapter=<that arm's reference>)`, and the arm identity
lives *beside* the request in `AdapterArmReference`.

This is the strongest single-axis guarantee available here, not a way past a validator:

* both arms enter `transformers_peft._generate` through the **same branch** — same chat
  rendering, reasoning policy, truncation rule, `set_seed`, `generation_kwargs`, PEFT
  attach, `active_adapters` liveness assertion and release;
* both arms therefore receive the **full** `_adapter_problems` verification: symlink
  refusal, `validate_adapter_directory`, tree-hash comparison against the plan-time
  reference, base-model and tokenizer cross-checks. Under the old shape that was gated on
  `request.is_candidate`, so a reference arm would have received **none** of it — and the
  arm nobody checks is the arm a swap hides in;
* the only values that differ between the two requests are `adapter` and
  `adapter_directory`. **That is the experiment.**

`parity_hash` excludes role and adapter, so both arms' parity hashes are equal and the
existing per-task parity assertion keeps working unchanged.

### The word "role" means two things, and this is where they part

On the **request** it means *what kind of arm is this* (both CANDIDATE, both
adapter-bearing). On the **result** every downstream consumer reads it as *which slot of
the comparison this is* — `compare_pair` fills `baseline_score`/`candidate_score` from it,
and `build_score_evidence` **refuses** to file a score whose role disagrees with its slot.

That refusal is what stops one arm's answer being attributed to the other, so it was not
weakened. The V4 runner stamps the comparison slot onto each result *after* the request
check. `baseline` means **arm 0**, which under V4 is the reference adapter — and
`V4ArmBinding` records `baseline_slot_is_a_bare_base_model: False` beside both digests.

---

## 3 — What the measurement layer does NOT know

The V4 runner returns an ordinary `PairedRun`. `build_comparison`, `paired_statistics`,
`evaluate_gates` and the whole metric stack run **completely unchanged** — not one line of
the measurement layer is V4-aware. That is precisely why S4D's gate-equivalence analysis
holds, and it was re-verified independently here rather than taken on trust:

| Check | Result |
|---|---|
| occurrences of `baseline` in `metrics.py` | **0** |
| `base_model` / `adapter` / `bare base` / `no adapter` in `gates.py` | **0** |
| the 10 `baseline` mentions in `gates.py` | all `summary.baseline_metrics`, i.e. arm 0 |
| gates · statistics · families · metrics digests re-derived | **all four MATCH** |

```
gates       e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5
statistics  663ebf65b73536fe3bd41043568a1f737ff751a43a964d14ff67c4e12662bf18
families    580fbe9104cbe684f702cba016e1191c83745fb8502642636c3fc885135065b1
metrics     e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a
```

**`V4_GATE_SEMANTICS: EQUIVALENT`. Zero thresholds changed.** What genuinely changes is
that the gates become harder to pass and more informative: a security regression now means
*005 is worse than a trained defensive adapter*, not *worse than a bare base model*. That
is a consequence of binding a stronger reference, never of retuning a gate — and a null
result under V4 is a materially stronger claim than the same null under v1–v3.

---

## 4 — Four defects found, four closed

Two came from the read-only audit, two from the fault-injection gauntlet. Each is recorded
with what it would have cost, because a defect list that only says "fixed" teaches nothing.

### D-S4E-1 · the live exam's body source was unprotected
`FORBIDDEN_BODY_SYMBOLS` stopped at `v6`, while `corpus_v7()` and `corpus_v7_material()`
have existed since the S4D freeze. A control-plane surface could have cited the **live**
exam's body source and passed. Worse, the fw1 suite listed `corpus_v7_material` as a
*harmless negative control* — "a generation that does not exist" — so the hole was
test-enforced. Registry extended; the control moved to `v8`.

### D-S4E-2 · a v4 receipt could not be validated at all
`m62.eval_receipt.4` was not in `MODERN_EVAL_RECEIPT_VERSIONS`, and the dispatch would
have validated a `.4` document against `.2`'s shape. The published v4 schema was an
unenforced orphan. It is now dispatched, canonically encoded, and the file is enforced
**byte-for-byte** by an in-code builder rather than trusted.

### D-S4E-3 · `classify_empirical_status` failed OPEN
It decided "was this live?" from a four-name **denylist** of known doubles. Any double with
an unlisted id classified as `live_measured`, and `decide_eligibility` would then let
synthetic numbers reach `eligible_for_human_review`. The S4E marker double reproduced it
immediately. It is now an **allowlist**: live only if every backend that answered is a
reviewed production backend. Historical classification does not move — S3Q and S3Y ran
`transformers_peft` alone.

### D-S4E-4 · two `ReceiptError` classes
`build_m62_eval_receipt` is importable both as `scripts.build_…` and `build_…`, creating
two module objects with two distinct exception classes, so a caller's `except ReceiptError`
silently misses the one raised. Aligned on the package path the suite already uses.

### Reported and NOT closed, because closing them means editing frozen files

* **the v1–v3 single-spend gap.** `record_holdout_commit` refuses a second commit on the
  same `plan_hash` or the same `(evaluation_id, generation)` — neither notices an attempt
  that renames itself. `store_v4.assert_holdout_never_spent` closes it for the V4 path by
  refusing on **dataset identity and pack digest**, checked against *both* ledgers. The
  v1–v3 path keeps the original two keys.
* **the ledger is per-`output_root`.** A caller pointing a run elsewhere gets a fresh
  ledger. Mitigated by the plan binding `expected_output_root_id`.
* **declared precision and device are never applied.** The eval backend passes no
  `torch_dtype` and no `device_map`, so `cpu`/`fp32` is the library default rather than an
  applied setting. Identical for both arms, so **not** a single-axis violation; recorded
  because the receipt names the policy.

---

## 5 — The runtime, and an honest deviation

`PROGRESS.md`'s frozen invariants name `.venv-m62-eval-linux` as "the runtime the
measurements of record were taken in", and say it **stays immutable**. It does not say
every later evaluation must execute inside it — and on this host it can no longer execute
anything at all:

> it was built against `/usr/bin/python3.13`, which **no longer exists**. Its entry points
> now resolve to `/usr/bin/python3` (3.14.6) while its packages sit in a 3.13
> `site-packages`, so it reports `torch` as missing when torch is present on disk.

Nothing was repaired — repairing it means installing packages, which the frozen invariants
forbid outright. The environment is untouched and stays immutable.

The measurement runs in `.venv-m62-train-py314`, and the deviation is exactly one
interpreter minor version:

| | eval profile (dead) | `.venv-m62-train-py314` (live) |
|---|---|---|
| Python | 3.13.14 | **3.14.6** |
| torch · transformers · peft | 2.13.0+cpu · 5.14.1 · 0.20.0 | **identical** |
| accelerate · safetensors · tokenizers · numpy · huggingface_hub | 1.14.0 · 0.8.0 · 0.22.2 · 2.5.2 · 1.27.0 | **identical** |
| `pip check` | — | **PASS** |

It is also the runtime candidate 005 was **trained** in. **Both arms run in one process
under one runtime**, so this is a deviation from the historical runtime and never a
difference between the arms. `RUNTIME_REPORT_SHA256` binds it into the plan hash; the
volatile halves (available RAM, free disk) are reported beside the digest and excluded from
it, so a token cannot expire between being printed and being typed.

---

## 6 — Everything proved before any authority exists

`ARTIFACT_INTEGRITY` — both adapters re-hashed from bytes on disk:

```
004  a105e01ca99d9b47d45c408a614b78aa9ec22df83ad32b321df57b1a1c3ecc67   MATCH
005  52d6da26dca20dce93de8845fa08e0b3e452d86472fd6e06d756a30e52688f2a   MATCH
```

`COMMON_BASE` — both sealed training receipts declare, byte-identically:
`Qwen/Qwen3-0.6B` @ `c1899de289a04d12100db370d81485cdf75e47ca`, identity `9701f4f3…`,
chat template `a55ee1b1…`. Distinct: adapter, manifest and artifact-set digests.

`eval-v7` rebuilt from nothing into a fresh root and reproduced the frozen identity
exactly — manifest `e80cc46f…`, pack `e6d8d0b2…`, 36 tasks, 12/12/12 splits, families
12/9/9/6, order balance 18/18, zero eligibility blockers. `TASK_ORDER_HASH`
`77a946d1ae78aa17c8bd89bb1a60b10cbbfe112aa3c4257151016330a129426d` binds the exact
sequence **without publishing a single task id**, which the control-plane firewall
requires.

### The suites

| File | Tests | What it owns |
|---|---|---|
| `…_s4e_v4_execution.py` | 37 | single axis, contamination markers, activation state machine, generation accounting, arm order, cross-arm containment, timeout enforcement |
| `…_s4e_exactly_once.py` | 26 | the four exactly-once properties, tested separately because they fail separately |
| `…_s4e_fault_injection.py` | 22 | the gauntlet, plus the plan mutation matrix |
| `…_s4e_receipt_v4.py` | 30 | the receipt dry run and 20 attacks on it |

**115 new tests.** Highlights worth naming:

* **contamination** is measured, not restated: the doubles are keyed on the **adapter**,
  not the role, so "the reference arm never emitted the candidate's marker" is a
  measurement. Both arm orders occur in one run, so a residue would show either way.
* **isolation is structural.** `LoadStrategy.ISOLATED` is the only selectable strategy;
  `generate()` loads its own model and calls `release()` in a `finally`. Two arms cannot
  share a model object, so KV-cache reuse is not mitigated — it is impossible.
  `AdapterActivationLog` makes it observable: `NO_ADAPTER → REFERENCE_ACTIVE → CLEAN →
  CANDIDATE_ACTIVE → CLEAN`, with no `BOTH_ACTIVE` state and reaching one arm's active
  state without passing through CLEAN refused.
* **timeout enforcement (D33) is closed for this path only.** A sleeping backend is
  stopped by the wall clock, the task stays in the denominator as a TIMEOUT, and an
  abandoned worker is reported as abandoned rather than claimed cancelled.
* **the spend is durable before the model.** The paired attempt is written and `fsync`ed
  *before* the ledger commit, so a crash between them reads as "attempted, never reached a
  model" — the fail-closed direction. A corrupt attempt line is a refusal, never a silent
  skip, because treating a corrupt spend record as absent is how a holdout gets spent
  twice.

---

## 7 — Capacity, proved before the spend

Discovering *after* `eval-v7` is spent that the repository cannot record what it measured
would be the one outcome this evaluation may not produce. So capacity is a pure transform
of the real snapshot, measured through the same `canonical_bytes` that writes it.

| Terminal state | bytes | headroom |
|---|---|---|
| ELIGIBLE | 8 521 | 26 295 |
| NOT_ELIGIBLE | 8 522 | 26 294 |
| **INCONCLUSIVE** | **8 621** | **26 195** |
| ABORTED | 8 532 | 26 284 |
| DURABILITY_FAILURE | 8 508 | 26 308 |

Budget 34 816, required floor 1 024. **`CONTROL_PLANE_POSTEVAL_CAPACITY: PASS`, and
result-neutral**: every ending fits, and the largest is INCONCLUSIVE — the one that needs
the most prose because it must say what was *not* established. A projection that only fits
when the result is good is a projection that pressures the result.

`PROGRESS.md` is the tight one: **40 945 bytes against 40 960**, 15 bytes of headroom and
150 lines. The post-evaluation milestone text will not fit without the repository-
prescribed lossless compaction — superseded detail folded into the milestone documents
that own it, with no defect, limitation, invariant or historical result deleted. The cap
is **not** raised.

---

## 8 — What this milestone did not do

- did not train anything, and created no candidate 006
- did not create `eval-v8`, or modify `eval-v7`'s bodies, rubrics or gates
- did not inspect or second-look `eval-v6`
- did not change a numerical gate, a threshold or a policy digest
- did not touch M64 / M64.1, the Cognitive Mesh, World State or Memory
- did not modify the production model assignment
- did not promote anything, merge, tag, release or bump a version
- **created no EVAL authority, and spent no holdout**

Readiness is not authority. A live run additionally requires a fresh single-use human
`EVAL:<plan-hash>` bound to the exact V4 plan hash, and no result it produces implies a
promotion.
