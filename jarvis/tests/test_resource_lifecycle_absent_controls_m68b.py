"""V69 M68B — structural tests for ABSENT resource/lifecycle controls.

WHY THESE ARE NOT MUTATION TESTS
--------------------------------
M68A established the rule and M68B extends it. A mutation campaign can only break
a line that EXISTS: flip ``maxsize=64`` to ``maxsize=0`` and a test fails, which
teaches you that line is load-bearing. It teaches you nothing about the queue that
never had a ``maxsize`` argument at all, the send that was never wrapped in a
deadline, or the volume that was never declared.

Every finding in M68B was exactly that shape — a control that was ABSENT, not a
control that was wrong. So each test here asks two questions:

    "could the required control be entirely missing?"
    "is it enforced at the REAL composition/call site?"

The second matters as much as the first. ``core.ollama_endpoint`` can be perfect
while ``LLM.__init__`` still passes a literal; ``core.deployment_persistence`` can
declare ``/app/data`` while ``docker-compose.yml`` never mounts it. A control that
exists but is not wired is indistinguishable from an absent one at runtime.

These are written against the AST and against parsed configuration, never against
raw text, so a comment or a docstring explaining the old defect cannot satisfy
them — M68A's first draft of one such probe passed for exactly that reason.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

CORE = PACKAGE_ROOT / "core"
AURA_SERVER = PACKAGE_ROOT / "aura" / "server.py"
CONTAINMENT = CORE / "containment.py"
CODE_INTEL = CORE / "code_intel.py"
LLM = CORE / "llm.py"
COMPOSE = PACKAGE_ROOT / "docker-compose.yml"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found — the control it carries may be absent")


def _method(tree: ast.Module, cls: str, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and child.name == name:
                    return child
    raise AssertionError(f"{cls}.{name}() not found")


def _call_names(node) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _dotted(func) -> str:
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    return ".".join(reversed(parts))


# ── §A  no output path may accumulate unbounded subprocess output ───────────
class TestOutputAccumulationIsAbsent:
    def test_the_containment_broker_never_calls_communicate(self):
        """``communicate()`` IS the accumulation. Its presence anywhere in the
        broker means some path reads a pipe to EOF into memory."""
        offenders = [
            node.lineno for node in ast.walk(_tree(CONTAINMENT))
            if isinstance(node, ast.Call) and _dotted(node.func).endswith("communicate")
        ]
        assert offenders == [], (
            f"core/containment.py calls communicate() at lines {offenders}; the "
            "output cap would again bound the returned string, not the memory")

    def test_every_popen_in_the_broker_is_read_through_the_bounded_reader(self):
        """The composition-site question: a bounded reader that some Popen does
        not go through is not a control, it is a module."""
        tree = _tree(CONTAINMENT)
        popens, bounded = 0, 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _dotted(node.func)
            if name.endswith("Popen"):
                popens += 1
            if name.endswith("capture_bounded"):
                bounded += 1
        assert popens >= 2, "the two execution backends were expected"
        assert bounded >= popens, (
            f"{popens} Popen call(s) but only {bounded} bounded capture(s)")

    def test_no_broker_execution_uses_capture_output(self):
        """``capture_output=True`` is ``communicate()`` with a different spelling.
        The read-only capability probe is exempt: fixed argv, no untrusted child."""
        tree = _tree(CONTAINMENT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _dotted(node.func).endswith("run"):
                continue
            kw = {k.arg for k in node.keywords}
            if "capture_output" not in kw:
                continue
            # Allowed only inside probe_capabilities, which runs /usr/bin/true.
            enclosing = [n for n in ast.walk(tree)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and node.lineno >= n.lineno
                         and node.lineno <= (n.end_lineno or n.lineno)]
            assert any(n.name == "probe_capabilities" for n in enclosing), (
                f"capture_output at containment.py:{node.lineno} is outside the "
                "read-only capability probe")

    def test_the_retention_bound_is_a_finite_byte_count(self):
        from core.containment import (CE_STDERR_CAP, CE_STDERR_RETAIN_BYTES,
                                      CE_STDOUT_CAP, CE_STDOUT_RETAIN_BYTES)
        for retain, cap in ((CE_STDOUT_RETAIN_BYTES, CE_STDOUT_CAP),
                            (CE_STDERR_RETAIN_BYTES, CE_STDERR_CAP)):
            assert isinstance(retain, int) and 0 < retain < 64 * 1024 * 1024
            assert retain >= cap, "the retention bound cannot deliver the cap"

    def test_the_bounded_reader_keeps_draining_after_the_limit(self):
        """If the read loop stopped at the limit, the pipe would fill and the
        child would block — a deadlock wearing a bound's clothes."""
        tree = _tree(CORE / "bounded_output.py")
        run = _method(tree, "_BoundedReader", "run")
        breaks = [n for n in ast.walk(run) if isinstance(n, ast.Break)]
        # Exactly one break: end-of-stream. None of them may be guarded by the limit.
        assert len(breaks) == 1, "more than one exit from the drain loop"
        loop = next(n for n in ast.walk(run) if isinstance(n, ast.While))
        assert isinstance(loop.test, ast.Constant) and loop.test.value is True, (
            "the drain loop is conditional; a child could hold it open or be "
            "blocked by it")

    def test_the_receipt_publishes_the_positive_contract(self):
        """Truncation must be observable evidence, not silent loss."""
        from core.containment import ContainmentReceipt
        assert "output_accounting" in ContainmentReceipt.__dataclass_fields__


# ── §B  every websocket delivery path has a bound ───────────────────────────
class TestWebsocketDeliveryIsBounded:
    def test_the_producer_path_awaits_no_socket(self):
        """``broadcast()`` must contain no await at all beyond its own frame:
        an await on a send is the producer-critical path the audit named."""
        tree = _tree(AURA_SERVER)
        fn = _method(tree, "BroadcastManager", "broadcast")
        awaits = [n for n in ast.walk(fn) if isinstance(n, ast.Await)]
        assert awaits == [], (
            "BroadcastManager.broadcast still awaits inside the fan-out; one slow "
            "consumer is back on the producer's stack")
        assert "offer" in _call_names(fn), "the fan-out no longer hands off frames"

    def test_every_per_connection_queue_declares_a_maxsize(self):
        tree = _tree(AURA_SERVER)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _dotted(node.func).endswith("Queue"):
                assert any(k.arg == "maxsize" for k in node.keywords), (
                    f"aura/server.py:{node.lineno} creates an unbounded Queue")

    def test_the_writer_send_is_deadline_bounded(self):
        tree = _tree(AURA_SERVER)
        run = _method(tree, "_ClientChannel", "_run")
        waits = [n for n in ast.walk(run)
                 if isinstance(n, ast.Call) and _dotted(n.func).endswith("wait_for")]
        assert waits, "the owned writer sends with no deadline"
        assert any(any(k.arg == "timeout" for k in w.keywords) for w in waits)

    def test_every_direct_websocket_send_is_deadline_bounded(self):
        """The channel writers are bounded; nine sends BYPASSED them — the welcome
        frame in `_ws_endpoint` and eight replies in `_handle_hud_command`. None is
        producer-critical, but each holds its own task for as long as the client
        refuses to read, and 64 wedged HUD replies exhaust the in-flight ceiling.
        A claim that every delivery path has a bound must cover them."""
        tree = _tree(AURA_SERVER)
        allowed = {"_ws_send", "_run"}
        offenders = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name in allowed:
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = _dotted(node.func)
                if name.endswith(("ws.send_json", "ws.send_text")):
                    offenders.append(f"{fn.name}() line {node.lineno}: {name}")
        assert offenders == [], (
            f"unbounded direct websocket send(s): {offenders}; route them through "
            "_ws_send, which applies AURA_SEND_TIMEOUT_S")

    def test_the_bounded_sender_actually_applies_a_deadline(self):
        """Non-vacuity for the rule above: the single allowed sender must itself be
        bounded, or the rule just relocates the defect."""
        tree = _tree(AURA_SERVER)
        fn = _function(tree, "_ws_send")
        waits = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and _dotted(n.func).endswith("wait_for")]
        assert waits, "_ws_send sends with no deadline"
        assert any(any(k.arg == "timeout" for k in w.keywords) for w in waits)

    def test_no_unbounded_task_creation_survives_in_the_server(self):
        """Every ``create_task`` must be inside the bounded spawner or a
        connection's own writer start — never one per event."""
        tree = _tree(AURA_SERVER)
        allowed = {"_spawn_bounded", "start", "_lifespan"}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in allowed:
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and _dotted(child.func).endswith("create_task"):
                    raise AssertionError(
                        f"aura/server.py:{child.lineno} in {node.name}() creates an "
                        "untracked task; route it through _spawn_bounded")

    def test_the_bounded_spawner_has_a_finite_ceiling(self):
        """Read from the AST, not by import: aura/server.py pulls in FastAPI, which
        the CI runner does not install, and an absent-control check that silently
        skips on the release gate is not a control."""
        tree = _tree(AURA_SERVER)
        ceiling = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "AURA_MAX_INFLIGHT_TASKS"
                            for t in node.targets)
                    and isinstance(node.value, ast.Constant)):
                ceiling = node.value.value
        assert isinstance(ceiling, int), "no finite in-flight ceiling is declared"
        assert 0 < ceiling < 100_000

    def test_the_endpoint_cleans_up_in_a_finally(self):
        """Cleanup inside ``except WebSocketDisconnect`` only is cleanup that a
        transport error skips."""
        tree = _tree(AURA_SERVER)
        fn = _function(tree, "_ws_endpoint")
        tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
        assert tries, "_ws_endpoint has no finally; a non-disconnect error leaks"
        names = set()
        for t in tries:
            for stmt in t.finalbody:
                names |= _call_names(stmt)
        assert "aclose_client" in names or "disconnect" in names


# ── §C  every code_intel producer reaches a bounded queue/worker system ─────
class TestCodeIntelIsBounded:
    def test_the_inbox_queue_declares_a_maxsize(self):
        tree = _tree(CODE_INTEL)
        queues = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and _dotted(n.func).endswith("Queue")]
        assert queues, "no queue at all in the watcher"
        for node in queues:
            assert any(k.arg == "maxsize" for k in node.keywords), (
                f"core/code_intel.py:{node.lineno} creates an unbounded Queue")

    def test_no_task_is_spawned_per_arriving_file(self):
        """``create_task(analyze_file(...))`` per event WAS the concurrency."""
        tree = _tree(CODE_INTEL)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _dotted(node.func).endswith("create_task"):
                continue
            inner = node.args[0] if node.args else None
            called = _dotted(inner.func) if isinstance(inner, ast.Call) else ""
            assert not called.endswith("analyze_file"), (
                f"core/code_intel.py:{node.lineno} spawns one task per file")

    def test_the_watcher_stops_and_joins_its_observer(self):
        tree = _tree(CODE_INTEL)
        fn = _function(tree, "start_inbox_watcher")
        tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
        assert tries, "start_inbox_watcher has no finally: shutdown is best-effort"
        names: set[str] = set()
        for t in tries:
            for stmt in t.finalbody:
                names |= _call_names(stmt)
        assert {"stop", "join", "cancel"} <= names, (
            f"teardown is incomplete: {sorted(names)}")

    def test_completion_is_never_recorded_before_the_work(self):
        """The defect in one property: inside ``analyze_file`` the FIRST ledger
        transition must be a claim, and ``complete`` must come after the report
        write, not before it.

        The report write is looked up as an ATTRIBUTE, not a call, because it is
        handed to ``asyncio.to_thread`` — ``to_thread(path.write_text, ...)``. A
        probe that only walked Call nodes found nothing and would have passed
        whatever the order was.
        """
        tree = _tree(CODE_INTEL)

        def _marks(fn):
            found: list[tuple[str, int]] = []
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    name = _dotted(node.func).split(".")[-1]
                    if name in {"claim", "complete", "fail", "reject"}:
                        found.append((name, node.lineno))
                elif isinstance(node, ast.Attribute) and node.attr == "write_text":
                    found.append(("write_text", node.lineno))
            found.sort(key=lambda pair: pair[1])
            return [k for k, _ in found]

        entry = _marks(_function(tree, "analyze_file"))
        assert entry and entry[0] == "claim", (
            f"the first ledger transition in analyze_file is {entry[:1]}")

        # Find whichever function writes the report, rather than hardcoding a
        # name: the ordering property belongs to that function wherever it lives.
        writers = [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and "write_text" in _marks(n)]
        assert writers, "nothing in code_intel writes a report any more"
        for fn in writers:
            kinds = _marks(fn)
            assert "complete" in kinds, (
                f"{fn.name}() writes a report but never records success")
            assert kinds.index("write_text") < kinds.index("complete"), (
                f"{fn.name}() marks COMPLETED before the report is written")

    def test_the_lifecycle_guard_never_records_success(self):
        """The guard claims and records failure; only the function that does the
        WORK may record success. The falsification campaign inserted
        `book.complete(key)` immediately after the claim and every behavioural test
        stayed green, because a later `fail` overwrote it and the success case ended
        COMPLETED either way. Ordering alone cannot see that; separation can."""
        tree = _tree(CODE_INTEL)
        guard = _function(tree, "analyze_file")
        recorded = {_dotted(n.func).split(".")[-1] for n in ast.walk(guard)
                    if isinstance(n, ast.Call)}
        allowed = {"claim", "fail"}
        forbidden = {"complete", "reject"} & recorded
        assert forbidden == set(), (
            f"analyze_file records {sorted(forbidden)}; the guard does not do the "
            "work, so it cannot know the outcome")
        assert allowed & recorded == allowed, (
            "the guard no longer claims and records failure")

    def test_every_blocking_helper_is_offloaded_at_every_call_site(self):
        """Counting `to_thread` calls was slack: removing one offload left two
        others and the count still passed. Name the helpers instead — each is
        blocking, and each of their call sites must be a `to_thread` argument."""
        tree = _tree(CODE_INTEL)
        blocking = {"_read_within_policy", "_static_scan", "_shannon_entropy",
                    "_extract_strings"}
        offloaded: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _dotted(node.func).endswith("to_thread"):
                for arg in node.args:
                    offloaded.add(id(arg))
        offenders = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = _dotted(node.func).split(".")[-1]
                if name in blocking and id(node.func) not in offloaded:
                    offenders.append(f"{name} at line {node.lineno} in {fn.name}()")
        assert offenders == [], (
            f"blocking helper(s) called directly on the event loop: {offenders}")

    def test_no_path_may_leave_a_file_running(self):
        """The defect M68B's own tests found: a cancellation between the claim and
        any recorded outcome left the entry RUNNING, and RUNNING is never
        claimable again — the old `_ANALYZED` poison, reintroduced.

        The control is a `finally` that re-checks the state, so it holds over
        EVERY exit path rather than at the await points someone thought of."""
        tree = _tree(CODE_INTEL)
        fn = _function(tree, "analyze_file")
        tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
        assert tries, "analyze_file has no finally; an interrupted claim is forever"
        guarded = False
        for t in tries:
            src = ast.dump(ast.Module(body=t.finalbody, type_ignores=[]))
            if "RUNNING" in src and "fail" in src:
                guarded = True
        assert guarded, (
            "the finally does not re-check for a RUNNING entry, so a cancellation "
            "can still leave a file permanently unclaimable")
        handlers = {ast.dump(h.type) if h.type else "" for t in tries
                    for h in t.handlers}
        assert any("CancelledError" in h for h in handlers), (
            "cancellation is not distinguished from a failure")

    def test_the_analysed_state_is_bounded(self):
        """A module-level ``set`` that only grows is the absent control here."""
        from core import code_intel
        assert not hasattr(code_intel, "_ANALYZED"), (
            "the unbounded _ANALYZED set is back")
        snap = code_intel.ledger_snapshot()
        assert isinstance(snap["capacity"], int) and snap["capacity"] > 0

    def test_blocking_file_work_leaves_the_event_loop(self):
        """Two properties, module-wide rather than per-function: the blocking steps
        are offloaded at all, and no coroutine performs a whole-file read inline."""
        tree = _tree(CODE_INTEL)
        to_thread = [n for n in ast.walk(tree)
                     if isinstance(n, ast.Call) and _dotted(n.func).endswith("to_thread")]
        assert len(to_thread) >= 2, (
            "the read and the CPU-bound scan still run on the event loop")
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and _dotted(node.func).endswith("read_bytes"):
                    raise AssertionError(
                        f"core/code_intel.py:{node.lineno} in async {fn.name}() "
                        "reads a whole file on the event loop")

    def test_no_coroutine_in_code_intel_is_called_without_await(self):
        """M68B found `scan_command(...)` — an `async def` invoked with no await,
        whose result was iterated inside a bare `except`, so the YARA hit list was
        always empty and the severity grade silently degraded. A call to a known
        coroutine function must be awaited."""
        tree = _tree(CODE_INTEL)
        coroutines = {"scan_command", "deep_disassemble", "analyze_file",
                      "_analyze_claimed", "speak_async"}
        awaited: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
                awaited.add(id(node.value))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or id(node) in awaited:
                continue
            name = _dotted(node.func).split(".")[-1]
            if name not in coroutines:
                continue
            # A coroutine handed to create_task/_spawn_bounded/gather is scheduled,
            # not dropped — that is a different, legitimate shape.
            offenders.append((name, node.lineno))
        scheduled = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _dotted(node.func).split(".")[-1] in {
                    "create_task", "gather", "_spawn_bounded", "ensure_future"}:
                for arg in node.args:
                    if isinstance(arg, ast.Call):
                        scheduled.add(arg.lineno)
        real = [(n, ln) for n, ln in offenders if ln not in scheduled]
        assert real == [], f"coroutine(s) called without await: {real}"


# ── §D  every Ollama client resolves through the canonical resolver ────────
class TestEndpointResolutionIsWired:
    def test_the_inference_client_base_url_is_computed_not_written(self):
        """The composition site. ``core.ollama_endpoint`` being correct is not
        the control; ``LLM.__init__`` USING it is."""
        tree = _tree(LLM)
        init = _method(tree, "LLM", "__init__")
        for node in ast.walk(init):
            if not isinstance(node, ast.Call):
                continue
            if not _dotted(node.func).endswith("AsyncOpenAI"):
                continue
            base = next((k.value for k in node.keywords if k.arg == "base_url"), None)
            assert base is not None, "AsyncOpenAI constructed with no base_url"
            assert not isinstance(base, ast.Constant), (
                "LLM.__init__ hardcodes its inference endpoint again")
            assert isinstance(base, ast.Call), "base_url is not resolved by a call"

    def test_no_core_module_constructs_an_ollama_client_from_a_literal(self):
        offenders = []
        for path in sorted(CORE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not _dotted(node.func).endswith(("AsyncOpenAI", "OpenAI")):
                    continue
                base = next((k.value for k in node.keywords if k.arg == "base_url"),
                            None)
                if isinstance(base, ast.Constant) and "11434" in str(base.value):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], offenders

    def test_the_canonical_module_is_the_only_one_reading_the_variable(self):
        from core.ollama_endpoint import OLLAMA_HOST_ENV
        offenders = []
        for path in sorted(CORE.glob("*.py")):
            if path.name in ("ollama_endpoint.py", "ollama_env.py"):
                continue          # the resolver, and the truthful env REPORT
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _dotted(node.func).endswith("getenv"):
                    arg = node.args[0] if node.args else None
                    if isinstance(arg, ast.Constant) and arg.value == OLLAMA_HOST_ENV:
                        offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], (
            f"{offenders} read OLLAMA_HOST directly instead of resolving it")


# ── §E  every required durable path is covered by deployment configuration ──
class TestDeploymentPersistenceIsWired:
    def test_the_compose_file_satisfies_the_classification(self):
        yaml = pytest.importorskip("yaml")
        from core.deployment_persistence import persistence_report
        compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        targets = []
        for entry in compose["services"]["jarvis"].get("volumes") or []:
            parts = str(entry).split(":")
            if len(parts) >= 2:
                targets.append(parts[1])
        report = persistence_report(targets)
        assert report["missing"] == [], (
            f"declared-durable paths with no volume: {report['missing']}")

    def test_the_classification_is_not_empty(self):
        from core.deployment_persistence import required_persistent_paths
        assert required_persistent_paths(), (
            "nothing is declared durable — the control is absent, not satisfied")
