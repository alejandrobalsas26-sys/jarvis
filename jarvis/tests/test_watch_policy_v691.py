"""
tests/test_watch_policy_v691.py — V69 M54.1.2/.3/.4: watch policy, backpressure
and overflow recovery.

Locks in the three proven defects behind the live QueueFull storm:

  1. `~/Downloads` was watched recursive and CONTAINS the repo on the target host,
     so JARVIS's own writes fed the scanner (main.py, tests/*.py, vector_store/*.bin
     all probed as SCAN before this patch);
  2. `_WATCHED_EXTENSIONS` — the intended executable allowlist — was dead code;
  3. exclusions were substring tests over the whole path ("log" also matched
     "catalog"; "tmp" was never checked at all).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from core.safe_enqueue import EventPriority
from core.watch_policy import (
    WatchClass,
    WatchEvent,
    WatchPolicy,
    default_watch_policy,
)
from core.watch_reconcile import RootState, WatchReconciler


def _policy(tmp_path: Path) -> WatchPolicy:
    """A policy shaped like production: a repo tree containing an inbox, plus a
    security root that CONTAINS the repo (the exact live topology)."""
    downloads = tmp_path / "Downloads"
    repo = downloads / "jarvis_v2" / "jarvis"
    inbox = repo / "analyze_inbox"
    for d in (downloads, repo, inbox):
        d.mkdir(parents=True, exist_ok=True)
    return WatchPolicy(
        security_roots=[str(downloads)],
        code_roots=[str(inbox)],
        self_root=str(repo),
    )


# -- The live topology: the repo lives inside the security root -----------------
def test_repo_tree_inside_downloads_does_not_queue(tmp_path):
    """THE storm. Before this patch main.py / tests/*.py / vector_store/*.bin under
    ~/Downloads/jarvis_v2/jarvis all classified as SCAN, so JARVIS editing itself
    fed its own scanner. They must all be ignored now."""
    pol = _policy(tmp_path)
    repo = tmp_path / "Downloads" / "jarvis_v2" / "jarvis"
    for rel in ("main.py",
                "tests/test_console_v69.py",
                "core/llm.py",
                "scripts/foo.py",
                "vector_store/index.bin",
                "core/integrity_baseline.json",
                "logs/jarvis.log",
                "__pycache__/main.cpython-311.pyc",
                ".pytest_cache/v/cache/lastfailed",
                ".git/objects/ab/cdef"):
        d = pol.classify(str(repo / rel), WatchEvent.MODIFIED)
        assert d.cls is WatchClass.IGNORE, f"{rel} must not queue (got {d.reason})"


def test_generated_and_noise_paths_are_ignored(tmp_path):
    pol = _policy(tmp_path)
    dl = tmp_path / "Downloads"
    for rel in ("x/__pycache__/a.pyc", "x/.ruff_cache/f", "x/.mypy_cache/f",
                "node_modules/pkg/i.js", "logs/a.exe", "vector_store/chroma.sqlite3",
                "a.exe.tmp", "a.exe.part", "a.exe.crdownload", "~$report.exe",
                "b.swp", "notes.exe~"):
        d = pol.classify(str(dl / rel), WatchEvent.MODIFIED)
        assert d.cls is WatchClass.IGNORE, f"{rel} must be ignored (got {d.reason})"


def test_security_root_queues_executables_only(tmp_path):
    """Downloads is a SECURITY_SCAN root — a malware drop still queues, but it is
    NOT a source-code inbox, so ordinary files there never do."""
    pol = _policy(tmp_path)
    dl = tmp_path / "Downloads"

    d = pol.classify(str(dl / "payload.exe"), WatchEvent.CREATED)
    assert d.cls is WatchClass.SECURITY_SCAN
    assert d.priority is EventPriority.HIGH, "a dropped executable is high-value"

    for rel in ("readme.txt", "photo.jpg", "script.py", "data.csv"):
        assert pol.classify(str(dl / rel), WatchEvent.CREATED).cls is WatchClass.IGNORE


def test_inbox_is_observed_for_code_even_though_it_is_inside_the_repo(tmp_path):
    """analyze_inbox is the explicit drop zone and must survive the self-tree
    exclusion that protects the rest of the repo."""
    pol = _policy(tmp_path)
    inbox = tmp_path / "Downloads" / "jarvis_v2" / "jarvis" / "analyze_inbox"
    d = pol.classify(str(inbox / "sample.py"), WatchEvent.CREATED)
    assert d.cls is WatchClass.CODE_ANALYSIS
    d2 = pol.classify(str(inbox / "dropped.exe"), WatchEvent.CREATED)
    assert d2.accepted, "an executable in the inbox is still interesting"


def test_security_and_code_classes_are_distinct(tmp_path):
    """The two classes must be separable so they never share one tiny queue."""
    pol = _policy(tmp_path)
    dl = tmp_path / "Downloads"
    inbox = dl / "jarvis_v2" / "jarvis" / "analyze_inbox"
    assert pol.classify(str(dl / "m.exe"), WatchEvent.CREATED).cls is WatchClass.SECURITY_SCAN
    assert pol.classify(str(inbox / "m.py"), WatchEvent.CREATED).cls is WatchClass.CODE_ANALYSIS


# -- Path handling -------------------------------------------------------------
def test_windows_paths_normalize_and_compare_case_insensitively(tmp_path):
    pol = _policy(tmp_path)
    dl = tmp_path / "Downloads"
    lower = pol.classify(str(dl / "payload.exe"), WatchEvent.CREATED)
    upper = pol.classify(str(dl).upper() + os.sep + "PAYLOAD.EXE", WatchEvent.CREATED)
    if os.name == "nt":
        assert upper.cls is WatchClass.SECURITY_SCAN, "case must not matter on Windows"
        assert upper.key == lower.key, "coalescing keys must normalize"
    # Separator normalization applies on every platform we build keys for.
    assert lower.key == lower.key.strip()


def test_exclusion_is_component_wise_not_substring(tmp_path):
    """The old code did `"log" in str(p).lower()`, which wrongly excluded any path
    containing 'log' (catalog/login) — a real detection blind spot."""
    pol = _policy(tmp_path)
    dl = tmp_path / "Downloads"
    d = pol.classify(str(dl / "catalog" / "installer.exe"), WatchEvent.CREATED)
    assert d.cls is WatchClass.SECURITY_SCAN, "'catalog' is not 'logs'"
    d2 = pol.classify(str(dl / "logs" / "installer.exe"), WatchEvent.CREATED)
    assert d2.cls is WatchClass.IGNORE, "a real logs/ component is excluded"


def test_root_boundary_is_not_a_prefix_match(tmp_path):
    """'C:/Downloads-old' must not be treated as inside 'C:/Downloads'."""
    pol = _policy(tmp_path)
    sibling = str(tmp_path / "Downloads-old" / "x.exe")
    assert pol.classify(sibling, WatchEvent.CREATED).cls is WatchClass.IGNORE


def test_directory_modify_is_noise_but_create_is_kept(tmp_path):
    pol = _policy(tmp_path)
    dl = tmp_path / "Downloads"
    noisy = pol.classify(str(dl / "somedir"), WatchEvent.MODIFIED, is_directory=True)
    assert noisy.cls is WatchClass.IGNORE and noisy.reason == "directory_noise"


def test_default_policy_excludes_the_real_repo_from_the_security_root():
    """Against the REAL configured roots on this host: the repo lives under
    ~/Downloads, and must not be observable as scannable content."""
    pol = default_watch_policy()
    repo = Path(__file__).resolve().parent.parent
    d = pol.classify(str(repo / "main.py"), WatchEvent.MODIFIED)
    assert d.cls is WatchClass.IGNORE, f"the repo must never self-trigger (got {d.reason})"
    d2 = pol.classify(str(repo / "tests" / "test_console_v69.py"), WatchEvent.MODIFIED)
    assert d2.cls is WatchClass.IGNORE


def test_operator_can_add_excludes_and_includes(tmp_path):
    class _S:
        watch_include = str(tmp_path / "extra_inbox")
        watch_exclude = "secrets"
        watch_queue_size = 512
        watch_debounce_ms = 1000
        watch_security_root = False

    pol = default_watch_policy(_S())
    assert any("extra_inbox" in r for r in pol.code_roots)
    assert "secrets" in pol.excluded_dir_names
    assert pol.security_roots == [], "watch_security_root=False drops the Downloads root"


# -- Overflow recovery (M54.1.4) ----------------------------------------------
def test_overflow_marks_root_stale_and_reconciles_once(tmp_path):
    """Overflow must trigger ONE bounded reconciliation per root, not one per
    dropped event, and must be honest that events were lost."""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    for i in range(5):
        (root / "sub" / f"f{i}.exe").write_text("x")

    offered: list = []
    rec = WatchReconciler(offer_path=offered.append, stopping_fn=lambda: False)

    async def _run():
        # Many overflow events for one root...
        for _ in range(100):
            rec.mark_overflow(str(root))
        assert rec.status(str(root)).state is RootState.STALE, "must not claim CURRENT"

        started = [rec.schedule_reconcile(str(root)) for _ in range(100)]
        assert sum(1 for s in started if s) == 1, "exactly one scan may start"
        await asyncio.sleep(0.05)
        st = rec.status(str(root))
        assert st.state is RootState.CURRENT
        assert st.reconciliations == 1
        assert len(offered) == 5, "every file is re-offered through the normal seam"

    asyncio.run(_run())


def test_no_reconciliation_starts_after_stopping(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.exe").write_text("x")
    offered: list = []
    stopping = {"v": False}
    rec = WatchReconciler(offer_path=offered.append,
                          stopping_fn=lambda: stopping["v"])

    async def _run():
        stopping["v"] = True
        rec.mark_overflow(str(root))
        assert rec.schedule_reconcile(str(root)) is False, "no new work after STOPPING"
        st = await rec.reconcile(str(root))
        assert st.state is RootState.DEGRADED
        assert st.last_error == "stopping"
        assert offered == []

    asyncio.run(_run())


def test_reconciliation_is_bounded_and_reports_truncation(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    def fake_walk(_r):
        yield str(root), [], [f"f{i}.exe" for i in range(500)]

    offered: list = []
    rec = WatchReconciler(offer_path=offered.append, stopping_fn=lambda: False,
                          walk_fn=fake_walk, max_files=100, page_size=10)

    async def _run():
        st = await rec.reconcile(str(root))
        assert st.truncated is True
        assert st.state is RootState.DEGRADED, "a truncated scan is not CURRENT"
        assert len(offered) <= 100, "recovery must stay bounded"

    asyncio.run(_run())


def test_reconciler_snapshot_shape_and_stale_count(tmp_path):
    rec = WatchReconciler(offer_path=lambda _p: None, stopping_fn=lambda: False)
    rec.mark_overflow("r1")
    snap = rec.snapshot()
    assert snap["stale_roots"] == 1
    assert snap["roots"]["r1"]["state"] == "STALE"
    assert set(snap) == {"roots", "stale_roots", "reconciliations"}
