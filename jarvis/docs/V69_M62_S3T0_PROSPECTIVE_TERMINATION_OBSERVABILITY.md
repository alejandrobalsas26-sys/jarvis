# V69 M62 S3T.0 — Prospective body-free termination observability

**Status** COMPLETE · observability only · no scientific activity
**Subject** `jarvis/training_gym/evaluation/scoring.py`, `…/score_evidence.py`
**Defect filed** **D43** — FIXED (observability only)
**Control plane** generation 7 → generation 8
**Scope** PROSPECTIVE. Nothing historical is recovered, reinterpreted or backfilled.

This document is **body-free**. It contains no held-out prompt, no held-out target, no
response text, and no excerpt of either. Every example below is synthetic and written
here.

---

## 1 — The S3S.1 premise failure this milestone answers

S3S.1 asked whether candidate003's one eligibility-deciding structured-output failure was

> **A.** the first JSON document never became valid or closed

or

> **B.** a valid first JSON document completed and additional output followed.

It could not answer, and the reason was not restraint — it was **absence**. The spent
eval-v4 response bodies were never persisted at all, so there was nothing to re-read
under any authorisation. That privacy property is deliberate and **stays**.

But the bodies were not the only thing that could have answered the question. At
evaluation time the parser **already held**, transiently, everything needed to
distinguish A from B without keeping a single character of the response — and threw it
away.

### The parser discriminator gap, reproduced before anything was changed

`structured_output_detail` catches `json.JSONDecodeError` and reads `exc.msg` and
`exc.lineno` into `ArmScore.notes`. `ArmScore.notes` is **never persisted** — correctly,
because a note about a response can quote it. Only the closed note code
`structured_output_not_valid_json` reached disk, and that one code covers every parse
failure there is.

Reproduced at the starting commit, on synthetic responses, through the production scorer:

| synthetic response | `ArmScore.notes` (in memory, never persisted) | persisted `note_codes` |
|---|---|---|
| `{"severity": "low"` | `not valid JSON (Expecting ',' delimiter at line 1)` | `structured_output_not_valid_json` |
| `{"severity": "lo` | `not valid JSON (Unterminated string starting at at line 1)` | `structured_output_not_valid_json` |
| `{"severity": "low"} …` | `not valid JSON (Extra data at line 1)` | `structured_output_not_valid_json` |

The persisted evidence records for hypothesis A and hypothesis B were **identical in
every readable field**. The only byte that differed was `response_sha256` — a digest of
a body nobody kept, which discriminates nothing without the body.

```
JSON_PARSE_DISCRIMINATOR_COMPUTED   (before)   YES
JSON_PARSE_DISCRIMINATOR_PERSISTED  (before)   NO
```

**This milestone does not recover candidate003 and does not reinterpret it.** The
diagnosis is prospective: the next evaluation that hits this failure will record which
of A or B happened. This one still cannot say.

---

## 2 — Why raw bodies remain prohibited

Nothing here weakens the standing rule. `RAW_RESPONSE_PERSISTED` stays **NO**.

The S3F.2 reasoning is unchanged and is reaffirmed: persisting response text is a
different authorisation with a different privacy cost, and it is not obtained by quietly
widening an observability patch. What S3F.2 established — that *almost everything a
reviewer needs is body-free by construction and was being discarded* — is exactly what
this milestone applies to the parser. The fix is to persist the **class** of a failure,
never the sentence that describes it and never the text it describes.

### `exc.msg` is not reliably body-free, so it is not what gets persisted

This is the concrete reason a closed vocabulary exists rather than a raw string field.
CPython's **pure-Python** decoder formats the offending character *into* the message:

```
"Invalid control character {0!r} at"      →  Invalid control character '\n' at
"Invalid \\escape: {0!r}"                 →  Invalid \escape: 'q'
```

The **C accelerator** omits the character and raises `Invalid control character at` and
`Invalid \escape`. Which spelling a run produces is a property of the interpreter build,
not of the evaluation. Persisting `exc.msg` would therefore leak one character of the
response **on some hosts and not others** — a conditional leak, which is the worst kind,
because a body-free scan that passed on the C-accelerated host would say nothing about
the other. So the message is classified and dropped, and only the class survives.

---

## 3 — The diagnostic contract

### 3.1 Closed parser-error vocabulary

`scoring.JsonParseErrorKind`, derived from the messages this repository's parser actually
raises — not from a guess about Python:

| member | persisted value | means |
|---|---|---|
| `EXTRA_DATA` | `extra_data` | **a complete JSON document parsed, and non-whitespace data followed** |
| `UNTERMINATED_STRING` | `unterminated_string` | a string opened and never closed |
| `EXPECTING_VALUE` | `expecting_value` | no value where one was required |
| `EXPECTING_PROPERTY_NAME` | `expecting_property_name` | an object key that was not a quoted string |
| `EXPECTING_DELIMITER` | `expecting_delimiter` | a missing `:` or `,` — the truncated-container signature |
| `ILLEGAL_TRAILING_COMMA` | `illegal_trailing_comma` | a comma before a closing brace or bracket |
| `INVALID_ESCAPE` | `invalid_escape` | a bad `\x` or `\uXXXX` |
| `INVALID_CONTROL_CHARACTER` | `invalid_control_character` | a literal control character inside a string |
| `OTHER_JSON_PARSE_ERROR` | `other_json_parse_error` | **fail-safe.** Anything unrecognised, stored as this and nothing else |

**Not exhaustive over Python, and not claimed to be.** `ILLEGAL_TRAILING_COMMA` does not
exist on interpreters older than CPython 3.13, where the same input classifies as
`EXPECTING_PROPERTY_NAME`. Matching is by **prefix** on the message stem, so the C and
pure-Python spellings of the two body-quoting messages collapse to one class and whatever
follows the stem is never read. A message this build has never seen becomes
`OTHER_JSON_PARSE_ERROR` **without storing any part of it**.

### 3.2 The EXTRA_DATA discriminator

`EXTRA_DATA` is the answer S3S.1 wanted and could not have. It is raised by CPython at
`decoder.py:348`, **after** `raw_decode` has successfully returned a whole document, when
the remaining text is not whitespace. It is therefore materially different from every
other member, all of which mean the first document never closed.

No second parser was written and no second parse is performed. The class comes from the
`JSONDecodeError` that the **already-existing** `json.loads` attempt raises. A
hand-written scan for "did the document close" would be a second opinion about the same
bytes, and the two would eventually disagree about a response nobody can re-read.

The three entry points are now one authority and two projections:

```
structured_output_diagnosis(text) -> (value, problem, code, diagnosis)   ← parses
structured_output_detail(text)    -> (value, problem, code)              ← projection
structured_output(text)           -> (value, problem)                    ← projection
```

`json.loads` appears **once** in the whole chain, and a test asserts it.

### 3.3 Persisted fields

Five fields, added to `ArmScore` and to the `baseline-scores.jsonl` /
`candidate-scores.jsonl` review-evidence record:

| field | type | not-applicable value |
|---|---|---|
| `json_parse_error_kind` | closed enum value (string) | `null` |
| `json_parse_error_line` | int, 1-based | `null` |
| `json_parse_error_column` | int, 1-based | `null` |
| `json_parse_error_position` | int, 0-based character offset | `null` |
| `response_unique_char_ngram_ratio` | float in `(0, 1]` | `null` |

The four parse fields are **all-or-nothing**: `ArmScore` refuses a class without a
location or a location without a class, because either would describe a parse attempt
that half happened. They are `null` together for every outcome that is not a parse
*failure* — a document that parsed, an empty response, a response that never left its
reasoning block, and a task whose family requests no structural check. A zero there would
publish a position the parser never reached.

### 3.4 The one generic runaway diagnostic

S3S.1 also established that **chars-per-token** separates a single degenerate token from
a normal-density runaway, but nothing persisted separated a normal-density runaway that
**repeats itself** from one that keeps saying **new things**.

Chars-per-token needed no new field: `response_chars` and `output_tokens` are both already
persisted in the results artefact, so the ratio has always been derivable. Adding it would
have been redundant. **The only real gap was repetition**, so exactly one metric was added:

```
response_unique_char_ngram_ratio  =  distinct 16-character shingles / total shingles
```

Measured over the **raw** response, reasoning block included, because a runaway that loops
inside `<think>` is still a runaway.

**Why character shingles rather than word n-grams.** This evaluation's degenerate
emissions are frequently whitespace-free. A structured-output task that loops
`{"a":1},{"a":1},…` is *one* whitespace token and has *no* word trigrams at all — a word
tokeniser would report nothing on exactly the failure shape these tasks produce. Character
shingles score it `0.002`. A 16-character window is wide enough that ordinary prose repeats
almost no window by chance, so a low value means repetition rather than English.

Measured on synthetic fixtures:

| synthetic emission | ratio |
|---|---|
| a single character repeated 4,000 times | `0.000251` |
| one sentence repeated 200 times | `0.005009` |
| `{"a":1},` repeated 500 times | `0.002008` |
| 200 distinct novel observations | `0.839694` |

Two to three orders of magnitude of separation, with **no threshold defined anywhere**.
Calling a given ratio "repetition" is a judgement about a run, not a property of a
response, and it belongs to later analysis — never to evaluation eligibility.

The candidate diagnostics that were **considered and rejected** for the minimum set:
`tail_repetition_ratio`, `whole_response_repetition_ratio`, `line_repetition_ratio` and
`longest_repeated_ngram_run_length`. Each is either subsumed by the shingle ratio (a
repeated line is a repeated shingle) or fails on the whitespace-free case (`line_*`), and
`longest_repeated_ngram_run_length` adds a second number without adding a second question.
One metric closes the gap; more would be decoration.

---

## 4 — Privacy analysis

Every proposed field was asked: could this reveal raw text, a long substring, token IDs,
rare literal secrets, prompt content or target content?

| field | raw text | substring | token IDs | secrets | prompt | target |
|---|---|---|---|---|---|---|
| `json_parse_error_kind` | no — one of nine fixed strings, none derived from input | no | no | no | no | no |
| `json_parse_error_line` / `_column` / `_position` | no — integers | no | no | no | no | no |
| `response_unique_char_ngram_ratio` | no — one scalar | no | no | no | no | no |

**On the offsets.** They say how far the parser got. The response *length* that bounds
them is already published as `response_chars` in the results artefact, which this
evaluation has always written, so an offset discloses strictly **less** than a field that
predates this milestone. They describe the response only; they carry nothing about the
prompt or the target.

**On the ratio.** The shingles are counted inside `unique_char_ngram_ratio` and discarded;
only their ratio leaves the function. A cardinality cannot be inverted into the text it
was measured over. The shingles are compared as substrings rather than through `hash()`,
whose per-process randomisation would let two runs disagree about how many collided —
determinism is a requirement, not a side effect.

**Not persisted, by construction:** token sequences, token IDs, n-gram strings, repeated
substrings, first/last text, parser context snippets, `exc.msg`, `exc.doc`, or any
fragment of a response, prompt or target.

```
RAW_RESPONSE_PERSISTED   NO
```

---

## 5 — Schema and version effect

The artefact that **owns** these diagnostics is the body-free review evidence, so that is
the one that is bumped. This follows the D38 precedent exactly.

| identity | before | after |
|---|---|---|
| `SCORE_EVIDENCE_VERSION` | `m62.evaluation_score_evidence.2` | `m62.evaluation_score_evidence.3` |
| `SCORING_VERSION` | `m62.evaluation_scoring.5` | `m62.evaluation_scoring.6` |
| `EVALUATION_BACKEND_PROTOCOL_VERSION` | `m62.evaluation_backend.1` | **unchanged** |

`SCORING_VERSION` moves because the fields are in `ArmScore.to_dict()`, and therefore in
`score_hash()`. That is deliberate: a digest that did *not* cover them would let a
diagnosis be edited after the score it describes was sealed.

The backend protocol is untouched. The diagnostics live in the scoring layer — the same
placement D38 chose for `output_budget_exhausted`, which likewise derives from
generation-side state without changing the generation-side record. The results artefact
`baseline-results.jsonl` is byte-identical in shape and content.

### Historical compatibility — proved, not assumed

`SCORE_EVIDENCE_FIELDS` is an **allowlist**: a record that omits a key is accepted, a
record that carries an undeclared one is refused. A `.1` record (no
`output_budget_exhausted`) and a `.2` record (no S3T.0 fields) both still read, and both
are asserted to gain **no** invented value. **Nothing is backfilled.** A diagnosis nobody
computed must not be manufactured for a response nobody kept.

S3Q artefacts, the candidate003 receipt and report, the measurement witness, eval-v4 and
eval-v5 are **untouched**. Neither the receipt builder nor the control-plane verifier
reconstructs an `ArmScore` or recomputes a `score_hash`; both read the sealed persisted
files and compare the digests those files already carry. Historical verification is
unaffected.

### Receipt v3

Audited, **not changed**. The diagnostics are diagnostic-only and are deliberately not
made load-bearing for historical receipt verification. Where a future receipt binds an
evidence-file hash, it will cover them transitively, which is the correct amount of
binding. No receipt v4 was created.

---

## 6 — Policy-hash effect

Re-derived from the code, before and after, and compared against the pristine starting
commit in a detached worktree:

| identity | before | after |
|---|---|---|
| `generation_policy_hash` | `e63cf7ed…` | **same** |
| `grader_policy_hash` | `20595792…` | **same** |
| `metric_policy_hash` | `e07dd133…` | **same** |
| `gate_policy_hash` | `e5003319…` | **same** |
| `statistical_policy_hash` | `663ebf65…` | **same** |
| `family_policy_hash` | `580fbe91…` | **same** |
| `resource_policy_hash` | `04863003…` | **same** |
| `EvaluationPolicySet.policy_hash` | `eae948cc…` | **same** |

**Every policy identity holds.** No policy binds an artefact's shape, and a test asserts
that none of `GraderPolicy`, `MetricPolicy` or `GatePolicy` references
`SCORING_VERSION` or `SCORE_EVIDENCE_VERSION` — so if that ever stops being true, the
identity must move and be **reported**, not pinned.

Note that D38 *did* move `metric_policy_hash`, because D38 added a **metric**. S3T.0 adds
no metric, no statistic and no gate, so it moves nothing. That contrast is the check that
the placement is right.

```
GENERATION_CHANGED    NO
SCORING_CHANGED       NO
GATES_CHANGED         NO
ELIGIBILITY_CHANGED   NO
OBSERVABILITY_IS_GATE NO
```

---

## 7 — Non-vacuity

### JSON — `PASS`

Every class is exercised through the production scorer on **substantially different**
synthetic literals, not one string mutated, so no test is coupled to a particular
spelling:

- valid JSON (six shapes: object, array, nested, padded, fenced, empty array) → no parse
  error, all four fields `null`;
- truncated container (four shapes) → a canonical **non-`EXTRA_DATA`** class;
- truncated string (three shapes) → `UNTERMINATED_STRING`;
- a completed document followed by more output (four shapes) → **`EXTRA_DATA`**;
- invalid delimiter, invalid property name, invalid escape, invalid control character,
  trailing comma, no value → each on its canonical class;
- location fields checked **against Python's own** `lineno`/`colno`/`pos` for the same
  input, and against a multi-line fixture that fails on line 4, so a location that always
  said line 1 would fail;
- determinism asserted across repeated calls.

A dedicated test asserts that eleven routine malformations produce **at least seven
distinct** classes and that **none** of them falls through to `OTHER_JSON_PARSE_ERROR` —
a vocabulary that collapsed everything into the fallback would discriminate nothing.

### Privacy — `PASS`

Using a unique synthetic canary inside a synthetic unterminated-string response:

| check | result |
|---|---|
| raw response string in the record | **0** |
| eight-word response shingles in the record | **0** |
| unique synthetic canary in the record | **0** |
| token IDs | **0** |
| parser source snippets / exception prose (`Extra data`, `Expecting`, `Invalid`, `JSONDecodeError`, `line 1`, `char `) | **0** |
| production `assert_no_private_content` scanner over the whole record | **PASS** |

The privacy tests are **not vacuous**: the same fixture is asserted to have actually
produced `json_parse_error_kind == "unterminated_string"`, so the record being clean is
not the record being empty.

### Diagnostic metric — `PASS`

Synthetic repeated emission `< 0.05`, synthetic novel continuation `> 0.5`, separated by
more than 20×. The whitespace-free degenerate shapes a word tokeniser would miss both
score `< 0.01`. Determinism across eight calls. Source scanned for `tokenizer`,
`AutoTokenizer`, `torch`, `transformers`, `from_pretrained`, `encode(` and `hash(` —
**none present**. Source scanned for `threshold`, `is_repetitive`, `degenerate_if` —
**none present**, and `gates.py` is scanned for the field name — **absent**.

### Behavioural equivalence — `PASS`

Twenty synthetic fixtures scored through the production path at the starting commit and
again at the final implementation state, comparing **every preexisting field**:

```
cases compared                   20
preexisting field differences     0
```

Covering: `status`, `reward`, `refusal`, `schema_valid`, `json_parseable`,
`evidence_findings`, `tool_call_*`, `security_findings`, `hygiene_findings`,
`grader_statuses`, `missing_graders`, `blocking`, `severity`, `latency_ms`,
`output_tokens`, `truncated`, `output_budget_exhausted`, `timed_out`, `empty`,
`note_codes`, `response_sha256`, the full in-memory `ArmScore.notes` prose, **and the
entire results-artefact record** including `result_hash`.

The **only** intentional movements are the five added fields and the two schema
identities. `score_hash` changes as a function of schema identity — by design, since it
must cover what it describes — and a test asserts the two hypotheses now differ in
`score_hash` as well as in the readable class.

A per-fixture test additionally pins that `set(record) − pre-S3T.0 fields` is **exactly**
the five new names, so a sixth field cannot arrive unannounced.

### S3Q ceremony regression — `PASS`

`1268 passed · 4 skipped · 0 failed` across the ceremony, preflight, runner, plan, store,
execution and receipt suites. Token-silent preflight, plan consumption, holdout commit
position, retry semantics, terminal durability and body-free result persistence are all
unchanged — the patch touches neither the runner nor the backend.

---

## 8 — Three rescoped assertions

Three sealed assertions moved. All are recorded here rather than amended away, and all
follow the documented pattern (PROGRESS §10): **an assertion that compares a sealed
milestone's property against live state also asserts, silently, that no later milestone
exists.**

1. **`test_the_evidence_record_carries_the_verdict_and_no_body`** (D38 suite) asserted
   that `response_sha256` was the *only* `response`-prefixed key. That encoded a passing
   fact about D38's field list, not the property it owns — **no field may carry the
   response body**. Rescoped to check the **values**: every `response`-shaped field must
   be a 64-character hex digest or a number, never a string that could be text. Strictly
   stronger than the name it replaces.

2. **`test_s3q0_changed_no_grader_metric_statistic_or_gate_source`** diffed S3Q.0's
   starting commit against the **working tree**, so it asserted that nothing in the whole
   future would ever touch those files. The property it owns is about **S3Q.0**. Rescoped
   to S3Q.0's own range, `05c043b3 … 4f683f78`, making the claim exact and permanent
   instead of decaying into a freeze on every later milestone — plus a guard that the
   diff is non-empty, so it cannot become vacuous.

3. **`test_s3s_changed_no_evaluation_policy_grader_or_gate_source`** (S3S suite) did the
   same against the working tree — and its own docstring said what to do about it: *"it
   is pinned to a closing commit the moment S3S closes, exactly as S3N's equivalent
   was."* S3T.0 is the first milestone after that arc closed, so the instruction is
   carried out. The range is now `f9d25fd2 … bf83cf52`, which spans the **whole** S3S arc
   — S3S itself (`e52129c`), the generation-7 recording and its two rescopes (`c0d6ffd`),
   and the S3S.1 body-free audit (`bf83cf5`). None of the three touched
   `jarvis/training_gym/` **at all**, which is a stronger statement than the working-tree
   form made, plus the same non-vacuity guard.

No rescope weakens a property; two of the three are strictly stronger and the third
executes an instruction the sealed test left for exactly this moment. What a *later*
milestone may change is that milestone's question, answered by its own
behavioural-equivalence evidence.

---

## 9 — D38 relationship, and D43

**D38 is not redefined.** It remains `FIXED (observability only)`, `D38_IS_GATE: NO`, and
it remains specifically about output-budget exhaustion beside the unchanged
input-truncation metric.

This is a **different** signal, in a different module, costing a different answer, so it
gets the next available ID rather than being folded into D38 — extending D38's entry
would change what D38 means.

> **D43** · FIXED (observability only) · A JSON parse failure persisted one closed note
> code, so `EXTRA_DATA` — a complete document followed by more output — was
> indistinguishable in the evidence from a document that never closed. The parser held
> `msg`/`lineno`/`colno`/`pos` and discarded them. Fixed **prospectively** with a closed
> body-free class plus location, and one repetition scalar. **No gate reads any of them
> and none may be added without a separate operator decision.** Nothing historical is
> backfilled.

The register's `is_gate: false` convention applies. D43 is not forced for numbering: it
records a real repository observability gap, now fixed, and it carries a standing
prohibition a future session must not silently drop.

---

## 10 — Limitations

1. **It is prospective and only prospective.** candidate003 is not recovered. Its one
   structured failure remains undiagnosed between hypotheses A and B, permanently, because
   the body it would need was never written.
2. **The vocabulary is not exhaustive over Python.** It is derived from CPython 3.13,
   which is this repository's runtime. On an older interpreter `ILLEGAL_TRAILING_COMMA`
   never fires and its inputs classify as `EXPECTING_PROPERTY_NAME`. Unknown messages fail
   safe to `OTHER_JSON_PARSE_ERROR`, so nothing leaks — but a class count is not a
   cross-version guarantee.
3. **The ratio is a shape statistic, not a semantic one.** A response that repeats itself
   in *meaning* while varying its wording will score high. It separates surface
   degeneracy from novel surface text, which is the question S3S.1 posed, and no more.
4. **No threshold exists, deliberately.** Nothing in the repository classifies any ratio
   as "repetition". Any future analysis that wants to must define its own and say so.
5. **`EXTRA_DATA` names a failure; it does not excuse one.** A response that emitted a
   valid document and kept going still fails the structural grader exactly as before. A
   test pins this.
6. **D28, D29, D33 and D39 are untouched** and still bind. This milestone measured none
   of them.

---

## 11 — NEXT

**STOP.**

A new Claude Code session, for an explicit **operator ruling** on whether M62 continues
and on which **single** candidate004 training axis may supersede the generation-7
`ruled_out` entries.

Not done here, and not to be done as a rider:

- designing candidate004, or selecting LR, rank, alpha, modules or a corpus intervention;
- creating train-v3, TRAIN authority or EVAL authority;
- training, evaluating, or spending eval-v5;
- reading eval-v5 semantically, or reusing eval-v4;
- creating a gate on any field this milestone added;
- promoting, tagging, releasing or merging.

`eval-v5` remains `FROZEN_UNUSED`, `spent_by: null`, manifest
`e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c`, pack
`287a9fb61e3feab510763d834f77a75c3a016fe27ba4d04a4ac86c588c09fed6`. candidate004 remains
`NOT_CREATED` and no axis is selected.
