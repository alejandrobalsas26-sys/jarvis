# V69 M68D — Production trust-boundary stabilization (H01–H05)

Milestone branch: `jarvis-v69-m68d-production-trust-boundary-hardening`
Base: `af54fb95c6f0cfb4daa84e08b6fde6dff9a62118` (generation 67, M67A.1 integrated)

An independent external Codex audit of that master commit reported fifteen
findings. M68D is scoped to the five P1 ones. **The audit was treated as
evidence, not as authority**: every finding was reproduced independently, with
local synthetic infrastructure only, before any production code was changed.

## Summary

| FINDING | EXTERNAL_CLAIM | INDEPENDENT_RESULT | FINAL_STATUS |
|---|---|---|---|
| H01 | SSRF / DNS rebinding: policy validates one DNS answer, the transport resolves again | **Reproduced.** Policy approved `93.184.216.34`; the socket connected to `127.0.0.1` and the loopback body returned with `error = None`. Exactly two resolver calls. **Also found, not in the audit:** an ungoverned `HTTP_PROXY` produces the same split with no DNS involved | CONFIRMED → REMEDIATED |
| H02 | `read_file` renders from one opening and `identify_source` hashes a later one | **Reproduced.** Content `v1` returned with `sha256(v2)` and `complete: true`; `cas_write_text` then APPLIED a write derived from `v1`, destroying the unread `v2` | CONFIRMED → REMEDIATED |
| H03 | Secrets reach audit / AURA before redaction; `_check_pii_output` only warns | **Reproduced.** Synthetic password and Bearer canaries reached the audit JSONL on both execution paths and the AURA payload. **Sharper than the audit:** a nested canary was absent only because it fell past the 200-char cut — moving it to the front of the same result put it in the file | CONFIRMED → REMEDIATED |
| H04 | `release()` ignores both deletion return codes and reports success; a partial add goes untracked | **Reproduced.** All three release scenarios returned the byte-identical `{"released": True}` and popped `_active`. IN-ok/OUT-fail left a live rule with `host_isolated=False` and no tracking. **Also found:** `release()` wrote **no audit receipt at all** | CONFIRMED → REMEDIATED |
| H05 | Windows: 147 control-plane problems with `autocrlf=true`, 16 with it false; 10 M68C test failures; path separators; `os.access(X_OK)` | **Reproduced on Linux.** Two clones of one commit: **0 problems with `autocrlf=false`, 131 with it true**. Path-separator and `os.access` divergences reproduced directly. **Stronger than the audit:** `os.access(X_OK)` is wrong on POSIX in *both* directions — mode `100755` with the worktree bit cleared PASSES the check, so the control was **bypassable**, not merely unportable | CONFIRMED → REMEDIATED |

Nothing was NOT_REPRODUCIBLE and nothing was AUDIT_INCORRECT. Three findings
came out **broader** than reported, and one defect (a PEM key body surviving
redaction) was found by this milestone's own tests rather than by the audit.

---

## H01 — HTTP destination identity

**External claim.** `_http_target_blocked(url)` resolves the hostname and
validates the returned public IPs. `_safe_http_fetch` then passes the *hostname*
to `requests.request`, which resolves it again. Validation DNS ≠ connection DNS.

**Independent reproduction.** A counting fake resolver answering a public
address once and loopback thereafter, plus a loopback `ThreadingHTTPServer` on
an ephemeral port. No sleeps, no real metadata service, no external host.

```
resolver calls total   = 2
meta.error             = None
status                 = 200
body                   = INTERNAL-LOOPBACK-SECRET
loopback server hits   = ['rebind.invalid:36683']
POLICY_APPROVED_IP     = 93.184.216.34
ACTUAL_CONNECTION_IP   = 127.0.0.1
```

A second reproduction set `HTTP_PROXY` to a loopback listener: the validated
public destination was never contacted and the proxy answered, again with
`error = None`.

**Classification.** CONFIRMED, and broader than reported — the proxy path
reaches the same outcome without touching DNS.

**Root cause.** The policy decision was a `str | None` block reason. It could
express *whether* a destination was allowed but not *which address* had been
approved, so there was nothing for the transport to be bound to.

**Fix.** The decision became an object that carries the address.

- `EgressDestination` — scheme, host, port, authority, every candidate the ONE
  resolution returned, and the `pinned` address the socket must use.
- `govern_destination(url)` — resolves exactly once, validates **every**
  candidate (so a hostname aliasing public *and* private is refused rather than
  raced), and pins `candidates[0]`.
- `_PinnedDestinationAdapter` — a `requests` adapter whose pool host is the
  pinned IP. `server_hostname` is read from the original host **before** that
  overwrite, so HTTPS still performs SNI with the hostname and still validates
  the certificate against it. TLS verification is not weakened anywhere; the
  absent-control suite refuses `verify=False`, `assert_hostname=False`,
  `CERT_NONE` and `InsecureRequestWarning` in the module.
- `_pinned_transport_request` — the ONE transport primitive. Sets `Host` from
  `dest.authority` explicitly (with the pool keyed on an IP, `requests` would
  otherwise send the IP and silently break virtual hosting — measured), and sets
  `trust_env = False`, `session.proxies = {}` and `proxies={}`. There is no
  governed proxy architecture in this build, so the policy is **default-deny
  environment-proxy inheritance**, not enterprise proxy support.
- Every redirect hop re-enters `govern_destination`, so each hop is resolved
  once, validated and pinned on its own. M66A.1's cross-origin credential
  stripping is unchanged and is re-asserted here.
- An unpinned decision cannot reach the transport in normal mode; it fails
  closed. Trusted-lab mode keeps its documented semantics — it waives the
  internal-range policy and *only* that, still resolving once and still pinning
  what it resolves.
- `http_request` now reports `destination`: the address the socket used.

**Tests.** 58. Address-class matrix (loopback, RFC1918, link-local, metadata,
unspecified, multicast, reserved, IPv6 loopback / unique-local / link-local,
public v4 and v6 literals), scheme and authority rejection, mixed-candidate
refusal in both orderings, resolver failure injection, a socket-level DNS
rebinding proof on two loopback addresses sharing one port, a tripwire resolver
that makes a second lookup fatal, Host-header preservation, env-proxy
non-inheritance at the socket, per-hop re-resolution, cross-origin stripping,
TLS-weakening absent-controls, and structural controls that no arbitrary-URL
handler performs a raw fetch and that `session.request` has exactly one caller.

**Mutations.** 14, all detected. Including: connect by hostname again; pin
nothing; pin an address outside the validated set; validate only the first
candidate; let trusted-lab weaken normal mode; drop per-hop revalidation;
inherit the environment proxy; send the IP as `Host`; use the pinned IP for SNI;
delegate redirects to the transport; allow an unpinned decision through; turn a
resolver failure into an allow.

**Residual.** (a) There is no governed proxy path at all — an operator who needs
one has no supported option, which is a capability gap rather than a hole.
(b) The *effective peer* is asserted through the pinned decision and the
listener's own `getsockname`, not by reading the socket back out of `requests`
after the fact. (c) IPv6 scope-id destinations are blocked by range policy
rather than pinned.

---

## H02 — Source read identity

**External claim.** `read_file` obtains rendered content, then `identify_source`
reopens, re-stats and re-hashes the path. The pair can be `content = v1`,
`source.sha256 = hash(v2)`, and a CAS write derived from `v1` can then pass the
precondition.

**Independent reproduction.** An injected hook fired in the window between the
content read returning and identity capture, performing an **atomic, same-size**
replacement (so size alone could not notice):

```
returned content            = 'AAAA-version-one-AAAA\n'
returned source.sha256      = <sha256 of version TWO>
source.complete             = True
CAS status                  = APPLIED
the V2 human edit survived  = False
```

**Classification.** CONFIRMED, both halves.

**Root cause.** Two independent openings of a mutable path, with nothing binding
them. `identify_source` itself performed two more (`stat` then `digest_file`),
and `size_kb` came from a third.

**Fix.** One observation, held open.

- `source_snapshot(path, capture_bytes=...)` — opens the path **once** and holds
  the descriptor. `capture_bytes=True` reads the file once and digests exactly
  those bytes, so for text there is no window at all. `capture_bytes=False`
  digests the descriptor in chunks and hands parsers a `dup()` of it, which is
  what keeps identifying a large PDF from meaning holding it in memory.
- `SourceSnapshot.stable` — `fstat` on the held descriptor before and after,
  comparing inode, device, mtime and size *against the bytes actually captured*.
  An atomic replacement cannot make it false (the fd pins the inode); an
  in-place rewrite can, and then it says so.
- `identify_snapshot(snapshot, …)` — builds the `SourceIdentity` from the
  snapshot. An unstable snapshot yields no digest and `DIGEST_COVERS_NOTHING`,
  so it can never be `complete` and no precondition can match it.
- `_tool_read_file` refuses with `SOURCE_UNSTABLE` rather than returning a mixed
  observation (§14 option B), takes `size_kb` from the snapshot, and no longer
  imports `identify_source` at all.
- Every derived reader (`_read_pdf`, `_read_docx`, `_read_xlsx`, `_read_pptx`,
  `_read_rtf`, `_read_image_ocr`) takes the **snapshot**, not a path, and reads
  `snapshot.stream()`. The derived path calls `recheck()` after parsing, because
  the digest pass and the parser's pass are two reads of one descriptor.
- `_DERIVED_READERS` replaced an if/elif chain whose final branch was an
  unconditional `return self._read_image_ocr(...)` — declaring a new derived
  extension silently made it an OCR attempt. The table is exhaustive and an
  undeclared extension fails closed.

**Tests.** 50 (2 skipped where `python-docx` is absent; dependency-free
equivalents cover the same properties). Empty / Unicode / CRLF / no-final-newline
files, the `max_chars` boundary at −1/0/+1, atomic replacement mid-read,
same-size replacement, in-place rewrite, short read, deletion during read,
recreation after deletion, concurrent barrier-synchronised readers, derived
identity over source bytes, a parser handed a swapped path, parser failure, and
absent-controls that the read performs no second `read_text`/`stat`/`digest_file`
and that the stability check precedes content assembly.

**Mutations.** 11, all detected. Including: digest from a second opening; accept
mutation during acquisition as stable; one `fstat` only; give an unstable
snapshot a digest; reopen the path in `stream()`; render from a fresh read; drop
the unstable short-circuit; ignore the derived `recheck()`; take the size from a
fresh `stat`; restore the OCR fall-through; drop the captured-length comparison.

**Residual.** (a) **H12 is NOT solved.** There is still no bound on how much a
caller may ask to read; `capture_bytes=True` holds one text file in memory,
which is what `read_text` already did. (b) A foreign process rewriting the file
in place *between* the two `fstat` calls with identical size and mtime
granularity is not detectable by this mechanism. (c) `identify_source` still
exists for callers that legitimately want "what is at this path now"; only the
mutation-authoritative read path is forbidden from using it.

---

## H03 — Secret-safe observability

**External claim.** `_check_pii_output` warns but preserves secret content, and
`output_summary` is built before it and used by the audit JSONL and the AURA
broadcast. `TacticAuditLogger.log_action` stores `reasoning` and `result` with no
sink-local guarantee.

**Independent reproduction.** Synthetic canaries in a tool result:

| sink | password | bearer | reasoning |
|---|---|---|---|
| audit JSONL (sync path) | LEAKED | LEAKED | LEAKED |
| audit JSONL (async path) | LEAKED | LEAKED | LEAKED |
| AURA broadcast | LEAKED | LEAKED | — |

A nested canary was *absent* — and a second run with it moved to the front of
the same result put it in the file. Truncation was doing a redactor's job.

**Classification.** CONFIRMED, and sharper: the apparent protection of nested
values was an artefact of the 200-character cut.

**Sink inventory (§22).** Every sink reachable from an executed tool result:

| SINK | AFTER M68D | WHEN | PERSISTENT | REDACTION OWNER |
|---|---|---|---|---|
| `TacticAuditLogger` JSONL | sanitized | at the sink **and** before summary | yes | `governance.log_action` → `safe_observability` |
| AURA broadcast | sanitized | at the sink | no (WebSocket) | `_aura_broadcast` → `safe_observability` |
| Loguru error log | sanitized | at the call site | yes | `executor` → `safe_observability` |
| LLM prompt history | sanitized | before reinsertion | long-lived, in-process | `llm.py` → `safe_observability` |
| durable effect journal | body-free **by design** | — | yes | digests only; nothing to redact |
| `_effect_ledger` | raw | in-process dedup | no | not a telemetry sink — see residual |
| operational store / session journal / episodic memory / incident workspace | **not fed by executed tool results** | — | — | out of scope, verified |

**Root cause.** Two problems, not one. The *ordering* was backwards at the call
sites, and the sinks had no guarantee of their own, so correctness depended on
every present and future call site remembering.

**Fix.** One governed sanitizer, two enforcement points.

- `core/safe_observability.py`. It does **not** fork a third credential
  vocabulary: the repository already designates `core.redaction_policy` as "the
  one deterministic redaction scanner", which already delegates credentials to
  `core.memory_router`. So `sanitize_text` **composes**: an anchored pre-pass,
  then `redaction_policy.redact_text` (hidden reasoning, OTP, credentials, home
  paths), then spans measured to slip through it. Of fifteen synthetic shapes
  that vocabulary redacted five and missed ten — including `"password": "…"` in
  JSON (`\b` matches before the quote but `[:=]` then cannot) and `secret_key=…`
  (`\bsecret\b` cannot match before `_`). A superset test asserts the composition
  never redacts less than the governed vocabulary does.
- The capability nothing had: a **structured** recursive walk, and redaction by
  **field name** as well as by span — which is the only thing that catches an
  opaque twelve-character value under a key called `password`.
- Ordering: sanitize → summarise → truncate. A secret past the cut is lucky, not
  safe.
- Fail closed (§19): a cycle, excessive nesting, an unwalkable structure or an
  internal exception yields `[REDACTION_FAILED]` plus privacy-safe metadata
  (reason class and type name). **No path returns the raw value.** The LLM
  history sink previously had *two* fail-open routes — an optional
  `ContextManager` and `except Exception: pass` — and both are gone.
- Reasoning is **minimised**, not preserved (§21), through
  `redaction_policy`'s pipeline, which strips hidden-reasoning blocks first.
  M67A.1's hidden-reasoning prohibition is reused rather than reimplemented and
  nothing here creates a private channel.
- The value **returned to the caller** is deliberately not redacted: the caller
  asked for the file, and redacting it would break the tool. What M68D
  guarantees is that no sink keeps it. The warning key stays.

**Tests.** 89. Ten secret shapes × redaction and recognition, the superset
property with a non-vacuity witness, a positive control that harmless output
stays useful, fifteen secret field names with opaque values, every supported
Python type, fail-closed on cycles / depth / raising sanitizer / raising
governed pipeline / unserialisable values, ordering (redaction before truncation
and before summarisation, and at six offsets), and end-to-end canary absence
from the audit JSONL on both paths, the AURA payload, the Loguru record and the
prompt history — **each with a non-vacuity assertion that the sink received an
event at all.**

**Mutations.** 16, all detected. Including: raw summary at both call sites; raw
result / reasoning / command in the JSONL; raw AURA event; raw exception into
Loguru; raw tool output into the prompt history; refusal and exception falling
back to raw; nested value not walked; field-name rule removed; PEM pre-pass
removed; summary skipping the sanitizer; reasoning bypassing it.

**A defect this milestone's own tests found.** The first draft lost
`context_manager`'s whole-block PEM pattern. `memory_router` matches only the
`-----BEGIN … PRIVATE KEY-----` *delimiter*, so running it first left the key
**body** in the clear on the next line. Fixed with an anchored pre-pass that runs
before the governed pipeline, plus an ordering control so it cannot move back.

**Residual.** (a) `core.context_manager` and `core.memory_router` keep their own
redactors for paths outside the tool-result fan-out; M68D did not unify them,
because §22 scopes this to executed tool results. (b) `_effect_ledger` holds raw
result dicts in process memory for exactly-once dedup; it is never serialised,
broadcast or persisted. (c) Redaction is pattern- and name-based: an opaque
secret in an unrecognised field with no telling name is not detectable by any
deterministic scanner, and this one does not claim to.

---

## H04 — Quarantine effect truth

**External claim.** `release()` invokes two firewall deletions, ignores both
return codes, then does `_active.pop(ip, None)` and `released = True`. And
`quarantine()` with IN succeeding and OUT failing produces one real rule,
`host_isolated = False`, and no entry in `_active`.

**Independent reproduction.** A deterministic fake backend. **No `netsh` was
invoked, no real rule was created or deleted, and no test needs admin rights.**

```
RELEASE both deletes FAIL  -> {'released': True}   _active erased
RELEASE one fails          -> {'released': True}   _active erased
RELEASE both succeed       -> {'released': True}   _active erased
ADD IN ok / OUT fail       -> host_isolated=False, _active tracks it = False
audit receipts written     = 4   (four ADDs, ZERO releases)
```

**Classification.** CONFIRMED, and broader: `release()` wrote no receipt at all.

**Root cause.** An effectful two-rule operation reduced to one optimistic bool,
with the command results discarded rather than consumed.

**Fix.** Per-rule evidence, and a *query* instead of a return code.

- `RuleEvidence` — name, direction, action, attempted, `command_rc`, an M65D
  `ExternalOutcome` string, and `observed_present` from a firewall query, where
  `None` means "no query was possible". The difference between *I looked* and
  *I assumed* is the point of the dataclass.
- `ContainmentEffect` — `NO_EFFECT` / `FULL_EFFECT` / `PARTIAL_EFFECT` /
  `UNKNOWN_EFFECT` / `RECONCILIATION_REQUIRED`. This is **not** a competing
  truth model: M65D's `ExternalOutcome` answers "is this effect in the world",
  per rule, and is carried per rule; this answers the different question M65D
  does not — how much of a multi-rule operation landed — and is *derived* from
  those outcomes. A test asserts the per-rule vocabulary is exactly
  `ExternalOutcome`'s.
- `_rule_present(name)` queries `netsh … show rule`. Rule present, rule absent
  and *query did not answer* are three distinguishable results.
- `_outcome_for(rc, present, want_present)` — a **query outranks a return
  code**. Without a query, `rc == 0` is the command's own receipt and anything
  else is `UNKNOWN`, because `_run` also returns `(1, …)` for a timeout, which
  proves nothing about whether the rule was created.
- `_derive_effect` never rounds an `UNKNOWN`. A mixed set containing one is
  `RECONCILIATION_REQUIRED`, because "some rules are there and I cannot tell
  about the rest" is the state an operator must act on.
- `quarantine()` tracks **any** non-`NO_EFFECT` outcome in `_active`, including
  partial and unknown ones. `host_isolated` is true only for a verified
  `FULL_EFFECT`.
- `release()` sets `released` only when **every** controlled rule is *proven
  absent* by a query. Anything less keeps local tracking and reports
  `reconciliation_required`. Every path — including refusals and duplicate
  requests, which were silently un-audited — writes a receipt.
- `pending_reconciliation()` and `active_targets()` expose the process-local
  view read-only.

**Tests.** 61. Add with both succeeding / IN-ok-OUT-fail / IN-fail-OUT-ok / both
failing, a command that reports failure but created the rule anyway, an
unanswerable query, all four refusal paths, the active cap, duplicate requests
against full and partial state, retry after partial failure, release with both /
one / neither deletion verified, release of something already absent, release
with one rule already gone, an unanswerable query blocking the release claim, a
deletion that reports failure but worked, retry of a failed release, receipt
presence and per-rule `command_rc` content, release without privileges, the
full `_derive_effect` and `_outcome_for` truth tables, and absent-controls that
no return code is discarded, that `released` cannot be assigned
unconditionally, that the `pop` is guarded by the proof, that a partial add
cannot be dropped, and that exactly one path creates and one path deletes rules.

**Mutations.** 15, all detected. Including: ignore the deletion rc;
unconditional `released = True`; pop before verification; skip the query;
discard a partial add; claim isolation on a partial; round `UNKNOWN` to
committed and to `NO_EFFECT`; collapse a mixed unknown to partial; let the rc
outrank the query; guess absence from an opaque query; write no release receipt;
un-audit a duplicate; accept `rc == 0` as proof of absence.

The deletion-rc mutation initially **survived**, and the reason is worth
recording: because absence is proven by a query, discarding the return codes
does not change what `released` says. The fix is stronger than the finding, and
the evidence field is what had to be asserted — so a test now requires the
receipt to carry the failing command's `command_rc`.

**Residual.** (a) `_active` is **process-local**. M68D did not add a durable
journal (§27 forbids inventing a second one), so a restart still loses the
in-memory view; the honest recovery is a firewall query, which
`pending_reconciliation()` gives a starting point for. (b) Tracking unknown
effects consumes `_MAX_ACTIVE` slots, so a run of unknowns can reach the cap and
refuse new quarantines until an operator reconciles — the correct trade, but a
real operational consequence. (c) The NAC webhook has its own effect identity
and is unchanged by M68D. (d) No test exercises real `netsh`; the Windows
behaviour of `show rule`'s exit codes and localised output is modelled, and the
Spanish/English "no rules match" strings are a best-effort match.

---

## H05 — Windows control-plane portability

**External claim.** Windows Python 3.12.10: 355 passed, 10 failed, 3 skipped,
all ten failures in `test_source_integrity_m68c.py`. Control Plane on a normal
checkout with `core.autocrlf=true`: 147 problems; with it false: 16. Three
categories: checkout newline conversion, path separators, `os.access(X_OK)`.

**Independent reproduction.** Two *full* temporary clones of one commit (the
first attempt used `--depth 1` and produced 13 shallow-clone artefacts in both,
which were excluded):

```
autocrlf=false  ->   0 problems
autocrlf=true   -> 131 problems
   67 SNAPSHOT_CHAIN   37 RECORD_STORE   11 SCHEMA   6 EVALUATION_RECEIPT
    4 ARCHIVE_INTEGRITY  2 CURRENT_POINTER  2 AUTHORITY_SEPARATION
    1 VERIFIER_INTEGRITY  1 INSTRUMENT_STACK
```

131 against the audit's 147 − 16 = 131. Every one is a byte-digest mismatch on a
file whose working-tree bytes were newline-converted — a **false rejection of a
legitimate checkout**, not corruption.

Path separators and `os.access` were reproduced directly:

```
git ls-files says tracked        : ['state/m62/snapshots/0067-….json']
str(Path.relative_to()) on Win   : 'state\m62\snapshots\0067-….json'
   win_rendered in tracked       = False      <- reported as UNTRACKED
direction 1: git mode 100644, os.access(X_OK) True   -> FALSE POSITIVE
direction 2: git mode 100755, os.access(X_OK) False  -> FALSE NEGATIVE
```

**Classification.** CONFIRMED in all three categories, and **stronger than the
audit in category B**: `os.access(X_OK)` is wrong on POSIX in both directions, so
a file committed `100755` with its working-tree bit cleared *passes* the check.
That made the control **bypassable by anyone who could commit a mode**, which is
a security finding rather than a portability one.

**Root cause.** Three places where the check and the effect used different
vocabularies for the same object: native vs repository paths, filesystem vs Git
modes, and converted vs sealed bytes.

**Fix.**

- **A · paths.** `repo_path(path)` renders a repository path: relative to the
  root, `as_posix()`. It is the only form compared against `git ls-files` or
  `git check-attr`.
- **B · modes.** `_git_index_modes()` reads `git ls-files --stage`. Both
  executable-bit checks (`PATH_INTEGRITY` and `RECORD_STORE`) now compare against
  `GIT_MODE_EXECUTABLE`, and both **fail closed** when Git cannot answer. A path
  absent from the result is absent from the dict — silence is not `100644`.
  `os.access` and `X_OK` no longer appear in the verifier at all.
- **C · bytes.** The control plane digests **working-tree** bytes deliberately:
  inspecting what is actually on disk is what catches a malicious edit, which a
  blob-only check would miss. The cost is that `core.autocrlf=true` rewrites
  them. So the bytes are **pinned**, not normalised: `.gitattributes` declares
  `-text` for `state/m62/**`, `PROGRESS.md`, `jarvis/docs/m62/**` and the
  verifier itself, which makes working-tree bytes equal blob bytes on every
  platform. **No sealed digest was recomputed and nothing is canonicalised
  before comparison.** A byte seal stays a byte seal.
- **The pin is made load-bearing.** A new mandatory `NEWLINE_POLICY` check
  requires `git check-attr text` to report `unset` for every tracked byte-sealed
  path (128 of them at generation 67), requires `.gitattributes` to exist and be
  tracked, verifies the live snapshot is in the pinned set, and **refuses to pass
  on an empty artifact set** so it cannot succeed vacuously. The set is declared
  as *trees*, so a new snapshot, record or receipt is covered the moment it is
  added.

**Proof the pin works.** A fresh `autocrlf=true` clone with the pin applied:

```
CRLF checkout, NO newline pin    -> 134 problems
CRLF checkout, WITH the pin      ->   3 problems
```

and all three remaining problems are the disposable test commits themselves
(verifier digest moved, commits outside a governed subject). **Zero byte-integrity
problems**, and `PROGRESS.md` materialises as LF even with `autocrlf=true`.

**D · test portability.** `test_source_integrity_m68c.py` now declares
capabilities instead of assuming them: `POSIX_MODES_ENFORCED`,
`FLOCK_AVAILABLE`, `OUTSIDE_SANDBOX`. The POSIX permission guarantees are still
asserted wherever they hold; the single-winner serialisation guarantee is
asserted only where `fcntl.flock` exists, and M68C's **honest-degradation**
contract (`serialised is False` with no `fcntl`) is asserted **everywhere** by
poisoning `sys.modules["fcntl"]`, so that contract is witnessed on Linux too
rather than only on a Windows runner. Nothing was weakened to make a platform
pass.

**E · the Windows runner.** A blocking `windows-portability` job on
`windows-latest` / Python 3.11 / `shell: pwsh`, with `fetch-depth: 0`. It runs
the Control Plane verifier, the M68C source-integrity suite, the M68D H02 and
H05 suites, the dependency-authority checks and an import smoke test. It leaves
`autocrlf` at the Windows **default** on purpose — setting it false would make
the job pass by avoiding the condition it exists to witness — and it *reports*
the newline configuration it ran under. It is deliberately **focused**: no
repository evidence justifies a second full 12 722-test run, and the skip
baselines are Linux-measured.

**Tests.** 65. Repository-path rendering including a Windows-shaped regression
fixture and an `as_posix` structural control, Git index modes with both
`os.access` divergence directions measured in a disposable repo and the Windows
`os.access`-always-true behaviour simulated, newline pinning per named artifact
and across all 128 tracked sealed paths, `-text` rather than `eol=`,
dispatch/ownership of `NEWLINE_POLICY`, its non-vacuity guard, platform
capability declarations, the `serialised=False` contract without `fcntl`,
integrity-not-weakened controls (canonical serialization intact, digest over raw
bytes **behaviourally** including a CRLF fixture, no whitespace or Unicode
canonicalisation, one owning check per category), and fourteen assertions on the
Windows job's shape.

**Mutations.** 18, all detected. Including: native path in the tracked set;
`repo_path` using `str`; `os.access` for the Git mode; mode check failing open;
record store skipping modes; `NEWLINE_POLICY` undispatched; passing vacuously;
empty pinned set; `text=auto`; `eol=lf` on `PROGRESS.md`; the digest normalising
CRLF; the Windows job on Linux, disabling autocrlf, skipping the verifier, or
becoming advisory; tests assuming `geteuid` or `flock`; the receipt claiming
serialisation without `fcntl`.

Two of these initially survived and both exposed real detector gaps:
`repo_path` using `str` is **behaviourally identical on POSIX**, so it needed a
structural control naming the method; and the digest normalising CRLF kept
`read_bytes()` in the source, so the structural assertion passed while the seal
had stopped being a byte seal — it needed a behavioural test over a CRLF fixture.

**Residual.** (a) **`WINDOWS_CI_EXECUTED = NO_PENDING_INTEGRATION.** Everything
above is Linux evidence plus a job definition. No real Windows runner has
executed. The audit's specific "355 passed / 10 failed / 3 skipped" is **not**
independently confirmed, and which ten tests failed is inferred from the code,
not measured. (b) Making the Windows job a *required status check* on master is
a GitHub repository-settings action — that is **H11** and is deferred. The job
is mandatory within the workflow; it is not yet a merge gate. (c) Windows
symlink policy and `netsh` output localisation are untested on the real platform.
(d) The byte-seal semantic question was resolved in favour of working-tree bytes
with the newlines pinned; a determined attacker with commit access can still
change both the blob and the working tree, which is what the Git-ancestry and
verifier-digest checks are for, not this one.

---

## Scope discipline

**Not fixed, and verified not fixed:** H06 regex termination, H07 AURA bootstrap
authentication, H08 BAS scope parity, H09 mobile RBAC, H10 PowerShell installer
exit codes, H11 GitHub required checks, H12 bounded input consumption, H13 large
function decomposition, H14 stale PROGRESS historical presentation, H15 Sliver
allowlist/handler mismatch.

**Science did not move.** No change under `training/`, `evals/`, `evaluation/`,
`training_gym/` or `graders/`. `candidate006` and `eval-v8` remain ABSENT. No
training, no model adaptation, no evaluation spent, no corpus work.

**Security testing boundary.** Every adversarial reproduction was local and
synthetic: fake resolvers and loopback listeners for H01, injected file hooks for
H02, invented canary strings for H03, a stateful fake firewall for H04,
disposable clones and temporary Git repositories for H05. No cloud metadata was
probed, no LAN device touched, no real firewall rule altered, no real credential
used, no TLS validation disabled, and no RBAC, HITL or containment control
weakened.
