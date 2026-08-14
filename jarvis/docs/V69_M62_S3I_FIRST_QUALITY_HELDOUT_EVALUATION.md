# V69 M62 S3I — First quality-candidate held-out eligibility evaluation

**Status: `BLOCKED` before EVAL authority creation. Nothing was generated. No token was
created. No token was consumed. The candidate remains `TRAINED_UNEVALUATED`.**

This document records an authorised evaluation that did not run, why it did not run, and the
defect it found instead. It is not a quality result and contains no eligibility verdict.

---

## 1. Authorisation

The human operator authorised **exactly one** eligibility-grade held-out evaluation
generation: 36 baseline + 36 candidate = 72 real generations of the pinned Qwen3-0.6B
baseline and the immutable S3H quality adapter over the qualified `m62-defensive-eval v2`
corpus, against the predeclared S3G §6 gates, under one single-use `EVAL:` authority
consumed exactly once. No retry, no promotion, no registry mutation, no merge, no tag, no
release, no version bump.

The authorisation also ratified the two conditions S3I.0 left open — **D32** and **D33**.

**The authorisation was not exercised.** The pre-token gate (§32 of the brief) did not pass,
and the brief's own rule applies: *if any blocker remains, stop before EVAL token creation;
do not work around it.* Two independent blockers were found. Both are recorded below.

---

## 2. Outcome

```
S3I_LIVE_EVALUATION:        BLOCKED
S3I_PRETOKEN_GATE:          BLOCKED
PLAN_BLOCKER_COUNT:         2
EVALUATION_PLAN_HASH:       NOT_DERIVED   (blocked before plan construction)
EVAL_TOKEN_CREATED:         NO
EVAL_TOKEN_CONSUMED:        NO
EVAL_ATTEMPTS:              0
TOKENS_GENERATED:           0
HELDOUT_TASKS_EXECUTED:     0
MODEL_FORWARD_PASSES:       0
CANDIDATE_STATUS:           TRAINED_UNEVALUATED   (unchanged)
CANDIDATE_ELIGIBILITY:      NOT_ESTABLISHED
SOURCE_CHANGED:             NO
RETRY_AUTHORIZED:           N/A — no authority was spent
```

Because no generation occurred, **every** S3G gate — SV-1…SV-9, QG-1…QG-4, FG-1…FG-4,
OG-1…OG-7 — is `NOT_EVALUATED`. None is `PASS`, none is `FAIL`. There is no paired sample,
no delta, no confidence interval and no security finding, and none of those may be inferred.

---

## 3. Blocker B1 — the generation runtime is absent on this host

The evaluation requires loading real weights 72 times through the production
`transformers_peft` backend. That backend needs `torch`, `transformers` and `peft`.

| Interpreter | State on this host |
|---|---|
| System `python3` (3.13.14) | `torch`, `transformers`, `peft` **all absent**; `jsonschema` present |
| `.venv` | **Windows** virtual environment — `Scripts/*.exe`, `Lib/`, no POSIX `bin/python` |
| `.venv-training-smoke` | **Windows** virtual environment; holds torch 2.13.0 / transformers 5.14.1 / peft 0.20.0 as **Windows binaries** (`c10.dll`, `libiomp5md.dll`), built for `Python312` |

`.venv-training-smoke` is the interpreter PROGRESS §15 names as authoritative for every live
M62 run, and the S3H adapter manifest records the exact runtime that produced it:

```
package_versions: {"peft": "0.20.0", "torch": "2.13.0+cpu", "transformers": "5.14.1"}
```

That environment is a Windows tree and cannot execute on this Linux host. The repository
state is a copy: the model cache the brief names by its Windows path is absent, though the
same reviewed cache is present at the host-equivalent location with the correct immutable
revision (§5).

**Why this was not worked around.** Installing torch would violate a stated non-negotiable
invariant (PROGRESS §3: *"No automatic dependency installation, no global pip mutation.
Optional training/eval packages live only in an ignored isolated environment"*), and PROGRESS
§19 lists *"installing dependencies or touching the global environment"* among the operations
requiring **new explicit operator authorisation**, which this brief does not grant. It would
also have replaced the runtime S3I.0 qualified — a different Python, a different torch build,
a different platform — putting a second and much larger variable into the first
reasoning-disabled measurement, which is precisely the trade S3F.2 refused over
`max_new_tokens`, S3G refused over D29 and S3I.0 refused over D33.

---

## 4. Blocker B2 — D34: the `v2` corpus digest depends on build lineage, not on content

This is the finding that would have cost the token, and it reopens D32.

The brief requires the corpus to be reproduced and verified before plan construction. It was.
The result contradicts the ratified D32 ruling.

### 4.1 What was measured

Tracked generator `jarvis/scripts/build_evaluation_corpus.py`, unmodified, at HEAD
`7b93883`, on this host:

| Build | `manifest_hash` | `parent_manifest_hash` |
|---|---|---|
| `v1` into a fresh root (control) | `0970600c677c89112db972c6024634aa871be92dee303db7f429c90967d3dd3b` | `genesis` |
| `v2` into a fresh root | **`10ad2308391567eeaa043001835b0c77a02473b26d2f83c0fb54a32d885b9df0`** | `genesis` |
| `v2` into a fresh root, repeated | **`10ad2308…`** (identical) | `genesis` |
| `v2` into a root **already containing `v1`** | **`82b60bfdbea263eef3990eb6e49c2f2ca16e9b9e26ec8ac435f314b374279d60`** | `0970600c…` (= `v1`) |

The v1 control reproduces its frozen digest exactly, matching both the record and S3I.0.

### 4.2 The mechanism

`v2`'s manifest binds `parent_manifest_hash`. That field resolves to `v1`'s digest **when
`v1` exists in the target dataset root**, and to `genesis` when it does not. The corpus
material is identical either way:

```
adversarial.jsonl          8738652dce7971f6176c84919c2496d0b02d970f65726d6d8da0d62e16b11e5a
security_regression.jsonl  af1dc5bb7235bc041920883f9cb72269944ae5a1f028d77c66f1790b1151614f
hidden_evaluation.jsonl    d75fb843e6cd2f1791fed0cef6a8ee90b633206806fcb03031233ff69662f74a
```

All three shard files are **byte-identical** across the two lineages. `diff -r` over the two
built `v2` trees reports exactly one differing file: `manifest.json`, and within it exactly
one differing field before the digest — `parent_manifest_hash`.

### 4.3 What this means for D32

D32 concluded that `82b60bfd…` is the corpus's identity, that `10ad2308…` *"reproduces under
no code version and no root"*, and that the discrepancy was a **documentation** defect. The
first claim is conditional and the second is falsified:

- `10ad2308…` **is** a reproducible output of the tracked generator — it is what `v2` builds
  to standalone, deterministically, twice here. It is almost certainly what S3F.2 recorded,
  because S3F.2 built `v2` as a new version rather than rebuilding `v1` first.
- `82b60bfd…` is equally reproducible — it is what `v2` builds to when `v1` is materialised
  in the same root first. S3I.0 rebuilt `v1` as its control in the same command sequence,
  which is exactly the condition that forms the parent link; its own evidence table records
  `parent_manifest_hash = 0970600c… = v1`, while the build here records `genesis`.

Neither digest is wrong and neither corpus is wrong. **The corpus identity is not a function
of the corpus alone.** Two evaluations binding "`m62-defensive-eval v2`" can legitimately
carry different manifest hashes for byte-identical held-out material, depending only on what
else happened to be present in the dataset root when the corpus was built.

This is recorded as **D34**, and it is the D22 / D30 / D32 wasted-authority shape arriving a
fourth way: a plan binds a digest, the digest is re-derived at execution against a root in a
different state, and the mismatch surfaces at the moment a single-use token is being spent.

### 4.4 Why this blocks

The ratified D32 ruling instructs S3I to bind `82b60bfd…` and states that the corpus
*"reproducibly generates the same `82b60bfd…` digest across multiple roots"*. On this host,
into a fresh root, it does not — it generates `10ad2308…`. Binding a plan to a digest that
the execution environment does not reproduce is the exact failure D32 was raised to prevent.

The corpus **content** is fully verified and is not in question: 36 records, splits
`hidden_evaluation` 12 / `security_regression` 12 / `adversarial` 12, families
`safety_refusal` 12 / `structured_report` 9 / `evidence_request` 9 / `tool_call_schema` 6,
leakage verdict `clean` with 0 findings, fixed seed, fixed `created_at_utc`, deterministic
across roots. Not one byte was altered.

**Not fixed here.** Choosing the corpus's canonical lineage is a decision about dataset
identity, not an engineering detail, and changing it would move the digest every artefact in
the record binds.

---

## 5. What was verified and holds

| Item | Result |
|---|---|
| Branch | `jarvis-v69-m62-training-gym` |
| Starting / final HEAD | `7b938833b6136800daa5a436f3a8e3c038293b91` (unchanged — no commit precedes this doc) |
| `origin/<feature>` divergence | `0  0` |
| `origin/master` | `3705114228edef2f665be349c5c4429b7b16777a` — untouched |
| Candidate | `qwen3-06b-lora-quality-live-001`, `TRAINED_UNEVALUATED` |
| Adapter SHA256 | `43213035c15cd38928d2d6a3bdbd9af96872a954801c6bfd0a9b82a8e22ac858` ✅ |
| Adapter manifest hash | `1f76ccfbb8efc566c293ab6430d041dd24748035ed48aec6552d1e3bac24699f` ✅ |
| Artifact tree hash | `00aa57bbbe7f0af73501dae2330fb0b08682ede813843f92b26681ec77d659b6` ✅ |
| `verify_completed_run` | **PASS — 0 problems** |
| Adapter bindings | plan `122efc62…`, config `b5f63cd8…`, dataset `9bbac2f0…`, chat template `a55ee1b1…` — all reproduce |
| Base model / revision | `Qwen/Qwen3-0.6B` @ `c1899de289a04d12100db370d81485cdf75e47ca` |
| Reviewed cache | present at the host-equivalent root; `c1899de2…` is the only revision cached; weights + tokenizer files present |
| Predeclared gates | S3G §6 read and reproduced unchanged — SV-1…SV-9, QG-1…QG-4, FG-1…FG-4, OG-1…OG-7 |
| Scoring / graders | unchanged, not touched |
| Corpus content | verified intact (§4.4) |

The adapter was not modified, loaded, merged, re-saved, renamed or requantised.

### 5.1 Working tree

`git status` is **not clean**: 183 tracked files differ. The difference is **line endings
only** — the working tree holds CRLF where the index holds LF, `git diff --ignore-all-space`
is empty, and no file's content differs by a single character. It is an artefact of the
repository having been copied from the Windows host where M62 ran.

Nothing was reset, restored, cleaned, stashed, discarded or force-pushed. The 182 files this
milestone does not legitimately touch were left exactly as found; only the two documentation
files this milestone must write were normalised to the index's LF form, so their commits show
their real content change and nothing else.

---

## 6. D33 — ratified, bound, and still unenforced

The operator ruling is recorded and unchanged:

```
S3I_TIMEOUT_S:            300
TIMEOUT_ENFORCEMENT:      NOT_IMPLEMENTED
TIMEOUT_RATE:             VACUOUS_NOT_QUALIFIED
D33_STATUS:               ACCEPTED_KNOWN_LIMITATION
```

`timeout_s = 300` was to be bound to the S3I plan. No plan was constructed, so nothing bound
it. D33 is untouched: no asyncio timeout, no subprocess bound, no reinterpretation of long
calls, no enforcement of any kind was added. `timeout_rate` remains structurally vacuous and
must never be cited as eligibility evidence.

---

## 7. Limitations that remain exactly as they were

- **D28** — the production backend populates no `proposed_tool_calls`.
  `TOOL_CALL_CAPABILITY: NOT_QUALIFIED`, `tool_call_validity_rate` `VACUOUS`. **OPEN.**
- **D29** — `looks_like_refusal` recognises sixteen literal phrasings the held-out JSON
  refusal targets do not contain. Not modified, not worked around. **OPEN.**
- **D33** — declared timeout never enforced; metric vacuous. **OPEN.**
- **D34** — corpus identity depends on build lineage (§4). **NEW, OPEN.**
- **D32** — must be reopened in light of D34: its ruling rests on a premise this session
  falsified.

Nothing in S3E.2, S3F, S3F.1, S3F.2, S3G, S3G.1, S3G.2, S3H or S3I.0 was rewritten,
re-scored, re-labelled or corrected. Historical evidence stands as written.

---

## 8. Explicitly not done

No generation. No EVAL plan. No EVAL token created or consumed. No promotion, no activation,
no Model Registry mutation, no proposal artefact, no merge, no tag, no release, no version
bump. No dependency installed, no global environment touched, no network contact, no model
download. No raw response body exists, so none was persisted. No adapter mutation. No retry.
No source file changed.

---

## 9. NEXT

The candidate is still unevaluated and the evaluation is still authorised in principle. Two
operator decisions stand between here and a run, and neither is engineering Claude may do
unilaterally:

1. **Resolve D34 / reopen D32** — decide which lineage is `m62-defensive-eval v2`'s canonical
   identity (`10ad2308…` standalone, or `82b60bfd…` parented on `v1`), and whether the corpus
   identity scheme should bind a parent link at all. Every document currently carrying either
   digest follows from that decision.
2. **Provide an execution host** — either run S3I on the Windows host where
   `.venv-training-smoke`, the reviewed cache and the S3H adapter runtime already exist and
   where S3I.0 measured the load strategy, or explicitly authorise provisioning an equivalent
   isolated environment here, accepting that a changed runtime is a changed measurement.

Until both are settled, the pre-token gate cannot pass and no `EVAL:` authority may be
derived. The one-run authorisation is **unspent** and remains available.

```
MODEL_PROMOTION:          NOT_AUTHORIZED
MODEL_REGISTRY_MUTATED:   NO
CANDIDATE_ELIGIBILITY:    NOT_ESTABLISHED
```
