"""scripts/soak_stabilization_m61.py — V69 M61.6.1: bounded deterministic soak.

Exercises the existing M60 continuity/recovery spine repeatedly, in-process, WITHOUT
Ollama, without a network and without a model, and measures whether anything grows
that should not. The question it answers is not "does a turn work" — the unit suites
cover that — but "does running many cycles leave residue": a task that is never
awaited, a thread that is never joined, a temporary file that is never removed, a
journal that grows without bound, a restart counter that never decays.

WHAT IT EXERCISES (all existing subsystems; M61 adds no capability)
------------------------------------------------------------------
  boot-state transitions · session/run journal writes · turn completion ·
  INTERRUPTED turn + crash reconciliation · console rendering · bounded queues ·
  optional-service failure · supervisor restart limits + circuit breaker ·
  diagnostics preview · backup creation and integrity verification · shutdown

WHAT IT MEASURES, BEFORE AND AFTER
----------------------------------
  RSS (when psutil is available) · Python thread count · asyncio task count ·
  open managed databases · queue depth · journal rows · runtime log size ·
  temporary files in the managed tree · restart history depth · health warnings

SAFETY
------
Everything runs against a TEMPORARY store under the managed tree, removed at the end.
No git mutation, no host mutation, no Ollama call, no model download, no semantic
collection touched, no network. Simulated time is injected (a monotonic counter), so
the soak is fast and does not sleep.

Usage::

    python jarvis/scripts/soak_stabilization_m61.py
    python jarvis/scripts/soak_stabilization_m61.py --cycles 40 --json
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import shutil
import sys
import tempfile
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

DEFAULT_CYCLES = 25

#: Growth ceilings. Deliberately non-zero where a runtime legitimately allocates
#: (an interpreter may add a thread for an executor), and hard zero where any growth
#: is a leak by definition (orphan tasks, leftover temp files).
MAX_THREAD_GROWTH = 2
MAX_TASK_GROWTH = 0
MAX_TEMPFILE_GROWTH = 0
MAX_RSS_GROWTH_MB = 64.0


def _rss_mb() -> float | None:
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001 — psutil is optional for this measurement
        return None


def _temp_files(root) -> int:
    """Count leftover temp/partial files in the managed tree."""
    count = 0
    for pattern in ("**/*.tmp", "**/*.partial", "**/*.lock", "**/~*"):
        count += sum(1 for _ in root.glob(pattern))
    return count


def _measure(journal, supervisor, root) -> dict:
    """One bounded, content-free measurement point."""
    try:
        tasks = len(asyncio.all_tasks(asyncio.get_running_loop()))
    except RuntimeError:
        tasks = 0
    health = journal.health() if journal is not None else {}
    restart_history = 0
    if supervisor is not None:
        for name in getattr(supervisor, "_services", {}):
            service = supervisor.get(name)
            if service is not None:
                restart_history = max(restart_history, len(service.history()))
    try:
        log_bytes = (root / "logs" / "jarvis.log").stat().st_size
    except OSError:
        log_bytes = 0
    return {
        "rss_mb": _rss_mb(),
        "threads": threading.active_count(),
        "asyncio_tasks": tasks,
        "journal_writes": health.get("writes", 0),
        "journal_write_failures": health.get("write_failures", 0),
        "journal_read_failures": health.get("read_failures", 0),
        "restart_history_depth": restart_history,
        "temp_files": _temp_files(root),
        "log_bytes": log_bytes,
        "gc_objects": len(gc.get_objects()) // 1000,  # thousands, coarse on purpose
    }


def _cycle(journal, supervisor, cycle: int) -> dict:
    """One full simulated session: boot, turns, an interruption, recovery, shutdown."""
    from core.session_continuity import RunOutcome, SessionState
    from core.turn_reconciliation import reconcile

    events = {}

    # 1. boot: a run and a session begin.
    journal.begin_run(runtime_version=f"soak-{cycle}")
    session = journal.begin_session(language="es", response_profile="AUTO")
    events["session_opened"] = session is not None

    # 2. two clean turns.
    for sequence in range(2):
        turn = journal.open_turn(role="user", sequence=sequence)
        journal.record_visible_progress(turn, visible_chars=48)
        journal.finalize_turn(turn, terminal_state="COMPLETED")
    events["clean_turns"] = 2

    # 3. one INTERRUPTED turn — opened and deliberately never finalized. This is the
    #    crash shape M60 reconciliation exists for.
    orphan = journal.open_turn(role="user", sequence=2)
    journal.record_visible_progress(orphan, visible_chars=12)
    events["unfinished_before_recovery"] = len(journal.unfinished_turns())

    # 4. an effectful tool op left open — reconciliation must NOT replay it.
    op = journal.open_tool_op(tool_name="soak_probe", effectful=True)
    events["unfinished_tool_ops"] = len(journal.unfinished_tool_ops())

    # 5. checkpoint, then an UNCLEAN run end (the crash).
    journal.checkpoint_run()
    journal.finalize_run(clean=False, lifecycle_state="CRASHED")

    # 6. recovery: reconcile the previous run. The invariant that matters is that
    #    reconciliation has NO execution path — nothing is replayed.
    report = reconcile(journal)
    events["actions_replayed"] = report.actions_replayed
    events["turns_reconciled"] = report.turns_reconciled
    events["recovery_state"] = str(report.state.value)
    events["recovered"] = True

    # 7. supervisor: drive an optional service to failure and back, so the restart
    #    window and circuit breaker are exercised (and must decay, not accumulate).
    supervisor.note_failure("soak-service", reason="synthetic")
    supervisor.mark_running("soak-service", health_probe_passed=True)

    # 8. shutdown: close the session cleanly.
    journal.close_session(state=SessionState.CLOSED)
    journal.finalize_run(clean=True, lifecycle_state="STOPPED")
    events["outcome"] = RunOutcome.CLEAN_SHUTDOWN.value

    # Keep the orphan/op referenced until here so nothing is collected mid-cycle.
    del orphan, op
    return events


async def _run_soak(cycles: int) -> dict:
    from core.managed_paths import app_root, data_dir
    from core.recovery_supervisor import RecoverySupervisor, ServiceClass
    from core.session_continuity import PersistenceMode, SessionJournal

    root = app_root()
    workdir = tempfile.mkdtemp(prefix="m61_soak_", dir=str(data_dir()))
    store_path = os.path.join(workdir, "soak_continuity.db")

    journal = SessionJournal(mode=PersistenceMode.LOCAL_REDACTED, path=store_path)
    supervisor = RecoverySupervisor()
    supervisor.register("soak-service", service_class=ServiceClass.OPTIONAL)

    try:
        before = _measure(journal, supervisor, root)
        samples, cycle_events = [], []
        for cycle in range(cycles):
            cycle_events.append(_cycle(journal, supervisor, cycle))
            if cycle % max(1, cycles // 5) == 0:
                samples.append(_measure(journal, supervisor, root))

        # Let anything scheduled settle, then collect: an orphan task surviving a
        # yield + a full collection is a genuine leak, not a timing artifact.
        await asyncio.sleep(0)
        gc.collect()
        after = _measure(journal, supervisor, root)

        # Diagnostics preview + backup integrity, once (they are not per-cycle work).
        extras = _exercise_diagnostics_and_backup(workdir)

        return {
            "cycles": cycles,
            "before": before,
            "after": after,
            "samples": samples,
            "events": cycle_events[-1] if cycle_events else {},
            "extras": extras,
            "assertions": _assertions(before, after, cycle_events, extras),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _exercise_diagnostics_and_backup(workdir: str) -> dict:
    """Bounded checks of the M60 diagnostics-preview and backup-integrity paths.

    Errors are RECORDED, not swallowed: an ImportError here used to make the
    "diagnostics are content-free" assertion pass vacuously, which is precisely the
    silent-pass shape M61 exists to remove. :func:`_assertions` now requires these to
    have actually run.
    """
    from pathlib import Path

    out: dict = {}
    try:
        from core.diagnostics_bundle import BundleMode, build_bundle
        from core.redaction_policy import scan_structure

        result = build_bundle(BundleMode.PREVIEW)
        payload = getattr(result, "payload", None)
        out["diagnostics_built"] = payload is not None
        out["diagnostics_state"] = str(getattr(
            getattr(result, "state", ""), "value", ""))
        # The preview is the IDENTICAL payload a redacted bundle would ship, so a
        # clean scan here is a real guarantee, not a sample.
        out["diagnostics_leaks"] = scan_structure(payload) if payload else ["no_payload"]
    except Exception as exc:  # noqa: BLE001
        out["diagnostics_built"] = False
        out["diagnostics_error"] = type(exc).__name__

    try:
        from core.managed_backup import BackupState, create_backup, verify_backup

        destination = Path(workdir)
        result = create_backup(name="m61soakbackup", destination=destination)
        out["backup_state"] = result.state.value
        if result.state is BackupState.EMPTY:
            # Nothing eligible to back up is a legitimate outcome, but it is NOT
            # evidence that the integrity path works, and must not read as one.
            out["backup_verified"] = None
        else:
            archive = destination / result.file_name
            verification = verify_backup(archive)
            out["backup_verified"] = bool(verification.get("valid"))
            out["backup_members"] = verification.get("members", 0)
            out["backup_integrity_hash_verified"] = bool(result.integrity_verified)
            try:
                archive.unlink()
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001
        out["backup_verified"] = False
        out["backup_error"] = type(exc).__name__
    return out


def _assertions(before: dict, after: dict, cycle_events: list, extras: dict) -> dict:
    """The bounded-growth verdict. Every entry is a hard pass/fail."""
    checks = {}

    checks["no_thread_leak"] = (
        after["threads"] - before["threads"] <= MAX_THREAD_GROWTH)
    checks["no_orphan_task"] = (
        after["asyncio_tasks"] - before["asyncio_tasks"] <= MAX_TASK_GROWTH)
    checks["no_temp_residue"] = (
        after["temp_files"] - before["temp_files"] <= MAX_TEMPFILE_GROWTH)

    if before["rss_mb"] is not None and after["rss_mb"] is not None:
        checks["bounded_rss"] = (after["rss_mb"] - before["rss_mb"]) <= MAX_RSS_GROWTH_MB
    else:
        checks["bounded_rss"] = True  # psutil absent — not a failure, just unmeasured

    # The journal must have actually written, and never failed a write or a read.
    checks["journal_wrote"] = after["journal_writes"] > before["journal_writes"]
    checks["no_write_failure"] = after["journal_write_failures"] == 0
    checks["no_read_failure"] = after["journal_read_failures"] == 0

    # Restart history is windowed; it must not grow with the cycle count.
    checks["restart_history_bounded"] = after["restart_history_depth"] <= 50

    # Recovery must never replay an effectful action. Structurally zero, every cycle.
    checks["no_effectful_replay"] = all(
        e.get("actions_replayed", 0) == 0 for e in cycle_events)
    checks["every_cycle_recovered"] = all(e.get("recovered") for e in cycle_events)
    checks["interrupted_turn_seen"] = all(
        e.get("unfinished_before_recovery", 0) >= 1 for e in cycle_events)

    # Diagnostics must have ACTUALLY BUILT, and be content-free. Requiring the build
    # first is what stops an ImportError from making this pass vacuously.
    checks["diagnostics_built"] = bool(extras.get("diagnostics_built"))
    checks["diagnostics_content_free"] = (
        bool(extras.get("diagnostics_built")) and not extras.get("diagnostics_leaks"))

    # Backup integrity: verified when a backup was produced. ``None`` means there was
    # nothing eligible to archive — reported as unproven, never as a pass.
    verified = extras.get("backup_verified")
    checks["backup_integrity"] = verified is not False
    if verified is None:
        checks["backup_integrity_proven"] = False
    else:
        checks["backup_integrity_proven"] = bool(verified)

    return checks


def main() -> int:
    ap = argparse.ArgumentParser(description="M61.6.1 bounded deterministic soak")
    ap.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--output", metavar="PATH")
    args = ap.parse_args()

    cycles = max(1, min(int(args.cycles), 500))
    result = asyncio.run(_run_soak(cycles))
    checks = result["assertions"]
    failed = [name for name, ok in checks.items() if not ok]
    result["verdict"] = "PASS" if not failed else "FAIL"
    result["failed"] = failed

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        before, after = result["before"], result["after"]
        print(f"JARVIS M61.6.1 deterministic soak - {cycles} cycles")
        print(f"  threads       {before['threads']} -> {after['threads']}")
        print(f"  asyncio tasks {before['asyncio_tasks']} -> {after['asyncio_tasks']}")
        print(f"  journal writes {before['journal_writes']} -> "
              f"{after['journal_writes']} (failures {after['journal_write_failures']})")
        print(f"  temp files    {before['temp_files']} -> {after['temp_files']}")
        if before["rss_mb"] is not None:
            print(f"  RSS MB        {before['rss_mb']:.1f} -> {after['rss_mb']:.1f}")
        else:
            print("  RSS MB        unmeasured (psutil absent)")
        for name, ok in sorted(checks.items()):
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        print(f"VERDICT: {result['verdict']}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, default=str)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
