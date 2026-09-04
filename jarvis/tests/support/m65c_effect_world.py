"""
tests/support/m65c_effect_world.py — V69 M65C: a TEST-OWNED external world and
the worker that acts on it.

WHY THIS EXISTS AS A SEPARATE MODULE, RUN AS A SUBPROCESS
=========================================================
The crash tests need real processes. A thread cannot be SIGKILLed, an
``os._exit`` inside pytest takes the whole run with it, and normal exception
cleanup is emphatically NOT equivalent to process death — a ``finally`` block
that releases a reservation is exactly what a crash does not run.

Workers are launched with ``subprocess`` against THIS FILE as a script, not with
``multiprocessing``. Measured: ``multiprocessing``'s ``spawn`` start method
re-executes the parent's ``__main__`` in the child, and under ``python -m
pytest`` that ``__main__`` is pytest itself — so every worker started a nested
pytest session and the suite hung with no output. A plain subprocess has no such
coupling, and it gives exactly what these tests need: a genuinely fresh
interpreter, and therefore a genuinely fresh ``runtime_instance_id``, which is
what a restart really is.

Ordering across processes is forced by :class:`FileBarrier` below rather than by
sleeping.

THE EXTERNAL WORLD
==================
Every effect here is synthetic and confined to a temporary directory. Nothing in
this module touches a network, a real tool, a real host or any process it did
not create.

The world is a directory with two observable facts:

    world/attempts.log     one line per INVOCATION the worker made
    world/effects/<name>   one file per DISTINCT external effect

The split is the entire point of the milestone's claim matrix. An idempotent
external system deduplicates, so two attempts leave two lines and ONE effect
file; a non-replayable one leaves two lines and TWO effect files. Counting
invocations would therefore prove nothing about duplication — only the effect
files do, which is why the tests assert on them.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path


class SyntheticWorld:
    """The observable external system. Created by the test, read by the test,
    written only by workers."""

    def __init__(self, root) -> None:
        self.root = Path(root)
        self.effects_dir = self.root / "effects"
        self.reconcile_dir = self.root / "reconcile"
        self.attempts = self.root / "attempts.log"

    def prepare(self) -> "SyntheticWorld":
        self.effects_dir.mkdir(parents=True, exist_ok=True)
        self.reconcile_dir.mkdir(parents=True, exist_ok=True)
        self.attempts.touch()
        return self

    # ── what the test measures ──────────────────────────────────────────────
    @property
    def effect_count(self) -> int:
        """DISTINCT external effects. The number that must not exceed one."""
        return len(list(self.effects_dir.iterdir()))

    @property
    def attempt_count(self) -> int:
        """Invocations. May legitimately exceed ``effect_count`` when the
        external system deduplicates."""
        return len([ln for ln in self.attempts.read_text().splitlines() if ln])


def apply_effect(world_root: str, *, mode: str, idempotency_key: str,
                 payload: str = "x") -> dict:
    """The synthetic tool. Performs ONE external effect, per *mode*.

    ``mode`` mirrors the durability class the tool would declare:

    * ``non_replayable`` — every invocation creates a NEW effect. Two attempts
      really are two effects, which is why an ambiguous crash cannot be retried.
    * ``idempotent_key`` — the effect file is named by the IDEMPOTENCY KEY and
      created ``O_EXCL``, so the external system itself refuses the second one.
      This is what "the external system dedupes" means concretely.
    * ``idempotent`` — a single fixed name rewritten with the same content, so
      repeats converge on one state.
    * ``reconcilable`` — a new effect each time, plus a key marker the
      reconciler can later query.
    """
    world = SyntheticWorld(world_root)
    with open(world.attempts, "a", encoding="utf-8") as fh:
        fh.write(f"{mode}:{idempotency_key}\n")
        fh.flush()
        os.fsync(fh.fileno())

    if mode == "idempotent_key":
        target = world.effects_dir / f"key-{idempotency_key}"
        try:
            fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # The external system deduplicated. One effect, two attempts.
            return {"status": "deduplicated_by_external_system",
                    "key": idempotency_key}
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        return {"status": "applied", "key": idempotency_key}

    if mode == "idempotent":
        (world.effects_dir / "converged").write_text(payload, encoding="utf-8")
        return {"status": "applied", "key": idempotency_key}

    name = f"eff-{uuid.uuid4().hex}"
    (world.effects_dir / name).write_text(payload, encoding="utf-8")
    if mode == "reconcilable":
        (world.reconcile_dir / idempotency_key).write_text(name, encoding="utf-8")
    return {"status": "applied", "effect": name}


def make_reconciler(world_root: str):
    """A bounded 'did this effect happen?' probe over the synthetic world.

    Returns CONFIRMED_COMMITTED when the key marker exists, CONFIRMED_NOT_EXECUTED
    when it provably does not, and UNKNOWN when the world itself is unreadable —
    the third answer is the important one and is never rounded away.
    """
    from core.effect_journal import ReconciliationVerdict

    def _probe(effect_id: str, idempotency_key: str):
        world = SyntheticWorld(world_root)
        try:
            if not world.reconcile_dir.exists():
                return ReconciliationVerdict.UNKNOWN
            if (world.reconcile_dir / idempotency_key).exists():
                return ReconciliationVerdict.CONFIRMED_COMMITTED
            return ReconciliationVerdict.CONFIRMED_NOT_EXECUTED
        except OSError:
            return ReconciliationVerdict.UNKNOWN

    return _probe


#: Exit code a worker uses for a DELIBERATE hard death, so a test can tell an
#: injected crash apart from a worker that broke by accident.
CRASH_EXIT = 70


def _die(point: str) -> None:
    """Hard process death at *point*. NOT an exception.

    ``os._exit`` skips every ``finally``, every ``atexit`` hook and every buffer
    flush, which is the whole point: a reservation released by a ``finally``
    block would prove nothing about a machine losing power. The only state that
    survives is what SQLite has already fsynced.
    """
    sys.stderr.write(f"m65c-worker: deliberate hard exit at {point}\n")
    sys.stderr.flush()
    os._exit(CRASH_EXIT)


class FileBarrier:
    """An N-way rendezvous across UNRELATED processes, on the filesystem.

    Every participant announces itself and then waits until all *count* have
    announced. That is a barrier, not a sleep: no participant proceeds until
    every other one has arrived, so a race the test needs is forced to happen
    rather than hoped for.

    The wait polls, because unrelated processes share no synchronisation
    primitive — but it is bounded, so a participant that never arrives makes the
    test FAIL rather than hang.
    """

    def __init__(self, directory, count: int, timeout_s: float = 25.0) -> None:
        self.dir = Path(directory)
        self.count = int(count)
        self.timeout_s = float(timeout_s)

    def wait(self, tag: str) -> None:
        import time as _t

        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{tag}.ready").write_text("1", encoding="utf-8")
        deadline = _t.monotonic() + self.timeout_s
        while _t.monotonic() < deadline:
            if len(list(self.dir.glob("*.ready"))) >= self.count:
                return
            _t.sleep(0.01)
        raise TimeoutError(
            f"barrier {self.dir} never reached {self.count} participants")


def run_effect(spec: dict, result_path: str = "") -> None:
    """Perform one synthetic effect through the durable protocol.

    Runs in a SPAWNED process, so this interpreter has its own
    ``runtime_instance_id`` — which is exactly what makes it a different owner
    from the parent, and what makes a restart a restart.

    ``spec["crash_at"]`` injects a deterministic hard death at one of the
    chaos points C0-C5 (§32).
    """
    from core.effect_journal import (
        DurableEffectJournal,
        EffectDurabilityClass,
        compute_effect_id,
        derive_idempotency_key,
    )

    crash_at = spec.get("crash_at", "")
    outcome: dict = {"pid": os.getpid()}

    if crash_at == "C0":
        _die("C0 before reserve")

    journal = DurableEffectJournal(
        spec["journal"],
        lease_s=spec.get("lease_s", 900.0),
        lease_grace_s=spec.get("lease_grace_s", 60.0),
        busy_timeout_ms=spec.get("busy_timeout_ms", 5000))
    outcome["instance_id"] = journal.instance_id

    cls = EffectDurabilityClass(spec["durability_class"])
    args = json.loads(spec["args"])
    effect_id = compute_effect_id(surface="native", tool_id=spec["tool"],
                                  identity_scope=spec["scope"], tool_input=args)
    outcome["effect_id"] = effect_id

    barrier = None
    if spec.get("barrier_dir"):
        barrier = FileBarrier(spec["barrier_dir"], spec.get("barrier_count", 2),
                              spec.get("barrier_timeout_s", 25.0))
    tag = spec.get("worker_tag") or str(os.getpid())

    if barrier is not None and spec.get("barrier_at", "reserve") == "reserve":
        # Every racer arrives at the reservation together. A barrier, not a
        # sleep: the race must be forced, not hoped for.
        barrier.wait(tag)

    reservation = journal.reserve(
        effect_id=effect_id, tool_id=spec["tool"], surface="native",
        durability_class=cls, tool_input=args)
    outcome["reservation"] = reservation.outcome.value
    outcome["owned"] = reservation.owned

    if not reservation.owned:
        _write_result(result_path, outcome)
        return

    if crash_at == "C1":
        _die("C1 after durable reserve")
    if barrier is not None and spec.get("barrier_at") == "after_reserve":
        barrier.wait(tag)
    if spec.get("park_dir"):
        # HOLD the reservation until the parent says otherwise (or never, if the
        # parent means to kill this worker here). A barrier would not do: it
        # releases as soon as everyone arrives, and these tests need the worker
        # still holding its row while the parent looks at the journal or sends a
        # signal. Bounded, so a parent that forgets to release makes the test
        # fail rather than hang forever.
        park = Path(spec["park_dir"])
        park.mkdir(parents=True, exist_ok=True)
        (park / f"{tag}.parked").write_text("1", encoding="utf-8")
        release = park / "release"
        deadline = time.monotonic() + float(spec.get("park_timeout_s", 25.0))
        while not release.exists():
            if time.monotonic() > deadline:
                raise TimeoutError(f"worker {tag} was never released from {park}")
            time.sleep(0.01)
    if crash_at == "C2":
        _die("C2 immediately before the tool")

    journal.mark_executing(effect_id)
    result = apply_effect(spec["world"], mode=spec["mode"],
                          idempotency_key=derive_idempotency_key(effect_id))
    outcome["effect_result"] = result

    if crash_at == "C3":
        # THE window. The external effect has happened and no COMMITTED row
        # exists. Nothing local can distinguish this from C2 afterwards.
        _die("C3 effect applied, before the durable commit")

    journal.commit(effect_id, receipt=result)
    outcome["committed"] = True

    if crash_at == "C4":
        _die("C4 after the durable commit, before the caller is told")

    _write_result(result_path, outcome)

    if crash_at == "C5":
        _die("C5 after the result was delivered")


def _write_result(path: str, outcome: dict) -> None:
    if not path:
        return
    tmp = f"{path}.partial"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(outcome, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


if __name__ == "__main__":
    # argv: <spec-json> [result-path]. Kept trivial on purpose — a worker with
    # its own argument parsing is a worker that can fail for reasons unrelated
    # to the invariant under test.
    _spec = json.loads(sys.argv[1])
    run_effect(_spec, sys.argv[2] if len(sys.argv) > 2 else "")
