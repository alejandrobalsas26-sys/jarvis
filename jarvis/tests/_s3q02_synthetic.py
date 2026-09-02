"""Synthetic scaffolding for the V69 M62 S3Q.0.2 post-live seal recovery.

WHAT THIS ADDS OVER S3Q.0.1
---------------------------
`.2`'s world is one completed synthetic evaluation in a git repository whose HEAD is the
evaluation source, because that is the only situation `.2` could describe. The whole
subject of `.3` is the situation `.2` could NOT describe: sealing failed, a repair had to
be committed, and HEAD moved off the commit that measured.

So this world builds the RECOVERY TOPOLOGY, not a snapshot:

    evaluation source commit
            |                  <- HEAD here while the witness is written
            v
    measurement witness commit     (first parent IS the evaluation source)
            |
            v
    repair implementation commit   <- HEAD here while the receipt is built

Three distinct commits, a clean worktree at each step, and a `.gitignore` that keeps the
runtime tree out of the repository exactly as the real one does. A receipt built here
therefore has an evaluation source and a seal implementation source that genuinely
DIFFER, which is the property no `.1` or `.2` fixture can produce.

Everything is synthetic. No eval-v4, no model, no generation, no plan consumption. The
adapter is four tiny LoRA tensors nothing deserialises.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("scripts.build_m62_eval_receipt")

import _s3q01_synthetic as W  # noqa: E402

#: Enough of the real layout for `WITNESS_SOURCE_SCOPE` to have tracked files under it,
#: so the witness's source digest is a digest of something rather than a refusal.
SOURCE_SCOPE_FILES = {
    "jarvis/training_gym/__init__.py": "# s3q02 synthetic evaluation machinery\n",
    "jarvis/training_gym/evaluation/__init__.py": "# s3q02 synthetic evaluation\n",
}

GITIGNORE = """\
# The runtime trees, gitignored exactly as the real repository ignores them.
data/
runs/
run/
evaluation-config.json
# Throwaway witness variants the non-vacuity tests write. Ignored so an untracked
# variant does not dirty a worktree the seal identity requires to be clean.
*.variant.json
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def recovery_world(tmp_path_factory, *, ledger_plan_hash: str = "") -> dict:
    """One completed synthetic evaluation, sealed across a real three-commit recovery.

    ``ledger_plan_hash`` models a WRAPPING protocol (D-S4F-2): under Protocol V4 the ledger
    records the OUTER plan the authority was bound to, while the report publishes the inner
    plan the outer one contains. Rewriting the ledger must happen HERE, before the witness
    is written, because the witness binds each ledger line by the digest of its canonical
    body -- a rewrite afterwards would leave a witness describing a different measurement.
    Left empty, the ledger names the report's plan and every v1-v3 caller is unchanged.
    """
    from scripts.build_m62_eval_receipt import (
        build_measurement_witness,
        witness_source_identity,
    )
    from scripts.verify_m62_control_plane import canonical_json

    world = dict(W.evaluated_world(tmp_path_factory))
    root = Path(world["repo_root"])

    if ledger_plan_hash:
        report = json.loads(
            (Path(world["directory"]) / "evaluation-report.json").read_text("utf-8"))
        ledger_path = Path(world["ledger"])
        lines = []
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if str(entry.get("plan_hash")) == str(report["plan_hash"]):
                entry["plan_hash"] = ledger_plan_hash
            lines.append(json.dumps(entry, sort_keys=True))
        ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # -- commit 1: the evaluation source ------------------------------------
    # `evaluated_world` already made a repository here and committed SOURCE.md. The
    # tracked evidence and the ignore rules join it, and THAT commit is the evaluation
    # source: the state the measurement above belongs to.
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    for rel, text in SOURCE_SCOPE_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "s3q02 synthetic evaluation source")
    evaluation_source_commit = _git(root, "rev-parse", "HEAD")
    assert not _git(root, "status", "--porcelain"), "the source commit must be clean"

    # -- the witness, written while HEAD is STILL the evaluation source ------
    source = witness_source_identity(root)
    assert source["evaluation_source_commit"] == evaluation_source_commit
    witness_payload = build_measurement_witness(
        world["directory"], ledger=world["ledger"],
        training_receipt=world["training_receipt"], evaluation_source=source,
        repo_root=root)
    witness_rel = "state/witnesses/0001-s3q02-synthetic-witness.json"
    witness_path = root / witness_rel
    witness_path.parent.mkdir(parents=True, exist_ok=True)
    witness_path.write_text(canonical_json(witness_payload), encoding="utf-8")

    # -- commit 2: the witness, ALONE, on top of the evaluation source -------
    _git(root, "add", "--", witness_rel)
    _git(root, "commit", "-q", "-m", "s3q02 synthetic measurement witness")
    witness_commit = _git(root, "rev-parse", "HEAD")
    parents = _git(root, "rev-list", "--parents", "-n", "1", witness_commit).split()
    assert parents[1] == evaluation_source_commit, "the witness parent IS the source"

    # -- commit 3: the repair. HEAD leaves the evaluation source -------------
    (root / "jarvis" / "training_gym" / "REPAIR.md").write_text(
        "s3q02 synthetic receipt-v3 implementation\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "s3q02 synthetic receipt-v3 implementation")
    seal_commit = _git(root, "rev-parse", "HEAD")
    assert seal_commit != evaluation_source_commit
    assert not _git(root, "status", "--porcelain"), "the seal commit must be clean"

    world.update({
        "evaluation_source_commit": evaluation_source_commit,
        "witness_commit": witness_commit,
        "witness_path": witness_path,
        "witness_rel": witness_rel,
        "witness_payload": witness_payload,
        "seal_commit": seal_commit,
        "source_identity": source,
    })
    return world


def witness_variant(world: dict, payload: dict, name: str) -> Path:
    """Write a throwaway witness INSIDE the repository, gitignored and uncommitted.

    Inside, so the builder resolves it to a repository-relative pointer and reaches the
    checks under test; gitignored, so it does not dirty a worktree the seal identity
    legitimately requires to be clean. It is deliberately NOT committed: a variant that
    is never a real witness cannot be mistaken for one later.
    """
    from scripts.verify_m62_control_plane import canonical_json

    path = Path(world["repo_root"]) / f"{name}.variant.json"
    path.write_text(canonical_json(payload), encoding="utf-8")
    return path


def reseal_witness(payload: dict) -> dict:
    """Re-digest a mutated witness so it is internally consistent but externally wrong."""
    from scripts.build_m62_eval_receipt import _sha256_bytes
    from scripts.verify_m62_control_plane import canonical_json

    body = {k: v for k, v in payload.items() if k != "witness_hash"}
    sealed = dict(body)
    sealed["witness_hash"] = _sha256_bytes(canonical_json(body).encode("utf-8"))
    return sealed


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
