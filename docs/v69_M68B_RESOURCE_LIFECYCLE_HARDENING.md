# JARVIS V69 — M68B · RESOURCE & LIFECYCLE HARDENING

**The findings M68A confirmed and deferred.** Five areas, each re-validated against
current `master` before a line changed, repaired, and pinned by focused,
absent-control and cleanup-proof tests.

| | |
|---|---|
| Branch | `jarvis-v69-m68b-resource-lifecycle-hardening` |
| Base | `7ae48a25d089a121236b172ac423cd7268efcaa5` (M68A, generation 64) |
| Scope | RESOURCE & LIFECYCLE ONLY. No science, no training, no evaluation, no corpus, no SWE patch engine |
| Spends | **0** — `eval-v7` stays `USED_IMMUTABLE`; candidate006 and eval-v8 absent |

---

## 0 — Why the audit wording was re-derived rather than trusted

Every finding below was reproduced, or structurally proven, against `7ae48a2`
**before** any edit. Two of the five turned out to be materially worse than the
audit described, and one of them contained a defect the audit had not seen at all:

| Finding | Audit said | Measurement found |
|---|---|---|
| A | output truncated after accumulation | also: `output_limit: ENFORCED` was published for a control that did not exist during execution |
| B | `broadcast()` may await sockets sequentially | it does; **and** nine other `create_task` sites in the same module were unbounded and untracked |
| C | unbounded queue, unbounded concurrency, growing state | all confirmed; **and** the YARA scan had never run — an `async def` called without `await` inside a bare `except`, so `yara_hits` was always empty and the severity grade was silently degraded |
| D | Docker config vs a localhost-fixed client | confirmed; the bypass count was **eleven** modules, across **two** different environment variables, plus a `settings.ollama_host` field that does not exist |
| E | logs persisted, `/app/data` may not be | confirmed; **and** the image never created `/app/data`, so the runtime user could not have written it even without a volume |

Nothing here rests on "the code looks wrong". Each finding has a dynamic
reproducer that executed, or an AST/configuration property over the shipped tree.

---

## A — PROCESS OUTPUT BUFFERING · CONFIRMED

### The defect

`core/containment.py` collected a contained execution's output like this, in both
execution backends:

```python
stdout, stderr = proc.communicate(timeout=request.timeout)
...
stdout = (stdout or "")[:CE_STDOUT_CAP]      # truncation AFTER accumulation
receipt.controls["stdout_cap"] = ControlStatus.ENFORCED
```

`communicate()` reads each pipe to EOF into a Python list and joins it. The cap
bounded the string the caller was **handed**; it never bounded the memory the
broker **allocated**. A snippet writing 4 GiB to stdout made the *parent* — the
JARVIS runtime, not the contained child — allocate 4 GiB.

The child's own limits do not help. `RLIMIT_AS` (512 MiB) bounds the child's
address space, and a pipe write is not address space. `RLIMIT_FSIZE` (16 MiB)
bounds a single file write, and a pipe is not a file. The writes are streamed, so
there is no per-child quota to exceed: the child stays inside every limit it was
given while the parent grows without one.

**A bounded returned string is not bounded memory.** And `output_limit` was in
`MANDATORY_SANDBOX_CONTROLS` — one of the thirteen controls that must all be
`ENFORCED` for the word `SANDBOXED` to be derived — so a control the tree treated
as load-bearing was being asserted from a post-hoc slice.

### Reproduction

`tests/test_bounded_output_m68b.py::TestRetentionBound` measures `bytes_seen`
against `bytes_retained`. Against the pre-M68B code the only observable was
`len(stdout) <= CE_STDOUT_CAP`, which the defect satisfied perfectly — which is
why the audit's own framing had to be sharpened before a test could fail.

### The fix

`core/bounded_output.py` (new). One reader **thread** per stream; bytes past the
retention limit are counted and **discarded as they arrive**. Three properties are
load-bearing and each has its own test:

1. **Both pipes drain concurrently** — the reason `communicate` exists at all. A
   child filling stderr while the parent reads stdout would deadlock a
   single-stream reader.
2. **Draining CONTINUES past the limit.** If the reader stopped, the pipe buffer
   would fill, the child would block on its next write, and the "bound" would be a
   deadlock wearing a bound's clothes. The wall-clock timeout stays the real bound.
   `TestOutputAccumulationIsAbsent::test_the_bounded_reader_keeps_draining_after_the_limit`
   asserts the drain loop has exactly one exit and it is end-of-stream.
3. **Truncation is positive evidence, not absent data** — `bytes_seen`,
   `bytes_retained`, `truncated`, `limit_bytes` and `chars_returned` per stream,
   plus a closed `termination_reason`, published on the receipt as
   `output_accounting` and promoted to `output_bytes_seen` /
   `output_bytes_retained` / `stdout_truncated` / `stderr_truncated` /
   `termination_reason`.

Two truncations exist and are not interchangeable. `bytes_seen > bytes_retained`
is the **memory** bound; `len(text) > cap` is the **presentation** bound.
`truncated` is true if either fired, and both numbers stay in the record so an
operator can tell which did.

The retention limit is `cap * 4 + 8 KiB`: four bytes per capped character (the
widest UTF-8 encoding, so the character cap is always reachable) plus a reserve for
the sandbox readiness record, which is consumed from the head of stdout before the
snippet's own output begins. Without that reserve a jail's readiness line would
have silently eaten part of the snippet's 3000 characters.

### What else changed, and what did not

* Pipes are now **binary**; decoding happens once at the end, with
  `errors="replace"`. `text=True` decoded strictly, so a snippet emitting invalid
  bytes could raise `UnicodeDecodeError` out of the capture itself. Newline
  translation is reproduced explicitly, so the returned text is unchanged.
* **The public outcome contract is unchanged.** A timed-out run still reports
  `returncode=None`; the signal the reaper observed is recorded in the accounting
  instead. `stdout`/`stderr` are still capped to the same character limits.
* The accounting is recorded on the two `executed=False` bubblewrap paths too:
  "the jail produced 9 GiB of stderr and never reached readiness" is a diagnosis a
  bare `failure_reason` cannot give.

### Residual

* The bound is on what the **parent retains**. Nothing in user space bounds what a
  child writes; that is what the wall timeout and the child's rlimits are for.
* A descendant holding a pipe open past the child's exit can delay the reader join
  by at most `reap_grace_s` (5 s). Retention has already stopped at the limit, so
  this costs latency, never memory.

---

## B — AURA BACKPRESSURE / PRODUCER ISOLATION · CONFIRMED

### The defect

```python
async def broadcast(self, event: dict) -> None:
    for ws in set(self._clients):
        try:
            await ws.send_text(payload)     # the producer awaits a SOCKET
        except Exception:
            dead.add(ws)
```

One sequential await per client, on the producer's own stack, with no deadline. A
browser tab that stops reading fills its TCP receive window; `send_text` then waits
on the transport's drain future, which resolves only when *that client* reads.

`_lifespan` does `correlator.attach(manager.broadcast)`, so this is not only a HUD
concern: **compound-incident correlation ran on the producer path of the slowest
websocket consumer.** Detection latency was a function of a browser tab.

Two further problems in the same module, neither in the audit text:

* `broadcast()` did `asyncio.create_task(_corr.ingest(event))` **per event**, and
  the websocket endpoint did the same per HUD command, plus seven more in
  `_dispatch_hud_command`. All unbounded (concurrency equal to arrival rate) and
  all **untracked** — asyncio keeps only a weak reference, so a long job nobody
  holds may be collected mid-await and simply stop, with no error anywhere.
* `_ws_endpoint` cleaned up inside `except WebSocketDisconnect` only. Any other
  error left the client in the set with a live writer behind it.

### The fix

A bounded queue per connection, **one owned writer task** per queue, and an
explicit send deadline:

| part | why it is there |
|---|---|
| bounded queue per connection | the producer's per-client cost is one `put_nowait` — O(1), and no await for a slow consumer to block on |
| exactly one owned writer | concurrency is one task per **connection**, never one per event; frames stay ordered on the wire |
| explicit send deadline (5 s) | without it the stall merely moves into the writer instead of the client being removed |
| explicit overflow policy | `DROP_OLDEST`, counted. Telemetry is a stream of snapshots: the newest frame is the one a HUD needs, so an evicted frame is the right loss and a hidden loss is the wrong one |
| deterministic teardown | the writer is cancelled **and awaited**; `aclose_client` runs in a `finally`, so a transport error cleans up like a disconnect |
| `_spawn_bounded` | one ceiling (64), one strong-reference set, one drop counter, for all ten former fire-and-forget sites |

Deliberately **not** solved by `create_task(ws.send_text(...))` per client per
event: that trades a bounded stall for unbounded task growth, reorders frames, and
keeps only a weak reference to each send.

A shutdown **sentinel** is queued behind the frames a channel already holds, so
closing a healthy connection costs one loop turn instead of always waiting out the
drain ceiling. Measured: the Finding B suite went from 51.7 s to 0.86 s when the
sentinel replaced an unconditional timed wait — the same evidence that the drain is
now a ceiling for a wedged client rather than the normal cost.

`/health` reports the bounds and the counters, so the policy is observable from
outside the process.

### Residual

* `AURA_CLIENT_QUEUE_MAX = 256` frames × the number of connections is the real
  memory bound. It is finite and declared, not derived from a measurement of frame
  size.
* A client that misses the 5 s deadline is declared dead and dropped. That is a
  policy choice: a HUD on a slow link loses its stream rather than holding the
  correlator. The alternative — an unbounded wait — is the defect.
* `core/c2_dashboard.py` has a second, older client set with the same sequential
  shape. It is **out of M68B's scope** and recorded here rather than silently
  widened into it.

---

## C — CODE_INTEL RESOURCE & LIFECYCLE BOUNDS · CONFIRMED

Every sub-item in the audit reproduced, and the completion mark was worse than
described.

### The defects

```python
_ANALYZED: set[str] = set()            # module-level, grows forever
...
if str(file_path) in _ANALYZED: return None
_ANALYZED.add(str(file_path))          # marked BEFORE any work happened
data = file_path.read_bytes()          # blocking, unbounded, ON THE EVENT LOOP
...
queue: asyncio.Queue = asyncio.Queue() # no maxsize
while True:
    path = await queue.get()
    await asyncio.sleep(0.5)           # serialises intake behind a sleep
    asyncio.create_task(analyze_file(path, ...))   # one task per arrival, untracked
```

* **"Started", "succeeded" and "failed" were the same observation.** A file whose
  read failed was marked analysed for the life of the process and could never be
  retried.
* The `Observer` thread was started and never stopped; the `while True` loop had no
  cancellation path. A shutdown left both behind.
* `_shannon_entropy` accumulated a frequency table one byte at a time in Python —
  on the event loop, with no size policy.
* The YARA scan had **never run**: `scan_command` is `async def` and takes a token
  list; it was called synchronously with a single string, the resulting coroutine
  was iterated, that raised, and a bare `except` swallowed it. `yara_hits` was
  always `[]`, and the severity grade reads `yara_hits` first.

### The fix

| audit requirement | how |
|---|---|
| bounded queue | `asyncio.Queue(maxsize=CI_QUEUE_MAX)` fed through `core.safe_enqueue.SafeEnqueue` — the repository's one thread→loop seam, which coalesces duplicate events on the producer thread and counts drops instead of raising into the loop |
| explicit worker count | `CI_WORKERS = 2`, named tasks, cancelled and awaited on exit |
| bounded / evictable state | `AnalysisLedger`: an LRU of `CI_LEDGER_MAX = 2048`, evicting terminal entries first and never an entry a worker is holding |
| `PENDING != COMPLETED != FAILED` | five distinct states, plus `REJECTED` for terminal-by-policy |
| explicit retry policy | `CI_MAX_ATTEMPTS = 3`; `FAILED` is retryable below it, `REJECTED` never is |
| never complete before success | `book.complete(key)` runs only after the report is on disk |
| explicit size policy | `CI_MAX_FILE_BYTES = 32 MiB`, enforced by a **stat before the read**, and re-checked after |
| blocking work off the loop | the read and the whole CPU-bound scan run in `asyncio.to_thread`; the entropy table is now a C-level `Counter` |
| clean shutdown | a `finally` that cancels the workers, awaits them, then `observer.stop()` + `observer.join(5.0)` |
| no uncontrolled fan-out | no task per file; the settle delay moved into the worker |

### The defect M68B's own tests found

The first draft caught `CancelledError` only around the LLM call. A cancellation
during the **threaded read** therefore left the entry `RUNNING` — and a `RUNNING`
entry is never claimable again, which poisons the file for the life of the process
in exactly the way the old `_ANALYZED` set did. The repair is not another handler
at another await point; it is an invariant over every exit:

```python
finally:
    if book.state(key) is AnalysisState.RUNNING:
        book.fail(key, "interrupted")
```

`analyze_file` is now the lifecycle guard and `_analyze_claimed` does the work.
`test_no_path_may_leave_a_file_running` asserts the `finally` re-checks for
`RUNNING` — a property, not a call site.

### Residual

* A `COMPLETED` file can be re-analysed after 2048 other files have passed
  through. That is correct: the ledger is a duplicate suppressor, not a permanent
  archive. The reports on disk are the archive.
* `CI_WORKERS = 2` is sized for the 15 W CPU-bound host, not measured against a
  throughput target.
* The `on_created`-only handler is unchanged: a file moved into the inbox by some
  filesystems raises `on_moved`, which this watcher has never observed. Recorded,
  not fixed — it is a coverage gap, not a resource bound.

---

## D — OLLAMA ENDPOINT CONSISTENCY · CONFIRMED

### The defect

Eleven modules resolved the endpoint in their own way, across two environment
variables and one field that does not exist:

| site | what it did |
|---|---|
| `core/model_router.normalize_ollama_host` | `$OLLAMA_HOST`, normalised — the de-facto canonical path |
| `core/ollama_native.default_base_url` | delegated to the above |
| **`core/llm.LLM.__init__`** | `base_url="http://localhost:11434/v1"` — **the inference client** |
| `core/sigma_generator` | `"http://localhost:11434/v1"` |
| `core/cognitive_synthesis` | `"http://127.0.0.1:11434/api/generate"` |
| `core/vision_engine` (×2) | `"http://127.0.0.1:11434"` |
| `core/fast_readiness` | `base_url: str = "http://localhost:11434"` as a dataclass default |
| `core/ai_reverser` | `$JARVIS_OLLAMA_URL` — **a different variable** |
| `core/health_watchdog` | `$JARVIS_OLLAMA_URL` again |
| `core/runtime_doctor` | `getattr(settings, "ollama_host", "")` — **not a field on Settings**, so always the hardcoded fallback |
| `core/ollama_env.posture_report` | `"host": "127.0.0.1"` as a literal |

The Dockerfile ships `ENV OLLAMA_HOST=http://host.docker.internal:11434`. In that
**supported** deployment the health check, the availability probe and the
diagnostics bundle resolved `host.docker.internal` while inference resolved
`localhost`. "Ollama is reachable" and "inference works" were answers about two
different machines — and a green health check could sit beside every generation
failing to connect. The reverse is worse: a diagnostic naming the wrong endpoint
sends an operator to debug a host that was never involved.

### The fix

`core/ollama_endpoint.py` (new) is THE resolution path. It returns a frozen
`OllamaEndpoint` carrying the provenance a diagnostic must report — `base_url`,
`openai_base_url`, `source` (`explicit` / `environment` / `default`), `raw`,
`valid`, `reason`.

* `normalize_ollama_host` survives as a **facade** and no longer parses. A second
  implementation is the defect, so `test_the_legacy_normaliser_delegates` walks its
  AST and refuses a `urlparse` call inside it.
* Validation is real: scheme allowlist (`http`/`https` only), host required, port
  range, no path/query/fragment, no whitespace or control characters. IPv6
  literals keep their brackets.
* **Invalid configuration fails explicitly** via `require_endpoint`, which raises
  `InvalidOllamaEndpoint`. The tolerant resolvers never raise — a malformed
  `OLLAMA_HOST` must degrade a diagnostic, not stop JARVIS booting — and they
  record the substitution, so the report says `valid: false` with the reason
  instead of pretending the default was chosen.
* `env` is an injectable mapping. Every test passes its own, so nothing mutates
  `os.environ` and no value leaks into the next test.
* No widening: with nothing configured it resolves the loopback default, exactly as
  before. Authorization and network policy are untouched.

The absent-control side is the composition site, not the module:
`test_the_inference_client_base_url_is_computed_not_written` walks `LLM.__init__`
and requires `base_url` to be an `ast.Call`, never an `ast.Constant`.
`test_only_the_canonical_module_owns_the_default` allows the literal in exactly two
files and pins why the second is exempt — `core/windows_hardener._LOOPBACK` is the
address Ollama **listens on**, a hardening action; resolving it through the client
resolver would be circular, since the hardener writes the variable the resolver
reads.

### Residual

* `core/deployment_planner` and `core/security_auditor` still hold `11434` as a
  **port number** for planning and port-binding audit. Those are not endpoint
  resolution and were left alone.
* The resolver reads `OLLAMA_HOST` from the process environment. It still cannot
  confirm what the Ollama **server** was launched with — `core/ollama_env` already
  says so, and `settings_verified` remains permanently `False`.

---

## E — DOCKER PERSISTENCE · CONFIRMED

### The defect

```yaml
volumes:
  - jarvis_logs:/app/logs
```

One application volume. Everything under `/app/data` was container-local — and
`core.effect_journal.DEFAULT_JOURNAL_PATH` is `<app>/data/effect_journal.db`, the
M65C/M65D **durable effect journal**, whose entire purpose is to survive a restart
so the retry authority can tell "this effect already happened" from "this effect is
new". `docker compose up --force-recreate`, the ordinary way to apply a new image,
destroys the container's writable layer. The journal went with it, and the next run
replayed effects it had already performed while every other signal said the
deployment was healthy.

It is also the directory the container **could not create**:

```dockerfile
RUN mkdir -p logs && chown jarvis:jarvis logs
USER jarvis
```

`/app` itself stays root-owned (`COPY --chown` sets ownership on the copied
*content*, not on `WORKDIR`), `data` is excluded by `.dockerignore`, and
`core.managed_paths._resolve` swallows the resulting `OSError` **by design**, so a
read-only tree cannot crash the runtime. The mkdir failed silently and every
durable write failed after it. A volume mounted onto a directory the runtime user
cannot write is a volume that holds nothing.

### The classification, before the configuration

`core/deployment_persistence.py` (new) classifies each container path rather than
persisting everything:

| class | paths | why |
|---|---|---|
| `DURABLE_SECURITY_STATE` | `/app/data`, `…/sessions`, `…/backups` | a security decision depends on it surviving |
| `AUDIT_EVIDENCE` | `/app/logs`, `…/diagnostics`, `…/exports` | the record of what the system did |
| `EPHEMERAL` | `/app/__pycache__`, `/tmp/jarvis_codeexec_*`, `/tmp/jarvis_sbx_*` | the last two are **harmful** to persist |
| `CACHE` | `/home/jarvis/.cache` | rebuildable; persisting is an operator optimisation |
| `OPERATOR_CONFIGURED` | `/app/.env` | arrives via `env_file`; a secret in a named volume outlives every `docker compose down` an operator would expect to clear it |

Persisting a containment workspace would **resurrect attacker-controlled files the
broker just proved it had deleted** — `core.containment` reports
`cleanup_status: ENFORCED` for exactly those directories. `must_not_persist_paths`
is therefore enforced as strictly as the durable list.

### The configuration

* `docker-compose.yml`: `jarvis_data:/app/data` added and declared.
* `Dockerfile`: `mkdir -p logs data data/sessions data/diagnostics data/backups
  data/exports && chown -R jarvis:jarvis logs data`, before `USER jarvis`, so a
  fresh named volume inherits a writable root.
* `test_docker_persistence_m68b.py` parses the compose YAML and the Dockerfile and
  asserts **coverage**, not a volume per row: an ancestor mount covers a
  descendant, a descendant never covers its ancestor, and `/app/database` is not
  covered by `/app/data`. It also asserts every directory
  `core.managed_paths` can produce is classified — an unclassified managed
  directory is an unanswered question, not an implicit `EPHEMERAL`.

### What is NOT claimed

**This buys survival of container recreation. It is not a crash-durability claim.**
A named volume says nothing about fsync, write ordering or host power loss, and
nothing in a Compose file can. `persistence_report()["guarantee"]` states it in
words, and `TestTheClaimIsNotOverstated` pins the wording so a later edit cannot
quietly upgrade it. Crash durability is the journal's own concern.

### Residual

* Restart/recreation behaviour is verified against the **declaration**, not against
  a running daemon. `tests/test_docker_and_startup_v69_m66a1.py` already
  demonstrates the pattern for a real `docker build` behind a daemon check; a
  recreate-and-recover integration test is future work, and is recorded as such
  rather than implied by the declaration tests.
* Postgres and Redis volumes were already present and were not touched.

---

## Verification

| gate | result |
|---|---|
| Focused M68B suites | **176 passed, 0 failed** (7 modules) |
| Absent-control suite | green |
| Cleanup proof (§10) | green |
| Falsification campaign | **78/78 detected, 0 survivors, 0 anchor errors** |
| `ruff check .` (from `jarvis/`) | clean |
| `bandit -r core tools -ll -q` | 0 Medium, 0 High, exit 0 |
| `compileall core tools scripts aura main.py __main__.py` | clean |
| `git diff --check` | clean |
| invisible/bidi source gate | clean |
| Control Plane verifier | PASS / 0 problems |

### Falsification campaign — `jarvis/scripts/mutation_campaign_m68b.py`

**75 mutations, 75 detected, 0 survivors, 0 anchor errors** on the second round.
The script runs a **preflight** first: every mapped test must PASS *and* collect at
least one test before a single mutation is applied. That is not ceremony — a mapped
test that is red, or merely **skipped**, reports DETECTED for every mutation and
turns the whole campaign into a green rubber stamp. The M68B selection needs
`fastapi`, `yaml`, `watchdog` and `httpx`, so the campaign is run with an
interpreter that has all four; without them it exits 2, "could not be evaluated",
rather than passing vacuously.

**The first round had 10 survivors, and every one was investigated rather than
counted.** Seven were real coverage gaps and three were mis-mappings. They fall
into three kinds, and the kinds are the interesting part:

| kind | survivors | what it taught |
|---|---|---|
| **self-referential assertion** | `A_retain_stdout_unbounded`, `A_retain_stderr_unbounded`, `C_ledger_capacity_absurd`, `C_size_limit_absurd` | the test compared an observed value against *the very constant it was meant to police*, so raising the constant to 1.2 GB satisfied it. Fixed by adding **absolute** ceilings a constant cannot move |
| **mutation masking** | `C_size_policy_removed` | the size policy is enforced twice — a stat gate before the read and a length check after. Deleting the stat gate changed nothing observable, because the second layer answered. The stat gate is the one that prevents the unbounded READ, so it now has its own proof: with reading made fatal, the refusal must still happen |
| **slack in the property** | `C_complete_before_success`, `C_scan_back_on_the_loop`, `D_diagnostic_hardcodes_host`, `E_harmful_list_emptied` | "at least two `to_thread` calls" survived removing one; "wrongly_persisted == []" is trivially true when the harmful list is emptied; a diagnostic compared against the resolver's *default*, which is the same literal the hardcoded value held. Each was replaced by a property that names what must hold |

The three mis-mappings (`C_workers_never_cancelled` and the two retention constants)
pointed at a fourth lesson: `asyncio.gather` cancels its children when the awaiting
task is cancelled, which is why deleting the explicit `cancel()` in the `finally`
was invisible. It is **not** invisible when a worker *raises* — gather re-raises and
leaves the siblings running — so that exit path now has its own test.

### The one deliberate baseline move

`BANDIT_LOW_BASELINE` 490 → **491**, for exactly one finding: `import subprocess`
in `core/bounded_output.py` is B404 ("consider the security implications"), and a
module whose job is reading subprocess pipes must import it. The finding is left
**visible and counted** rather than suppressed, so `BANDIT_SUPPRESSION_COUNT` stays
at two. Notably the module **starts no process and makes no subprocess call** — no
B603; argv construction and the `shell=False` discipline stay entirely in
`core/containment.py`. The two container workspace prefixes in
`core/deployment_persistence.py` that would read as B108 are assembled from parts —
the documented false-positive avoidance M66B used for the jail-internal tmpfs
targets, not a suppressed finding. Medium/High remain blocking at zero.

### Measured baselines

Measured on the subject tree, with the CI-parity interpreter the release gate
declares (CPython 3.11.16, `requirements/constraints-ci.txt`), from the repository
ROOT — the command `ci.yml` runs:

```
authoritative BEFORE   12312 passed ·  45 skipped · 0 failed   = 12357 collected
authoritative AFTER    12469 passed ·  48 skipped · 0 failed   = 12517 collected
scientific (54 modules) 3179 passed ·   2 skipped · 0 failed   =  3181 collected
```

**The collection totals reconcile exactly, and that is the check that matters.**
12357 + 176 new M68B tests − 17 (the whole `test_aura_backpressure_m68b.py` module
is skipped at collection in the CI-parity environment, which has no `fastapi`, so
its tests are not collected at all) + 1 (that module counted once as a skip) =
**12517**. The three extra skips are that module plus the two `TestAuraCleanup`
tests, which skip individually because their `importorskip` is inside the test.

The scientific selection is **unchanged at 3181**, the value M68A sealed: M68B
touched nothing the scientific suite measures. `state/m62/scientific-suite.json`
still digests `46ff7f62…`, and `git diff 7ae48a2 -- state/` is empty.

### Environment-dependent skip counts

Recorded separately and **never reconciled by hand**, per the invariant M68A
sealed: the authoritative collection total is the integrity check, not the
pass/skip split. `tests/test_aura_backpressure_m68b.py` needs `fastapi`, which the
CI runner does not install (dev+soc only), so it skips there and runs locally —
the same shape as M66B's "CI cannot witness the L3 process limits". Its
**AST-level** absent-control checks live in
`tests/test_resource_lifecycle_absent_controls_m68b.py`, need no import, and
therefore run on the release gate: an absent-control check that silently skips on
the gate is not a control.
