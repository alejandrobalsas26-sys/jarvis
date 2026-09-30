"""scripts/mutation_campaign_m68b.py — V69 M68B (§9): the falsification campaign.

For each mutation:
  0. (once, up front) run every MAPPED test UNMUTATED and require it to PASS,
  1. assert the anchor occurs EXACTLY ONCE in its file,
  2. apply it, run the mapped focused test in a FRESH subprocess,
  3. require the test to FAIL (the mutation must be DETECTED),
  4. restore the file (always).

Step 0 is not ceremony. A mapped test that is RED — or merely SKIPPED — before any
mutation reports DETECTED for every mutation and turns the whole campaign into a
green rubber stamp. The M68B test selection needs `fastapi`, `yaml`, `watchdog` and
`httpx`; run this with an interpreter that has them, or step 0 fails loudly instead
of the campaign passing vacuously. Exit 2 means "could not be evaluated".

A mutation whose mapped test stays GREEN is a LOAD-BEARING SURVIVOR: a resource or
lifecycle property with no test behind it. Requirement: 0 survivors, 0 anchor
errors, 0 vacuous mappings. Run from `jarvis/`.

Nothing here mutates sealed historical state: every target is a file M68B wrote or
changed.
"""
from __future__ import annotations

import os
import subprocess  # nosec B404 - this IS a test runner; every argv is a fixed list
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── files under mutation ─────────────────────────────────────────────────────
BO = "core/bounded_output.py"
CONT = "core/containment.py"
AURA = "aura/server.py"
CI = "core/code_intel.py"
OE = "core/ollama_endpoint.py"
LLM = "core/llm.py"
DP = "core/deployment_persistence.py"
COMPOSE = "docker-compose.yml"
DOCKERFILE = "Dockerfile"

# ── mapped tests ─────────────────────────────────────────────────────────────
TA = "tests/test_bounded_output_m68b.py"
TB = "tests/test_aura_backpressure_m68b.py"
TC = "tests/test_code_intel_lifecycle_m68b.py"
TD = "tests/test_ollama_endpoint_m68b.py"
TE = "tests/test_docker_persistence_m68b.py"
AC = "tests/test_resource_lifecycle_absent_controls_m68b.py"

A_BOUND = f"{TA}::TestRetentionBound"
A_EDGE = f"{TA}::TestBoundaries"
A_TERM = f"{TA}::TestTerminationTruth"
A_DEC = f"{TA}::TestDecoding"
A_ACC = f"{TA}::TestAccountingShape"
A_REAL = f"{TA}::TestRestrictedBackendIsBounded"

B_ISO = f"{TB}::TestProducerIsolation"
B_Q = f"{TB}::TestBoundedQueue"
B_DL = f"{TB}::TestSendDeadline"
B_LIFE = f"{TB}::TestLifecycle"
B_TASK = f"{TB}::TestBoundedFireAndForget"
B_END = f"{TB}::TestEndpointCleanup"
B_DIRECT = f"{TB}::TestDirectSendsAreBounded"

C_LEDGER = f"{TC}::TestAnalysisLedger"
C_LIFE = f"{TC}::TestAnalyzeFileLifecycle"
C_BOUND = f"{TC}::TestWatcherBounds"

D_RES = f"{TD}::TestResolution"
D_INV = f"{TD}::TestInvalidConfiguration"
D_ONE = f"{TD}::TestOneResolutionPath"
D_DIAG = f"{TD}::TestDiagnosticsReportTheRealEndpoint"
D_NOHARD = f"{TD}::TestNoHardcodedEndpointSurvives"

E_CLASS = f"{TE}::TestClassification"
E_COMP = f"{TE}::TestComposeSatisfiesTheClassification"
E_DOCK = f"{TE}::TestDockerfilePreparesTheDirectories"
E_CLAIM = f"{TE}::TestTheClaimIsNotOverstated"
E_COV = f"{TE}::TestCoverageSemantics"

AC_A = f"{AC}::TestOutputAccumulationIsAbsent"
AC_B = f"{AC}::TestWebsocketDeliveryIsBounded"
AC_C = f"{AC}::TestCodeIntelIsBounded"
AC_D = f"{AC}::TestEndpointResolutionIsWired"
AC_E = f"{AC}::TestDeploymentPersistenceIsWired"


def _mut(mid, cat, file, find, replace, test):
    return {"id": mid, "cat": cat, "file": file, "find": find,
            "replace": replace, "test": test}


MUTATIONS: list[dict] = []

# ── A · OUTPUT BUFFERING ─────────────────────────────────────────────────────
MUTATIONS += [
    # "remove the output cap": a retention limit so large it bounds nothing.
    _mut("A_retain_stdout_unbounded", "A_CAP", CONT,
         "CE_STDOUT_RETAIN_BYTES = CE_STDOUT_CAP * 4 + CE_OUTPUT_RESERVE_BYTES",
         "CE_STDOUT_RETAIN_BYTES = CE_STDOUT_CAP * 4 * 100000", A_REAL),
    _mut("A_retain_stderr_unbounded", "A_CAP", CONT,
         "CE_STDERR_RETAIN_BYTES = CE_STDERR_CAP * 4 + CE_OUTPUT_RESERVE_BYTES",
         "CE_STDERR_RETAIN_BYTES = CE_STDERR_CAP * 4 * 100000", A_REAL),
    # "bypass the bounded reader": retain everything the child sends.
    _mut("A_reader_retains_everything", "A_READER", BO,
         "                room = self._limit - self._retained",
         "                room = len(chunk)", A_BOUND),
    # The deadlock disguised as a bound: stop draining once full.
    _mut("A_reader_stops_at_limit", "A_READER", BO,
         "                self._seen += len(chunk)\n"
         "                room = self._limit - self._retained",
         "                self._seen += len(chunk)\n"
         "                if self._retained >= self._limit:\n"
         "                    break\n"
         "                room = self._limit - self._retained", AC_A),
    # Truncation stops being observable.
    _mut("A_truncated_always_false", "A_TRUTH", BO,
         "        return self.bytes_seen > self.bytes_retained",
         "        return False", A_BOUND),
    _mut("A_seen_not_counted", "A_TRUTH", BO,
         "                self._seen += len(chunk)",
         "                self._seen += 0", A_BOUND),
    _mut("A_chunk_kept_whole", "A_EDGE", BO,
         "                    keep = chunk[:room]",
         "                    keep = chunk", A_EDGE),
    # Termination truth.
    _mut("A_timeout_reported_as_exit", "A_TERM", BO,
         "        reason = TerminationReason.TIMEOUT",
         "        reason = TerminationReason.EXITED", A_REAL),
    _mut("A_no_kill_on_timeout", "A_TERM", BO,
         "            with contextlib.suppress(Exception):\n"
         "                on_timeout()",
         "            with contextlib.suppress(Exception):\n"
         "                pass", A_TERM),
    _mut("A_decode_strict", "A_DEC", BO,
         '        text = _universal_newlines(raw.decode(encoding, errors="replace"))',
         "        text = _universal_newlines(raw.decode(encoding))", A_DEC),
    _mut("A_stdin_never_written", "A_DEC", BO,
         "            self._stream.write(self._payload)",
         "            self._stream.write(b\"\")", A_DEC),
    # The accounting stops reaching the receipt.
    _mut("A_accounting_dropped", "A_ACC", CONT,
         "    receipt.output_accounting = acc\n    return out, err",
         "    return out, err", A_REAL),
    _mut("A_char_truncation_hidden", "A_ACC", CONT,
         '    acc["stdout"]["truncated"] = bool(\n'
         '        acc["stdout"]["truncated"] or len(out) < len(stdout_text))',
         '    acc["stdout"]["truncated"] = False', A_REAL),
    _mut("A_accounting_shape_broken", "A_ACC", BO,
         '            "bytes_retained": self.bytes_retained,',
         '            "bytes_retained": self.bytes_seen,', A_ACC),
]

# ── B · AURA BACKPRESSURE ────────────────────────────────────────────────────
MUTATIONS += [
    # "remove queue maxsize".
    _mut("B_queue_unbounded", "B_QUEUE", AURA,
         "        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, int(maxsize)))",
         "        self._queue: asyncio.Queue = asyncio.Queue()", B_Q),
    _mut("B_queue_capacity_absurd", "B_QUEUE", AURA,
         "AURA_CLIENT_QUEUE_MAX = 256",
         "AURA_CLIENT_QUEUE_MAX = 10000000", B_Q),
    # "bypass websocket send timeout".
    _mut("B_send_deadline_removed", "B_DEADLINE", AURA,
         "                    await asyncio.wait_for(self.ws.send_text(payload),\n"
         "                                           timeout=self._send_timeout)",
         "                    await self.ws.send_text(payload)", B_DL),
    _mut("B_send_deadline_infinite", "B_DEADLINE", AURA,
         "AURA_SEND_TIMEOUT_S = 5.0",
         "AURA_SEND_TIMEOUT_S = 1000000.0", B_DL),
    # Put the socket back on the producer's stack.
    _mut("B_producer_awaits_socket", "B_ISOLATION", AURA,
         "        for channel in list(self._channels.values()):\n"
         "            channel.offer(payload)",
         "        for channel in list(self._channels.values()):\n"
         "            await channel.ws.send_text(payload)", AC_B),
    # Overflow policy stops being observable.
    _mut("B_overflow_uncounted", "B_OVERFLOW", AURA,
         "            self.metrics.dropped_overflow += 1\n"
         '            self.metrics.last_drop_reason = "overflow_evicted_oldest"',
         "            pass", B_Q),
    _mut("B_overflow_keeps_oldest", "B_OVERFLOW", AURA,
         "            try:\n"
         "                self._queue.get_nowait()\n"
         "                self._queue.put_nowait(payload)\n"
         "            except (asyncio.QueueEmpty, asyncio.QueueFull):\n"
         "                self.metrics.dropped_overflow += 1\n"
         '                self.metrics.last_drop_reason = "overflow"\n'
         "                return False",
         "            self.metrics.dropped_overflow += 1\n"
         '            self.metrics.last_drop_reason = "overflow_evicted_oldest"\n'
         "            return False", B_Q),
    # Lifecycle: a writer that outlives its socket.
    _mut("B_writer_not_awaited", "B_LIFECYCLE", AURA,
         "        if not task.done():\n"
         "            task.cancel()\n"
         "            with contextlib.suppress(asyncio.CancelledError, Exception):\n"
         "                await task",
         "        return", B_LIFE),
    _mut("B_dead_client_not_reaped", "B_LIFECYCLE", AURA,
         "            if self._on_dead is not None:\n"
         "                try:\n"
         "                    self._on_dead(self.ws)",
         "            if self._on_dead is None:\n"
         "                try:\n"
         "                    self._on_dead(self.ws)", B_ISO),
    _mut("B_shutdown_leaves_writers", "B_LIFECYCLE", AURA,
         "        for channel in channels:\n"
         "            await channel.aclose(drain_s=drain_s)\n",
         "        return\n", B_LIFE),
    _mut("B_endpoint_cleanup_removed", "B_LIFECYCLE", AURA,
         "        await manager.aclose_client(ws)\n"
         "        _pending_ws_responses.pop(id(ws), None)",
         "        _pending_ws_responses.pop(id(ws), None)", B_END),
    # "no silent unbounded task creation".
    _mut("B_spawn_ceiling_removed", "B_TASKS", AURA,
         "    if len(_bounded_tasks) >= AURA_MAX_INFLIGHT_TASKS:",
         "    if False:", B_TASK),
    _mut("B_spawn_ceiling_absurd", "B_TASKS", AURA,
         "AURA_MAX_INFLIGHT_TASKS = 64",
         "AURA_MAX_INFLIGHT_TASKS = 100000", AC_B),
    _mut("B_spawn_drops_strong_ref", "B_TASKS", AURA,
         "    _bounded_tasks.add(task)",
         "    pass",  B_TASK),
    # The nine sends that BYPASS the bounded channel.
    _mut("B_direct_send_unbounded", "B_DEADLINE", AURA,
         "        await asyncio.wait_for(ws.send_json(payload), timeout=AURA_SEND_TIMEOUT_S)",
         "        await ws.send_json(payload)", AC_B),
    _mut("B_welcome_send_unbounded", "B_DEADLINE", AURA,
         "    await _ws_send(ws, {\n"
         '        "type":      "system",',
         "    await ws.send_json({\n"
         '        "type":      "system",', AC_B),
    _mut("B_direct_send_swallows_the_deadline", "B_DEADLINE", AURA,
         "        await asyncio.wait_for(ws.send_json(payload), timeout=AURA_SEND_TIMEOUT_S)\n"
         "        return True",
         "        await asyncio.wait_for(ws.send_json(payload), timeout=3600)\n"
         "        return True", B_DIRECT),
    _mut("B_hud_command_untracked", "B_TASKS", AURA,
         "                        _spawn_bounded(\n"
         "                            _handle_hud_command(raw, ws, _executor_ref, broadcast),\n"
         '                            name="hud-command")',
         "                        asyncio.create_task(\n"
         "                            _handle_hud_command(raw, ws, _executor_ref, broadcast))",
         AC_B),
]

# ── C · CODE_INTEL RESOURCE / LIFECYCLE ──────────────────────────────────────
MUTATIONS += [
    _mut("C_queue_unbounded", "C_QUEUE", CI,
         "        queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, int(queue_max)))",
         "        queue: asyncio.Queue = asyncio.Queue()", AC_C),
    _mut("C_queue_capacity_absurd", "C_QUEUE", CI,
         "CI_QUEUE_MAX = 64", "CI_QUEUE_MAX = 10000000", C_BOUND),
    _mut("C_worker_limit_removed", "C_WORKERS", CI,
         "CI_WORKERS = 2", "CI_WORKERS = 100000", C_BOUND),
    # "mark code_intel complete before success".
    _mut("C_complete_before_success", "C_LIFECYCLE", CI,
         "    if not book.claim(key):\n        return None",
         "    if not book.claim(key):\n        return None\n    book.complete(key)",
         AC_C),
    _mut("C_read_error_is_terminal", "C_LIFECYCLE", CI,
         '        book.fail(key, "read_error")',
         '        book.reject(key, "read_error")', C_LIFE),
    _mut("C_cancellation_is_a_verdict", "C_LIFECYCLE", CI,
         '        book.fail(key, "cancelled")     # shutdown is not a verdict',
         '        book.complete(key)', C_LIFE),
    _mut("C_running_never_released", "C_LIFECYCLE", CI,
         "        if book.state(key) is AnalysisState.RUNNING:\n"
         '            book.fail(key, "interrupted")',
         "        pass", AC_C),
    _mut("C_duplicate_event_requeued", "C_DEDUP", CI,
         "                if entry.state in (AnalysisState.PENDING, AnalysisState.RUNNING):\n"
         "                    return False",
         "                if False:\n                    return False", C_LEDGER),
    _mut("C_retry_forever", "C_RETRY", CI,
         "            if entry.attempts >= self._max_attempts:\n"
         "                return False\n"
         "            entry.state = AnalysisState.RUNNING",
         "            entry.state = AnalysisState.RUNNING", C_LEDGER),
    _mut("C_ledger_unbounded", "C_STATE", CI,
         "        while len(self._entries) > self._max_entries:",
         "        while False:", C_LEDGER),
    _mut("C_ledger_capacity_absurd", "C_STATE", CI,
         "CI_LEDGER_MAX = 2048", "CI_LEDGER_MAX = 100000000", C_BOUND),
    _mut("C_ledger_leaks_file_names", "C_STATE", CI,
         '            return {"tracked": len(self._entries), "capacity": self._max_entries,',
         '            return {"tracked": list(self._entries), "capacity": self._max_entries,',
         C_LEDGER),
    # Size policy.
    _mut("C_size_policy_removed", "C_SIZE", CI,
         "    size = file_path.stat().st_size\n    if size > max_bytes:",
         "    size = file_path.stat().st_size\n    if False:", C_LIFE),
    _mut("C_size_limit_absurd", "C_SIZE", CI,
         "CI_MAX_FILE_BYTES = 32 * 1024 * 1024",
         "CI_MAX_FILE_BYTES = 32 * 1024 * 1024 * 100000", C_BOUND),
    # Blocking work back on the event loop.
    _mut("C_read_back_on_the_loop", "C_LOOP", CI,
         "        data = await asyncio.to_thread(\n"
         "            _read_within_policy, file_path, max_bytes=max_bytes)",
         "        data = file_path.read_bytes()", AC_C),
    _mut("C_scan_back_on_the_loop", "C_LOOP", CI,
         "        scan = await asyncio.to_thread(_static_scan, data)",
         "        scan = _static_scan(data)", AC_C),
    # Shutdown.
    _mut("C_observer_never_stopped", "C_SHUTDOWN", CI,
         "                observer.stop()\n                observer.join(5.0)",
         "                pass", C_BOUND),
    _mut("C_workers_never_cancelled", "C_SHUTDOWN", CI,
         "        for task in worker_tasks:\n            task.cancel()",
         "        for task in worker_tasks:\n            pass", C_BOUND),
    # The unawaited-coroutine class of defect M68B found.
    _mut("C_yara_not_awaited", "C_ASYNC", CI,
         "        hits = await scan_command(",
         "        hits = scan_command(", AC_C),
]

# ── D · OLLAMA ENDPOINT ──────────────────────────────────────────────────────
MUTATIONS += [
    # "bypass endpoint resolver" / "point inference back to localhost".
    _mut("D_inference_hardcodes_localhost", "D_WIRING", LLM,
         "            base_url=ollama_openai_base_url(),",
         '            base_url="http://localhost:11434/v1",', AC_D),
    _mut("D_env_ignored", "D_RESOLVE", OE,
         "        candidate = env.get(OLLAMA_HOST_ENV) or \"\"",
         '        candidate = ""', D_RES),
    _mut("D_openai_suffix_dropped", "D_RESOLVE", OE,
         '        return self.base_url.rstrip("/") + "/v1"',
         "        return self.base_url", D_ONE),
    _mut("D_scheme_allowlist_removed", "D_VALIDATE", OE,
         "    if scheme not in ALLOWED_SCHEMES:",
         "    if False:", D_INV),
    _mut("D_path_component_tolerated", "D_VALIDATE", OE,
         '    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:',
         "    if False:", D_INV),
    _mut("D_invalid_never_reported", "D_VALIDATE", OE,
         "        return OllamaEndpoint(base_url=DEFAULT_OLLAMA_BASE_URL, source=source,\n"
         "                              raw=candidate, valid=False, reason=reason)",
         "        return OllamaEndpoint(base_url=DEFAULT_OLLAMA_BASE_URL, source=source,\n"
         "                              raw=candidate, valid=True, reason=None)", D_INV),
    _mut("D_strict_gate_never_raises", "D_VALIDATE", OE,
         "    if not endpoint.valid:\n        raise InvalidOllamaEndpoint(",
         "    if False:\n        raise InvalidOllamaEndpoint(", D_INV),
    _mut("D_bare_host_not_normalised", "D_RESOLVE", OE,
         '    if "://" not in candidate:\n        candidate = "http://" + candidate',
         '    if False:\n        candidate = "http://" + candidate', D_RES),
    _mut("D_ipv6_brackets_dropped", "D_RESOLVE", OE,
         '    rendered = f"[{host}]" if ":" in host else host',
         "    rendered = host", D_RES),
    _mut("D_provenance_always_default", "D_DIAG", OE,
         '        source = "environment" if candidate.strip() else "default"',
         '        source = "default"', D_RES),
    _mut("D_diagnostic_hardcodes_host", "D_DIAG", "core/ollama_env.py",
         '            "endpoint": endpoint_report(),',
         '            "endpoint": {"base_url": "http://127.0.0.1:11434"},', D_DIAG),
    _mut("D_legacy_facade_reimplements", "D_WIRING", "core/model_router.py",
         "    return ollama_base_url(raw if (raw is None or raw.strip()) else None)",
         '    return "http://127.0.0.1:11434"', D_NOHARD),
]

# ── E · DOCKER PERSISTENCE ───────────────────────────────────────────────────
MUTATIONS += [
    # "remove required durable volume mapping".
    _mut("E_data_volume_removed", "E_VOLUME", COMPOSE,
         "      - jarvis_data:/app/data\n      - jarvis_logs:/app/logs",
         "      - jarvis_logs:/app/logs", E_COMP),
    _mut("E_logs_volume_removed", "E_VOLUME", COMPOSE,
         "      - jarvis_logs:/app/logs\n    depends_on:",
         "    depends_on:", E_COMP),
    _mut("E_volume_declaration_removed", "E_VOLUME", COMPOSE,
         "  jarvis_data:\n  jarvis_logs:", "  jarvis_logs:", E_COMP),
    _mut("E_data_mounted_too_deep", "E_VOLUME", COMPOSE,
         "      - jarvis_data:/app/data\n", "      - jarvis_data:/app/data/sessions\n",
         E_COMP),
    # Persisting what must not be persisted.
    _mut("E_sandbox_workspace_persisted", "E_HARMFUL", COMPOSE,
         "      - jarvis_data:/app/data\n",
         "      - jarvis_data:/app/data\n      - jarvis_tmp:/tmp\n", E_COMP),
    # The classification itself.
    _mut("E_journal_reclassified_cache", "E_CLASS", DP,
         '        container_path="/app/data",\n'
         "        classification=StateClass.DURABLE_SECURITY_STATE,",
         '        container_path="/app/data",\n'
         "        classification=StateClass.CACHE,", E_CLASS),
    _mut("E_nothing_is_durable", "E_CLASS", DP,
         "MUST_PERSIST_CLASSES: frozenset[StateClass] = frozenset({\n"
         "    StateClass.DURABLE_SECURITY_STATE,\n"
         "    StateClass.AUDIT_EVIDENCE,\n"
         "})",
         "MUST_PERSIST_CLASSES: frozenset[StateClass] = frozenset()", AC_E),
    _mut("E_coverage_accepts_a_descendant", "E_COVERAGE", DP,
         "        if container_path == t or container_path.startswith(t + \"/\"):",
         "        if container_path == t or t.startswith(container_path):", E_COV),
    _mut("E_coverage_ignores_boundaries", "E_COVERAGE", DP,
         "        if container_path == t or container_path.startswith(t + \"/\"):",
         "        if container_path.startswith(t):", E_COV),
    _mut("E_harmful_list_emptied", "E_HARMFUL", DP,
         "    return tuple(sorted(e.container_path for e in CLASSIFIED_STATE\n"
         "                        if e.harmful_to_persist))",
         "    return ()", E_CLASS),
    _mut("E_guarantee_overstated", "E_CLAIM", DP,
         '        "guarantee": "container recreation; NOT crash durability",',
         '        "guarantee": "fully crash-durable",', E_CLAIM),
    # The image must prepare what the volume is mounted onto.
    _mut("E_dockerfile_no_data_dir", "E_IMAGE", DOCKERFILE,
         "RUN mkdir -p logs data data/sessions data/diagnostics data/backups data/exports \\\n"
         "    && chown -R jarvis:jarvis logs data",
         "RUN mkdir -p logs && chown jarvis:jarvis logs", E_DOCK),
    _mut("E_dockerfile_no_chown", "E_IMAGE", DOCKERFILE,
         "    && chown -R jarvis:jarvis logs data",
         "    && true", E_DOCK),
    _mut("E_dockerfile_runs_as_root", "E_IMAGE", DOCKERFILE,
         "USER jarvis", "# USER jarvis", E_DOCK),
    _mut("E_dockerfile_drops_endpoint_env", "E_IMAGE", DOCKERFILE,
         "ENV OLLAMA_HOST=http://host.docker.internal:11434",
         "# no endpoint configured", E_DOCK),
]


def _pytest(target: str, timeout: int = 300) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(  # nosec B603 - fixed argv test runner
        [sys.executable, "-m", "pytest", "-x", "-q", "--no-header",
         "-p", "no:cacheprovider", target],
        cwd=_ROOT, capture_output=True, text=True, env=env, timeout=timeout)


def _preflight(targets: list[str]) -> list[str]:
    """Every mapped test must PASS, and collect at least one test, BEFORE any
    mutation. A red or skipped mapping makes every mutation look DETECTED."""
    bad: list[str] = []
    print(f"PREFLIGHT — {len(targets)} mapped test target(s)")
    for target in sorted(targets):
        proc = _pytest(target)
        tail = (proc.stdout or "").strip().splitlines()
        summary = tail[-1] if tail else "<no output>"
        if proc.returncode != 0:
            bad.append(f"{target}: RED before mutation ({summary})")
            print(f"  [RED ] {target}")
        elif " passed" not in summary or "no tests ran" in summary:
            bad.append(f"{target}: vacuous ({summary})")
            print(f"  [VOID] {target} — {summary}")
        else:
            print(f"  [ OK ] {target} — {summary}")
    return bad


def _run() -> int:
    total = len(MUTATIONS)
    targets = sorted({m["test"] for m in MUTATIONS})
    print(f"M68B FALSIFICATION CAMPAIGN — {total} mutations, "
          f"{len(targets)} mapped targets\n")

    vacuous = _preflight(targets)
    if vacuous:
        print("\nPREFLIGHT FAILED — the campaign would be vacuous:")
        for line in vacuous:
            print(f"  {line}")
        print("M68B_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
        return 2

    survivors: list[str] = []
    anchor_errors: list[str] = []
    detected = 0
    print(f"\nMUTATING — {total} mutations\n")
    for m in MUTATIONS:
        path = os.path.join(_ROOT, m["file"])
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
        count = original.count(m["find"])
        if count != 1:
            anchor_errors.append(f"{m['id']}: anchor occurs {count}x in {m['file']}")
            print(f"  [ANCHOR ERR] {m['id']:34s} {count}x")
            continue
        mutated = original.replace(m["find"], m["replace"], 1)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(mutated)
            proc = _pytest(m["test"])
            if proc.returncode != 0:
                detected += 1
                print(f"  [DETECTED] {m['id']:34s} ({m['cat']})")
            else:
                survivors.append(m["id"])
                print(f"  [SURVIVOR] {m['id']:34s} ({m['cat']}) — NO TEST FAILED")
        finally:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)

    print(f"\n{'=' * 70}")
    print(f"mutations:      {total}")
    print(f"detected:       {detected}")
    print(f"survivors:      {len(survivors)}  {survivors if survivors else ''}")
    print(f"anchor errors:  {len(anchor_errors)}  {anchor_errors if anchor_errors else ''}")
    ok = not survivors and not anchor_errors
    print(f"M68B_MUTATION_CAMPAIGN: {'PASS' if ok else 'FAIL'} "
          f"({detected}/{total} detected, {len(survivors)} survivors)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_run())
