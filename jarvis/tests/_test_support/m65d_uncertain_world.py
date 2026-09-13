"""
tests/_test_support/m65d_uncertain_world.py — V69 M65D: an external system that
applies the effect and then loses the response, and the worker that meets it.

WHY A REAL SOCKET
=================
M65D's claim is about a window that only exists because the network is not the
same thing as the remote system:

        the effect lands  ->  the response is lost  ->  the handler raises

A mock that raises cannot demonstrate that, because a mock has no external state
to leave behind. The server here does: it applies a DURABLE effect the test can
count, and only then closes the connection without answering. The client sees a
transport failure, and the effect is real.

Everything is confined to 127.0.0.1 and a temporary directory. Nothing here
reaches a network, a real tool or any process it did not create — the socket is
bound to the loopback interface on an ephemeral port and is closed by the test
that opened it.

The world model (attempt log vs. distinct effect files) is M65C's, reused
deliberately: counting INVOCATIONS proves nothing about duplication, because an
idempotent external system legitimately sees two. Only the effect files count.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time

from _test_support.m65c_effect_world import (  # noqa: F401 — re-exported
    CRASH_EXIT, FileBarrier, SyntheticWorld, apply_effect, make_reconciler,
)


class LostResponseServer:
    """A localhost server that applies the effect and then drops the response.

    ``behaviour`` selects the window under test:

    * ``lose_response``  — apply, then close the socket with no reply (F4)
    * ``apply_and_reply``— apply and answer normally
    * ``hang``           — accept, apply, and never answer (F6, client times out)
    * ``refuse``         — never started, so the client cannot connect (F3)

    The effect is applied BEFORE the failure in every case but ``refuse``, which
    is the point: the external truth and what the client can observe disagree.
    """

    def __init__(self, world_root, *, mode="non_replayable",
                 behaviour="lose_response") -> None:
        self.world_root = str(world_root)
        self.mode = mode
        self.behaviour = behaviour
        self.applied: list = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        # A bounded accept, so `close()` is prompt. Closing a listening socket
        # from another thread does NOT reliably interrupt a blocking accept on
        # Linux, and every teardown then paid the full join timeout — measured
        # at five seconds per test.
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # ── lifecycle ───────────────────────────────────────────────────────────
    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5.0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ── the server ──────────────────────────────────────────────────────────
    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                # The bounded accept above. Loop and re-check `_stop`; note
                # this MUST precede the OSError arm, since socket.timeout is
                # an OSError and would otherwise end the server.
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5.0)
            raw = self._read_request(conn)
            body = raw.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in raw else b"{}"
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            key = str(payload.get("idempotency_key", "none"))

            # ── THE EXTERNAL EFFECT. Durable, applied, irreversible. ────────
            result = apply_effect(self.world_root, mode=self.mode,
                                  idempotency_key=key)
            self.applied.append(key)

            if self.behaviour == "hang":
                # Accepted, applied, and never answered. The client's timeout
                # says nothing whatever about this.
                while not self._stop.is_set():
                    time.sleep(0.02)
                return
            if self.behaviour == "lose_response":
                # ...and now the response is gone. One dropped segment.
                conn.close()
                return
            self._reply(conn, result)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _read_request(conn: socket.socket) -> bytes:
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        # Read a declared body, so the request is complete before it is applied.
        head, _, rest = buf.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                try:
                    length = int(line.split(b":", 1)[1].strip())
                except ValueError:
                    length = 0
        while len(rest) < length:
            chunk = conn.recv(4096)
            if not chunk:
                break
            rest += chunk
        return head + b"\r\n\r\n" + rest

    @staticmethod
    def _reply(conn: socket.socket, result: dict) -> None:
        body = json.dumps(result).encode("utf-8")
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                     b"Content-Length: " + str(len(body)).encode() +
                     b"\r\nConnection: close\r\n\r\n" + body)


def call_remote(port: int, idempotency_key: str, *, timeout: float = 3.0) -> dict:
    """POST one effect request. Raises on any transport failure, like a client.

    Deliberately NOT catching anything: the tool adapter's job in this fixture
    is to talk to the server, and turning a transport failure into a tidy
    return value here would hide the exact ambiguity the tests exist for.
    """
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/effect",
        data=json.dumps({"idempotency_key": idempotency_key}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8") or "{}")


def _die(point: str) -> None:
    """Hard process death. Skips every finally, atexit hook and buffer flush."""
    sys.stderr.write(f"m65d-worker: deliberate hard exit at {point}\n")
    sys.stderr.flush()
    os._exit(CRASH_EXIT)


def _write_result(path: str, outcome: dict) -> None:
    if not path:
        return
    tmp = f"{path}.partial"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(outcome, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def run_uncertain_effect(spec: dict, result_path: str = "") -> None:
    """One call through the REAL ToolExecutor protocol, in a fresh interpreter.

    This is the difference from M65C's worker, which drove the journal
    directly. M65D's rule lives partly in the executor — which disposition is
    published, which evidence is honoured — so a worker that skipped it would
    prove the store correct and say nothing about the runtime.
    """
    import asyncio

    from core.effect_journal import (
        DurableEffectJournal, EffectDurabilityClass, EffectOutcomeEvidence,
        ExternalOutcome, DeclaredEffectOutcome, compute_effect_id,
        derive_idempotency_key, register_durability, register_reconciler,
    )
    from tools.executor import ToolExecutor
    import tools.executor as executor_module

    crash_at = spec.get("crash_at", "")
    outcome: dict = {"pid": os.getpid()}

    async def _no_broadcast(_payload):
        return None
    executor_module._aura_broadcast = _no_broadcast

    journal = DurableEffectJournal(
        spec["journal"],
        lease_s=spec.get("lease_s", 900.0),
        lease_grace_s=spec.get("lease_grace_s", 60.0))
    outcome["instance_id"] = journal.instance_id

    cls = EffectDurabilityClass(spec["durability_class"])
    tool = spec["tool"]
    args = json.loads(spec["args"])
    register_durability(tool, cls)
    if spec.get("reconciler"):
        register_reconciler(tool, make_reconciler(spec["world"]))

    executor = ToolExecutor(journal=journal)
    executor.begin_effect_epoch(spec["scope"])

    async def _granted(tool_name, preview):
        return True, "test:granted"
    executor._challenge = _granted

    effect_id = compute_effect_id(surface="native", tool_id=tool,
                                  identity_scope=spec["scope"], tool_input=args)
    idem = derive_idempotency_key(effect_id)
    outcome["effect_id"] = effect_id
    outcome["idempotency_key"] = idem

    port = int(spec.get("port", 0))
    handler_mode = spec.get("handler", "remote")

    def _handler(**kwargs):
        if handler_mode == "raise_before_call":
            # F2: EXECUTING is durably committed and nothing external happened.
            # JARVIS still cannot know that, which is the point.
            raise RuntimeError("failed before contacting the remote system")
        result = call_remote(port, idem, timeout=float(spec.get("timeout", 3.0)))
        if handler_mode == "postprocess_raises":
            raise ValueError("could not parse the response")     # F5
        if handler_mode == "postprocess_raises_with_ack":
            raise DeclaredEffectOutcome(
                EffectOutcomeEvidence(ExternalOutcome.PROVEN_COMMITTED,
                                      "acked_before_parse"),
                "the server acknowledged before the parse failed")
        return result

    setattr(executor, f"_tool_{tool}", _handler)

    if crash_at == "F11":
        # Evidence obtained, process dies before the durable transition.
        original = journal.fail_observed

        def _die_first(*a, **kw):
            _die("F11 before the durable no-effect transition")
            return original(*a, **kw)                     # pragma: no cover
        journal.fail_observed = _die_first
    if crash_at == "F12":
        original_commit = journal.commit

        def _die_commit(*a, **kw):
            _die("F12 before the durable committed transition")
            return original_commit(*a, **kw)              # pragma: no cover
        journal.commit = _die_commit

    note: dict = {}

    async def _run():
        return await executor.aexecute(tool, dict(args), "worker",
                                       effect_note=note)

    if crash_at == "F7":
        # SIGKILL-equivalent AFTER the boundary is handled by the parent, which
        # kills this process while it is parked below.
        pass

    try:
        result = asyncio.run(_run())
    except BaseException as exc:                       # noqa: BLE001
        outcome["raised"] = type(exc).__name__
        result = None
    outcome["note"] = {k: v for k, v in note.items()
                       if isinstance(v, (str, int, bool, type(None)))}
    outcome["result_error_class"] = (result or {}).get("error_class") \
        if isinstance(result, dict) else None
    record = journal.get(effect_id)
    outcome["state"] = record.state.value if record else None
    outcome["external_outcome"] = record.external_effect.value if record else None
    _write_result(result_path, outcome)


if __name__ == "__main__":
    _spec = json.loads(sys.argv[1])
    run_uncertain_effect(_spec, sys.argv[2] if len(sys.argv) > 2 else "")
