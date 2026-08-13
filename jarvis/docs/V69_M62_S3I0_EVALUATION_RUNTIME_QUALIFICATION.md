# V69 M62 S3I.0 — Held-out evaluation runtime qualification

**Date (UTC):** 2026-08-13
**Status:** QUALIFICATION COMPLETE — **NOTHING WAS GENERATED**
**Scope:** determine with measurement, not assumption, whether the evaluation runtime wastes
material wall time reloading model weights; decide on that evidence whether to sessionize;
and qualify the preconditions for S3I. No response was generated, no held-out task was read
by a model, no `EVAL` authority was created or consumed.

```
S3I0_RUNTIME_QUALIFICATION:  PASS
TOKENS_GENERATED:            0
HELDOUT_TASKS_EXECUTED:      0
EVAL_TOKEN_CREATED:          NO
EVAL_TOKEN_CONSUMED:         NO
RUNTIME_OPTIMIZATION_DECISION: KEEP_EXISTING_LOADING_STRATEGY
SOURCE_CHANGED:              NO
```

**The headline is not the benchmark.** The load measurement settled the question it was asked
in about twenty minutes and the answer was "no, don't optimise". The two findings that
actually matter — **D32** and **D33** — were found while preparing the evaluation the
benchmark was meant to speed up, and each would have cost a spent single-use `EVAL` token.

---

## 1. Authorisation and boundary

The operator authorised **M62 S3I.0 — held-out evaluation runtime qualification**: bounded
Git verification, inspection of the evaluation architecture, **load-only** benchmarking of the
pinned baseline and the S3H candidate with zero generation, source changes to evaluation
runtime infrastructure *if the evidence justified them*, focused tests, an evaluation plan
preview, documentation, a PROGRESS update, commits and a push.

It authorises **none** of: generating even one evaluation response, consuming an `EVAL`
token, creating a live generation, executing held-out evaluation, changing the evaluation
corpus, scoring, graders, thresholds, security vetoes, reasoning policy or `max_new_tokens`,
training, another `TRAIN` token, modifying the S3H adapter, promotion, activation, registry
mutation, merge, tag, release or version bump.

**Starting checkpoint (verified):** branch `jarvis-v69-m62-training-gym`, HEAD
`4772a2c94c635600fb15ea1d21d738a0e34590bd`, divergence `0 0`, `origin/master`
`3705114228edef2f665be349c5c4429b7b16777a` unchanged, working tree clean.

**Subagents: none.** The session already held the context; nothing here needed delegation.

---

## 2. The S3H candidate is intact

Re-verified before it was used for anything:

| Check | Result |
|---|---|
| `verify_completed_run` | **0 problems** |
| `adapter_model.safetensors` sha256 | `43213035c15cd38928d2d6a3bdbd9af96872a954801c6bfd0a9b82a8e22ac858` — matches S3H |
| Adapter mutated | **NO** |

---

## 3. The historical model-load lifecycle, traced

```
HISTORICAL_MODEL_LOAD_LIFECYCLE:        PER_REQUEST
EXPECTED_MODEL_LOAD_COUNT_FOR_36x2:     72
```

Traced through the production code rather than taken from the previous session's prose:

| # | Boundary | Code | Behaviour |
|---|---|---|---|
| 1 | Orchestration | `evaluation/runner.py` `run_paired_evaluation` | `for task in pack.tasks:` — one baseline call and one candidate call **per task** |
| 2 | Invocation | same, `_invoke` | `backend.generate(request)` — one call per arm per task |
| 3 | Backend entry | `backends/transformers_peft.py` `generate` | *"Preflight, then import, then load, then generate, then release. In that order."* |
| 4 | Load | `_generate` | `AutoTokenizer.from_pretrained` + `AutoModelForCausalLM.from_pretrained` (+ `PeftModel.from_pretrained` for the candidate) |
| 5 | Release | `generate`'s `finally:` | `self.release()` — drops handles and `gc.collect()`s **after every single task** |

So for 36 tasks × 2 arms the weights are loaded and released **72 times**, in one process.

**A documentation imprecision worth naming, because it is not a defect.** The backend's
`LoadStrategy.ISOLATED` docstring says it *"loads the base model twice — once per arm"*. The
implemented lifecycle reloads **per request**, which is 72 times, not twice. The docstring is
describing the property the strategy guarantees — that no state crosses between arms — and
the implementation delivers something strictly *stronger* than it claims. Nothing is wrong;
the sentence is just not a description of the load count, and a future reader could easily
misread it as one.

---

## 4. Load-only benchmark

**Method.** The exact load calls `_generate` makes, replicated against the same reviewed
cache, offline, in one process, three load/release cycles per arm — which is precisely the
historical loop shape. Each arm ran in its own process, sequentially, never concurrently
(§19 of the brief: this host is CPU-only). **No prompt was built, no forward pass was run,
`generate()` was never called on a model, and no held-out task was read.**

`psutil` 7.2.2 was already present in the isolated interpreter, so RSS was recorded without
installing anything.

### 4.1 Baseline arm — base model only

| cycle | tokenizer (s) | model (s) | ready-to-infer (s) | release (s) | RSS loaded (MB) | RSS after release (MB) |
|---:|---:|---:|---:|---:|---:|---:|
| 1 (first) | 1.2964 | 0.6420 | **1.9384** | 0.5391 | 1005.8 | 347.6 |
| 2 (warm) | 1.2415 | 0.4948 | **1.7363** | 0.5279 | 1008.9 | 356.3 |
| 3 (warm) | 1.2744 | 0.4958 | **1.7701** | 0.6761 | 1009.3 | 363.3 |

```
BASELINE_LOAD_FIRST_SECONDS:   1.9384
BASELINE_LOAD_MEDIAN_SECONDS:  1.7701
BASELINE_LOAD_RANGE_SECONDS:   1.7363 – 1.9384
BASELINE_LOAD_PLUS_RELEASE_MEDIAN: 2.4462
```

### 4.2 Candidate arm — base model + the immutable S3H adapter

| cycle | tokenizer (s) | model (s) | adapter (s) | ready-to-infer (s) | release (s) | RSS loaded (MB) | RSS after release (MB) |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 (first) | 1.2920 | 0.6758 | 1.1048 | **3.0725** | 0.5183 | 1052.4 | 391.5 |
| 2 (warm) | 1.2838 | 0.5921 | 1.0156 | **2.8915** | 0.5535 | 1070.8 | 404.9 |
| 3 (warm) | 1.2920 | 0.5454 | 0.8829 | **2.7202** | 0.5330 | 1070.4 | 411.8 |

```
CANDIDATE_LOAD_FIRST_SECONDS:   3.0725
CANDIDATE_LOAD_MEDIAN_SECONDS:  2.8915
CANDIDATE_LOAD_RANGE_SECONDS:   2.7202 – 3.0725
CANDIDATE_LOAD_PLUS_RELEASE_MEDIAN: 3.4450
ADAPTER_ATTACH_SECONDS:         0.8829 – 1.1048
```

### 4.3 Identity proved on every load, without a forward pass

| Property | Baseline | Candidate |
|---|---|---|
| Model class | `Qwen3ForCausalLM` | `PeftModelForCausalLM` |
| PEFT-wrapped | **no** | **yes** |
| `active_adapters` | n/a | **True** |
| Adapter r / alpha | n/a | **16 / 32** |
| `_name_or_path` | `Qwen/Qwen3-0.6B` | `Qwen/Qwen3-0.6B` |
| `model.training` | `False` (eval mode) | `False` (eval mode) |
| Revision | `c1899de2…` | `c1899de2…` |
| Adapter directory | none attached | the S3H run directory |

### 4.4 Two observations recorded rather than buried

**Framework import costs 21.2 s per process, once.** It is paid at first use, not per load.
It matters only to a design that would put each arm in its own process: that would pay it
twice instead of once, giving back ~21 s of the ~206 s such a design would save.

**Release does not return exactly to the pre-load baseline.** After each cycle RSS settled
~8 MB (baseline) to ~10 MB (candidate) higher than before it: 347.6 → 356.3 → 363.3 and
391.5 → 404.9 → 411.8. Extrapolated over 36 cycles that is roughly 300–400 MB of drift on a
host with far more than that to spare, and it is a small residue rather than the ~660 MB
working set failing to release. It does not change the decision and it is not a blocker; it
is recorded because "release" reporting `RELEASED` and RSS not returning to baseline are two
different facts.

---

## 5. Historical cost attribution

**The attribution is direct, not modelled.** The backend sets `started = time.monotonic()` at
the top of `generate()` — *before* the runtime import and *before* the load — and computes
`latency_ms` from that same mark. So the historical S3E.2 per-request latencies **already
contain the load**, and the measured load cost can be divided straight into them.

| | Baseline | Candidate |
|---|---:|---:|
| S3E.2 median request latency | 109.5 s | 123.0 s |
| Measured load + release (median) | 2.4462 s | 3.4450 s |
| **Load share of a median request** | **2.23 %** | **2.80 %** |

Aggregated over the full 36 × 2:

```
legacy_load_count                  72
baseline loads   36 × 2.4462  =    88.1 s
candidate loads  36 × 3.4450  =   124.0 s
TOTAL LOAD+RELEASE            =   212.1 s   ≈ 3 min 32 s

S3E.2 total, summing medians  = 8370 s     ≈ 2 h 19 min  (a LOWER bound: p95 was
                                             596.5 s / 704.4 s, so the real total was higher)

ESTIMATED_LOAD_OVERHEAD_FRACTION:  ≈ 2.5 %, and strictly less than that against the
                                   real total
```

The remaining ~97.5 % is autoregressive decoding on CPU: `max_new_tokens = 512`, median
output 434 / 428.5 tokens, and 27 of 72 generations hit the ceiling (PROGRESS §14.12).

---

## 6. Decision

```
RUNTIME_OPTIMIZATION_DECISION:
KEEP_EXISTING_LOADING_STRATEGY
```

**Measured, not argued.** Sessionizing per arm would cut 72 loads to 2 and save **≈206 s**
— and if the arms were put in separate processes for isolation, one extra 21.2 s framework
import gives back part of it, netting **≈185 s ≈ 3 minutes** against a run of **at least
2 h 19 min**. That is the case §8 of the brief named exactly: *"do not spend a large
implementation effort to save five minutes from a multi-hour generation run."* The
measurement says three.

**And the thing that would be given up is not nothing.** Per-request loading is not an
accident of the architecture; it is the mechanism behind a stated safety property. The
backend refuses `LoadStrategy.SHARED_BASE` outright, in code, with the reason spelled out:
reusing a loaded base *"requires proving that attaching and removing an adapter leaves no
residue, and this repository has not tested that."* The failure it guards against — a
candidate's adapter still attached to what the next call labels a baseline — is the single
most dangerous silent failure in a paired comparison, because it does not crash, it just
reports the wrong arm. Trading a structural guarantee for 2.5 % of wall time is a bad trade
in either direction, and it is a very bad one at this price.

**No production evaluation code was changed.** `GENERATION_SEMANTICS_CHANGED: NO`,
`SCORING_CHANGED: NO`, `SECURITY_SCANNING_CHANGED: NO`,
`RAW_RESPONSE_PERSISTENCE_CHANGED: NO`, `BODY_FREE_EVIDENCE_CHANGED: NO`.

Consequently the future S3I load counts are unchanged from history:

```
BASELINE_ARM_MODEL_LOADS_FUTURE:   36
CANDIDATE_ARM_MODEL_LOADS_FUTURE:  36
TOTAL_MODEL_LOADS_FUTURE:          72
TOTAL_GENERATIONS_FUTURE:          72
```

**If wall time ever does need attacking, the target is the output budget and the decode, not
the loader.** That is a separate decision with its own evidence, and S3F.2 already fixed
`max_new_tokens` at 512 for this measurement precisely so it stays one variable at a time.

---

## 7. D32 — the recorded `m62-defensive-eval v2` manifest hash does not reproduce

**This is the finding that would have cost a token.**

Preparing the S3I plan preview needed the held-out corpus, and only `v1` was materialised in
the runtime dataset root. Rebuilding `v2` from the tracked generator — the one condition
PROGRESS §18 permits — produced:

```
measured:  82b60bfdbea263eef3990eb6e49c2f2ca16e9b9e26ec8ac435f314b374279d60
recorded:  10ad2308391567eeaa043001835b0c77a02473b26d2f83c0fb54a32d885b9df0
```

**v1 is the control, and it passes.** Rebuilt in the same command sequence, `v1` reproduces
`0970600c677c89112db972c6024634aa871be92dee303db7f429c90967d3dd3b` — byte-identical to the
value S3F.2 recorded and PROGRESS §7 carries. So the generator, the authority chain, the
manifest hashing and the determinism-across-roots property are all sound. The problem is
specific to `v2`.

**The generator has not changed since the commit that created v2.** `git log` over
`jarvis/scripts/build_evaluation_corpus.py` shows exactly two commits, the newest being
`68ba078` — the S3F.2 commit that added `corpus_v2` itself.

**Checked against the historical code, in a temporary worktree at `68ba078`, into a fresh
root:**

| Version | S3F.2-era code (`68ba078`) | Current code (`4772a2c`) | Recorded |
|---|---|---|---|
| v1 | `0970600c…` | `0970600c…` | `0970600c…` ✅ |
| v2 | **`82b60bfd…`** | **`82b60bfd…`** | `10ad2308…` ❌ |

Three independent roots and two code versions all agree on `82b60bfd…`. **The tracked
generator has never produced `10ad2308…`**, and that digest corresponds to no other artefact
in the record either — not the v2 task-pack hash (`b4f9d6b1…`), not the promotion plan
(root-dependent), not v1.

**The corpus itself is fine, and that distinction is the whole point.** Everything S3F.2
asserted about v2's *content* reproduces exactly:

| Property | Recorded (S3F.2) | Measured |
|---|---|---|
| Candidates built / promoted / rejected | 36 / 36 / 0 | 36 / 36 / 0 |
| Splits | 12 / 12 / 12 | `ADVERSARIAL` 12, `HIDDEN_EVALUATION` 12, `SECURITY_REGRESSION` 12 |
| Families | 12 / 9 / 9 / 6 | `SAFETY_REFUSAL` 12, `STRUCTURED_REPORT` 9, `EVIDENCE_REQUEST` 9, `TOOL_CALL_SCHEMA` 6 |
| Leakage | CLEAN, 0 findings | `clean`, 0 findings |
| Parent link | derived from v1 | `parent_manifest_hash` = `0970600c…` = v1 |

So **D32 is a documentation defect, not a corpus defect.** The material is deterministic,
reproducible, leakage-clean and correctly derived from v1; only the recorded digest is wrong.

**Why it mattered.** `10ad2308…` is quoted as binding authority in PROGRESS §7, §14.11,
§18 and §19, in the S3F.2 doc, in S3G §12, in S3G.2 §5 and in the S3H doc §14. A future S3I
session would bind the corpus by that hash, get a refusal or a mismatch it could not explain,
and would be doing so at exactly the moment a fresh single-use `EVAL` token was about to be
spent — the D22 and D30 failure shape, arriving a third way.

**What was NOT done.** The corpus was not changed, not regenerated differently, not
"corrected" toward the recorded value, and no row was touched. `v2`'s identity is
`82b60bfd…` and that is now what the documents record. **Why S3F.2 wrote `10ad2308…` is not
established** — the material and the code both reproduce, so it was most likely a
transcription or a digest taken from the wrong object, but this session cannot prove which
and does not guess.

---

## 8. D33 — the declared per-task generation timeout is not enforced

Found while binding the generation policy for the plan preview.

`GenerationPolicy.timeout_s` (default 120, S3E.2 ran 300) is validated, serialised, and
travels inside `policy_hash` and therefore inside `parity_hash`. There is a
`BackendErrorCategory.TIMEOUT`, an `ArmScore.timed_out` field, a `timeout_rate` metric and a
`max_timeout_rate_increase` regression gate.

**The production backend never reads it.** A search for `timeout` across the whole evaluation
package returns the policy field, the error category, the metric, the gate — and, as the only
consumer, `backends/fake.py`, which synthesizes a timeout result for tests.
`backends/transformers_peft.py` contains no reference to it at all. No watchdog, no thread,
no signal, no `max_time` passed to `generate`.

**The historical record is consistent with that and hard to explain otherwise.** S3E.2
declared `timeout_s=300`, observed p95 latencies of **596.5 s and 704.4 s** — both far past
300 — and reported **0 timeouts**. Those three facts reconcile only if the timeout was never
applied.

**Consequences for S3I, stated so they cannot be missed:**

- a runaway generation has **no automatic bound**; the only real limit is the operator
  watching the clock;
- `timeout_rate` is **structurally vacuous** over a production run — it can only ever be 0 —
  and the `max_timeout_rate_increase` gate over it therefore decides nothing. This is exactly
  D28's shape: a metric whose transport does not exist;
- the S3I report must not cite `timeout_rate` as evidence of anything, and must record it
  `VACUOUS`, as D28 forces for `tool_call_validity_rate`.

**Deliberately not fixed here.** Adding enforcement would change run behaviour — tasks that
previously completed could now be cut off — and would put a second variable into the first
reasoning-disabled measurement, which is the exact trade S3F.2 refused over `max_new_tokens`
and S3G refused over D29. It is its own decision, and it needs the operator's, not mine.
`D33_STATUS: OPEN, NOT_FIXED`.

**A related trap for whoever configures S3I.** The default is **120 s** while S3E.2 ran at
**300 s**, and the measured median request latencies were **109.5 s and 123.0 s**. If the
timeout is ever made real, a config that silently takes the default would cut off around half
the candidate arm. The S3I configuration must state `timeout_s` explicitly rather than
inherit it.

---

## 9. Evaluation plan preview

```
EVALUATION_PLAN:       PREVIEW_PREPARED (config bound; not hashed into a plan this session)
EVAL_TOKEN_CREATED:    NO
EVAL_TOKEN_CONSUMED:   NO
```

An S3I evaluation configuration was constructed from values the contract already fixes —
binding nothing that required a new design decision:

| Field | Bound value |
|---|---|
| `evaluation_id` | `qwen3-06b-lora-quality-eval-001` |
| Baseline | `Qwen/Qwen3-0.6B` @ `c1899de289a04d12100db370d81485cdf75e47ca` |
| Candidate | run `qwen3-06b-lora-quality-live-001`, manifest `1f76ccfb…`, plan `122efc62…` |
| Dataset | `m62-defensive-eval` **v2** — identity `82b60bfd…` per D32 |
| `reasoning_policy` | **`disabled`** via `eligibility_generation_policy()` |
| `max_new_tokens` | **512** |
| `do_sample` | `False` (greedy) |
| `seed` | 11 (S3E.2's) |
| `max_input_tokens` | 4096 |
| `timeout_s` | **must be stated explicitly — see D33** |

**The plan hash was deliberately not fixed this session**, because two of its inputs are
unresolved: the corpus identity the plan binds is the subject of D32 (now corrected in the
documents, but the operator should ratify that before a token is derived against it), and
`timeout_s` enters `policy_hash` → `parity_hash` and must be an explicit decision rather than
an inherited default. Deriving a plan hash over an unratified input would produce exactly the
kind of authority that looks binding and is not.

**No token was created.** A confirmation is `EVAL:` + plan hash and is derived on demand; no
plan hash was fixed, nothing was issued, nothing was recorded and nothing was spent.

---

## 10. Estimated S3I runtime

Separated by component, with each labelled for what it is:

| Component | Estimate | Basis |
|---|---|---|
| Model load + release | **≈ 212 s (3.5 min)** | **measured this session**, 72 loads |
| Autoregressive generation | **≈ 2 h 15 min – 4 h+** | S3E.2 medians (109.5 / 123.0 s per request, load included) with a heavy tail (p95 596.5 / 704.4 s) |
| Scoring / comparison / artefacts | seconds to low minutes | deterministic, no model |
| **Total** | **≈ 2 h 20 min – 4 h+, estimated** | dominated entirely by decoding |

**Reasoning `DISABLED` may reduce this materially, and that is an expectation, not a
measurement.** Every S3E.2 response opened with a `<think>` block, 27 of 72 hit the 512
ceiling and 5 of 18 structured generations never left the reasoning block. Removing the
narration should shorten outputs and therefore decode time. **No model has ever generated
under `DISABLED` in this repository**, so the size of that effect is unknown, not estimated,
and the range above deliberately does not assume it.

---

## 11. Tests and gates

**No tracked source changed**, so per §47 of the brief no focused suite, no full inner suite,
no Ruff, no `compileall` and no Bandit were run — they gate source changes, and there are
none. The bounded qualification that does apply was run:

| Check | Result |
|---|---|
| Git verification | PASS — expected HEAD, `0 0`, master unchanged, clean |
| S3H adapter re-verification | PASS — `verify_completed_run` 0 problems, sha256 matches |
| Model-load lifecycle trace | PASS — per-request, 72 loads for 36 × 2 |
| Load-only benchmark, both arms | PASS — 6 real loads, **0 tokens generated** |
| Load identity proof (class, revision, adapter active, eval mode) | PASS |
| Eval corpus v1 reproduction (control) | PASS — `0970600c…` |
| Eval corpus v2 reproduction, 3 roots × 2 code versions | **MISMATCH vs the record → D32** |
| Timeout enforcement search across the evaluation package | **ABSENT → D33** |
| Temporary worktree used for the historical rebuild | removed; `git worktree prune` clean |
| `git diff --check` | PASS |
| Host-path / token scan over the changeset | PASS — no absolute host path, no Windows user path, no cache location, no `EVAL:`/`TRAIN:` token in either changed file |
| Secret scan (`core.redaction_policy.scan_for_leaks`) | PASS, finding named — one `reasoning` category in this document, the literal `<think>` in §10's prose describing S3E.2's responses. Hygiene under operator ruling **H4**, identical to what S3G, S3G.2 and S3H recorded. Not reworded to quiet the detector |

---

## 12. What was NOT done

- **No generation.** No `generate()` call on any model, no forward pass, no prompt built, no
  held-out task read by a model. `TOKENS_GENERATED: 0`.
- No `EVAL` plan hash fixed, no `EVAL` token created, none consumed.
- No change to the evaluation corpus, scoring, graders, thresholds, security vetoes,
  reasoning policy or `max_new_tokens`.
- No change to any production source file. No sessionization implemented.
- No fix for D33 — recorded, deliberately deferred.
- No training, no `TRAIN` token, no modification of the S3H adapter.
- No promotion, activation, registry mutation, merge, tag, release or version bump.
- No dependency installed (`psutil` was already present), no network contact.
- No absolute host path in any tracked file.

---

## 13. Limitations

1. **The load benchmark is three cycles per arm on one host.** Enough to separate ~2 s from
   ~110 s by two orders of magnitude; not a distribution.
2. **The historical total is a lower bound.** Summing medians gives 2 h 19 min while the p95
   tail was 596.5 / 704.4 s, so the true S3E.2 total was larger and the load fraction
   correspondingly smaller. The decision is insensitive to this — it only gets stronger.
3. **The RSS residue across cycles is measured but not diagnosed.** ~8–10 MB per cycle. It
   did not affect the decision and was not chased.
4. **D32's cause is not established.** Only that `82b60bfd…` reproduces and `10ad2308…` does
   not, under two code versions and three roots.
5. **D33 is open.** The timeout is unenforced and `timeout_rate` is vacuous.
6. **Nothing here says anything about candidate quality.** No held-out task has been put to
   any model. The candidate remains `TRAINED_UNEVALUATED`.

---

## 14. S3I readiness

```
S3I_READY: YES — with two conditions the operator must ratify (D32, D33)
```

| | Condition | Result |
|---|---|---|
| 1 | S3H adapter still verifies | PASS — 0 problems, sha256 matches |
| 2 | Eval corpus reproduces deterministically | PASS — **at `82b60bfd…`**, not the previously recorded digest (D32) |
| 3 | Corpus content unchanged | PASS — 36/36, splits 12/12/12, families 12/9/9/6, leakage clean |
| 4 | Reasoning `DISABLED` bound | PASS — `eligibility_generation_policy()` |
| 5 | `max_new_tokens` 512 bound | PASS |
| 6 | Scoring / graders / security unchanged | PASS — nothing touched |
| 7 | Body-free review evidence unchanged | PASS |
| 8 | Runtime load strategy qualified | PASS — per-request, measured, deliberately kept |
| 9 | Arm isolation | PASS — two distinct backend objects; per-request loads are stronger than per-arm |
| 10 | Task state isolation | PASS — the model is destroyed between tasks, so no state can carry |
| 11 | Zero generated tokens in S3I.0 | PASS |
| 12 | No `EVAL` token created or consumed | PASS |
| 13 | Plan buildable without blockers | **CONDITIONAL** — buildable once D32's corrected digest is ratified and `timeout_s` is stated explicitly (D33) |

**The two conditions are decisions, not engineering.** Ratify that `m62-defensive-eval v2` is
`82b60bfd…`, and state `timeout_s` explicitly in the S3I configuration knowing it is not
enforced. Neither requires code.

---

## 15. Next

**M62 S3I — first quality-candidate held-out eligibility evaluation**, under the S3G §12
contract and against the S3G §6 gates, with `timeout_rate` reported `VACUOUS` alongside
`tool_call_validity_rate` (D33 joins D28), a fresh `EVAL` plan and a single-use token
consumed exactly once.

It requires **explicit operator authorisation, which has not been given.**

**S3I is not started, and must not begin automatically.**

**ZERO GENERATED TOKENS. NO EVAL AUTHORITY CREATED OR CONSUMED.**
