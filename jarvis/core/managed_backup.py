"""core/managed_backup.py — V69 M60.6.1: atomic backup and dry-run-first restore.

SCOPE — JARVIS-MANAGED STATE ONLY
---------------------------------
Eligible: the session-continuity journal, the operational store, the alias registry,
configuration fingerprints, conversation digests and non-secret application metadata.

Semantic collections are NOT copied here. A Chroma directory copied while the database
is live is not a backup, it is a plausible-looking corrupt file — those go through
their own existing checkpoint/export seam (M53) or not at all. This module refuses
rather than produces something that only LOOKS like a backup.

RESTORE IS DRY-RUN FIRST, ALWAYS
--------------------------------
``plan_restore`` inspects and reports; ``apply_restore`` requires an explicit operator
approval token AND a passed plan. Before replacement it takes a rollback backup of the
current managed state, so a restore that turns out to be wrong is itself reversible.
Nothing restored is ever launched: no job, no service, no tool, no HITL token.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from loguru import logger

from core.managed_paths import (
    app_root, backups_dir, data_dir, managed_path, sessions_dir,
)

BACKUP_SCHEMA_VERSION = 1
MAX_BACKUP_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MANIFEST_NAME = "manifest.json"

# The closed set of eligible managed artifacts, as (label, path factory).
_ELIGIBLE: tuple[tuple[str, str], ...] = (
    ("session_journal", "data/sessions/session_continuity.db"),
    ("operational_store", "data/operational_state.db"),
    # V69 M65C — the durable effect journal. Eligible precisely because §25
    # forbids repairing a corrupt one by deleting it: restoring from a managed
    # backup is the recovery route, and it can only be that if the journal is
    # backed up. It holds ids, states and digests — no secret — so it does not
    # trip the exclusion markers below.
    ("effect_journal", "data/effect_journal.db"),
    ("alias_registry", "data/alias_registry.json"),
)
# Paths that must NEVER enter a backup, even if they appear under data/.
_EXCLUDED_MARKERS = ("chroma", "vector", ".env", "credential", "secret", "token")


class BackupState(str, Enum):
    OK = "OK"
    EMPTY = "EMPTY"                       # nothing eligible existed
    FAILED = "FAILED"
    REFUSED = "REFUSED"


class RestoreState(str, Enum):
    PLANNED = "PLANNED"
    INCOMPATIBLE = "INCOMPATIBLE"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    REFUSED = "REFUSED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass
class BackupResult:
    state: BackupState = BackupState.EMPTY
    file_name: str = ""
    size: int = 0
    members: list[str] = field(default_factory=list)
    integrity_hash: str = ""
    integrity_verified: bool = False
    git_commit: str = ""
    created_at: str = ""
    error: str = ""

    def snapshot(self) -> dict:
        return {"last_backup_state": self.state.value,
                "last_backup_at": self.created_at,
                "last_backup_size": self.size,
                "members": list(self.members),
                "integrity_verified": self.integrity_verified,
                "error": self.error}


@dataclass
class RestorePlan:
    state: RestoreState = RestoreState.PLANNED
    file_name: str = ""
    members: list[str] = field(default_factory=list)
    source_git_commit: str = ""
    source_created_at: str = ""
    schema_version: int = 0
    integrity_verified: bool = False
    compatible: bool = False
    incompatibilities: list[str] = field(default_factory=list)
    would_replace: list[str] = field(default_factory=list)
    rollback_available: bool = False
    dry_run: bool = True
    jobs_launched: int = 0                # STRUCTURALLY always 0
    error: str = ""

    def snapshot(self) -> dict:
        return {"restore_plan_state": self.state.value,
                "members": list(self.members),
                "integrity_verified": self.integrity_verified,
                "compatible": self.compatible,
                "incompatibilities": list(self.incompatibilities),
                "would_replace": list(self.would_replace),
                "rollback_available": self.rollback_available,
                "dry_run": self.dry_run, "jobs_launched": self.jobs_launched,
                "error": self.error}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _is_excluded(rel: str) -> bool:
    low = rel.replace("\\", "/").lower()
    return any(marker in low for marker in _EXCLUDED_MARKERS)


def eligible_artifacts(root: Path | None = None) -> list[tuple[str, Path]]:
    """Existing eligible artifacts. An excluded or missing path is simply absent."""
    base = root or app_root()
    out: list[tuple[str, Path]] = []
    for label, rel in _ELIGIBLE:
        if _is_excluded(rel):
            continue
        p = base / rel
        if p.is_file():
            out.append((label, p))
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  Backup
# ══════════════════════════════════════════════════════════════════════════════
def create_backup(*, name: str | None = None, root: Path | None = None,
                  destination: Path | None = None,
                  max_bytes: int = MAX_BACKUP_BYTES) -> BackupResult:
    """Create ONE atomic, integrity-hashed backup of the managed state.

    Written to a ``.tmp`` and renamed, so a crash mid-write never leaves a file that
    looks like a valid backup. The manifest records the schema version, the source Git
    commit and a per-member hash; the archive's own hash is verified by re-reading it
    after the rename (verifying the buffer we just wrote would prove nothing).
    """
    base = root or app_root()
    dest_dir = destination or backups_dir()
    result = BackupResult(created_at=_now_iso())
    artifacts = eligible_artifacts(base)
    if not artifacts:
        result.state = BackupState.EMPTY
        return result

    try:
        from core.session_continuity import read_git_commit
        result.git_commit = read_git_commit()
    except Exception:  # noqa: BLE001
        result.git_commit = ""

    stamp = hashlib.sha256(result.created_at.encode("utf-8")).hexdigest()[:10]
    leaf = name or f"managed_state_{stamp}"
    try:
        target = managed_path(dest_dir, leaf, suffix=".zip")
    except Exception as exc:  # noqa: BLE001
        result.state = BackupState.REFUSED
        result.error = type(exc).__name__
        return result

    manifest = {
        "schema": "jarvis.managed.backup",
        "schema_version": BACKUP_SCHEMA_VERSION,
        "created_at": result.created_at,
        "git_commit": result.git_commit,
        "members": [],
    }
    tmp = target.with_suffix(".zip.tmp")
    total = 0
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for label, path in artifacts:
                size = path.stat().st_size
                total += size
                if total > max_bytes or size > MAX_MEMBER_BYTES:
                    raise OSError(f"backup exceeds bound at {label}")
                arcname = f"{label}/{path.name}"
                zf.write(path, arcname)
                manifest["members"].append({
                    "label": label, "arcname": arcname, "size": size,
                    "sha256": _sha256_file(path)})
                result.members.append(label)
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2,
                                                  ensure_ascii=False))
        tmp.replace(target)
    except (OSError, zipfile.BadZipFile) as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        result.state = BackupState.FAILED
        result.error = type(exc).__name__
        logger.warning(f"BACKUP: failed ({type(exc).__name__}); no file retained")
        return result

    # Verify by RE-READING the written file, not the in-memory buffer.
    result.integrity_hash = _sha256_file(target)
    result.size = target.stat().st_size
    result.file_name = target.name
    result.integrity_verified = verify_backup(target).get("valid", False)
    result.state = BackupState.OK if result.integrity_verified else BackupState.FAILED
    if not result.integrity_verified:
        result.error = "integrity_check_failed"
    return result


def verify_backup(path: Path) -> dict:
    """Verify a backup archive: readable, manifest present, per-member hash matches."""
    out = {"valid": False, "members": 0, "schema_version": 0, "error": ""}
    try:
        with zipfile.ZipFile(path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                out["error"] = f"corrupt_member:{bad[:40]}"
                return out
            if MANIFEST_NAME not in zf.namelist():
                out["error"] = "manifest_missing"
                return out
            manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
            out["schema_version"] = int(manifest.get("schema_version", 0))
            members = manifest.get("members") or []
            out["members"] = len(members)
            for m in members:
                arc = str(m.get("arcname", ""))
                if arc not in zf.namelist():
                    out["error"] = f"missing:{arc[:40]}"
                    return out
                digest = hashlib.sha256(zf.read(arc)).hexdigest()
                if m.get("sha256") and digest != m["sha256"]:
                    out["error"] = f"hash_mismatch:{arc[:40]}"
                    return out
            out["valid"] = True
            out["created_at"] = manifest.get("created_at", "")
            out["git_commit"] = manifest.get("git_commit", "")
            return out
    except (OSError, zipfile.BadZipFile, ValueError, KeyError) as exc:
        out["error"] = type(exc).__name__
        return out


def list_backups(destination: Path | None = None) -> list[dict]:
    """Bounded listing of managed backups: name, size, integrity — never contents."""
    dest = destination or backups_dir(create=False)
    out: list[dict] = []
    try:
        if not dest.is_dir():
            return out
        for p in sorted(dest.glob("*.zip"))[-20:]:
            info = verify_backup(p)
            out.append({"name": p.name, "size": p.stat().st_size,
                        "valid": info.get("valid", False),
                        "members": info.get("members", 0),
                        "created_at": info.get("created_at", "")})
    except OSError:
        pass
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  Restore — dry-run first
# ══════════════════════════════════════════════════════════════════════════════
def plan_restore(path: Path, *, root: Path | None = None) -> RestorePlan:
    """Inspect a backup and REPORT what a restore would do. Changes nothing."""
    base = root or app_root()
    plan = RestorePlan(file_name=Path(path).name)
    info = verify_backup(Path(path))
    plan.integrity_verified = bool(info.get("valid"))
    plan.schema_version = int(info.get("schema_version", 0))
    plan.source_created_at = str(info.get("created_at", ""))
    plan.source_git_commit = str(info.get("git_commit", ""))
    if not plan.integrity_verified:
        plan.state = RestoreState.INCOMPATIBLE
        plan.incompatibilities.append(str(info.get("error") or "integrity_failed"))
        plan.error = str(info.get("error") or "integrity_failed")
        return plan
    if plan.schema_version != BACKUP_SCHEMA_VERSION:
        # A schema mismatch is never merged silently; it needs a migration first.
        plan.state = RestoreState.INCOMPATIBLE
        plan.incompatibilities.append(
            f"schema {plan.schema_version} != {BACKUP_SCHEMA_VERSION}")
        return plan
    try:
        with zipfile.ZipFile(Path(path), "r") as zf:
            manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        plan.state = RestoreState.INCOMPATIBLE
        plan.error = type(exc).__name__
        return plan

    lookup = {label: rel for label, rel in _ELIGIBLE}
    for m in manifest.get("members") or []:
        label = str(m.get("label", ""))
        if label not in lookup:
            plan.incompatibilities.append(f"unknown_member:{label[:24]}")
            continue
        plan.members.append(label)
        if (base / lookup[label]).is_file():
            plan.would_replace.append(label)
    if plan.incompatibilities:
        plan.state = RestoreState.INCOMPATIBLE
        return plan
    plan.compatible = True
    plan.rollback_available = bool(eligible_artifacts(base))
    plan.state = RestoreState.PLANNED
    plan.jobs_launched = 0
    return plan


def apply_restore(path: Path, plan: RestorePlan, approval: str, *,
                  root: Path | None = None,
                  destination: Path | None = None) -> RestorePlan:
    """Apply a restore. Requires an EXPLICIT approval token AND a compatible plan.

    Sequence: back up the CURRENT managed state (the rollback record), extract each
    member to a ``.restore.tmp`` beside its target, then rename into place. A failure
    part-way removes every temp file and reports ROLLED_BACK — the rollback backup is
    on disk and named in the plan, so the operator can reverse the whole operation.

    Nothing restored is started. There is no code path here that creates a task,
    starts a service, executes a tool or reinstates an authorization.
    """
    base = root or app_root()
    if approval != "OPERATOR_APPROVED":
        plan.state = RestoreState.REFUSED
        plan.error = "approval_required"
        return plan
    if not plan.compatible or plan.state is not RestoreState.PLANNED:
        plan.state = RestoreState.REFUSED
        plan.error = plan.error or "plan_not_approved"
        return plan

    rollback = create_backup(name=None, root=base, destination=destination)
    plan.rollback_available = rollback.state in (BackupState.OK, BackupState.EMPTY)
    if rollback.state is BackupState.FAILED:
        plan.state = RestoreState.REFUSED
        plan.error = "rollback_backup_failed"
        return plan

    lookup = {label: rel for label, rel in _ELIGIBLE}
    written: list[Path] = []
    temps: list[Path] = []
    try:
        with zipfile.ZipFile(Path(path), "r") as zf:
            manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
            for m in manifest.get("members") or []:
                label = str(m.get("label", ""))
                rel = lookup.get(label)
                if rel is None:
                    continue
                target = base / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(target.suffix + ".restore.tmp")
                temps.append(tmp)
                with zf.open(str(m.get("arcname", "")), "r") as src, \
                        tmp.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=1 << 20)
                tmp.replace(target)
                written.append(target)
    except (OSError, zipfile.BadZipFile, ValueError, KeyError) as exc:
        for t in temps:
            try:
                t.unlink(missing_ok=True)
            except OSError:
                pass
        plan.state = RestoreState.ROLLED_BACK
        plan.error = type(exc).__name__
        logger.warning(f"RESTORE: failed ({type(exc).__name__}); rollback backup "
                       f"{rollback.file_name or 'none'} retained")
        return plan
    finally:
        for t in temps:
            try:
                t.unlink(missing_ok=True)
            except OSError:
                pass

    plan.state = RestoreState.APPLIED
    plan.dry_run = False
    plan.jobs_launched = 0
    logger.info(f"RESTORE: applied {len(written)} member(s); rollback backup "
                f"{rollback.file_name or 'none'}")
    return plan


def render_backup_panel(backup: "BackupResult | None" = None,
                        plan: "RestorePlan | None" = None,
                        *, language: str = "es") -> str:
    english = str(language or "es").lower().startswith("en")
    rows: list[tuple[str, object]] = []
    if backup is not None:
        rows += [("last_backup_state", backup.state.value),
                 ("last_backup_size", backup.size),
                 ("members", ",".join(backup.members) or "-"),
                 ("integrity_verified", "yes" if backup.integrity_verified else "no")]
    if plan is not None:
        rows += [("restore_plan_state", plan.state.value),
                 ("dry_run", "yes" if plan.dry_run else "no"),
                 ("compatible", "yes" if plan.compatible else "no"),
                 ("would_replace", ",".join(plan.would_replace) or "-"),
                 ("rollback_available", "yes" if plan.rollback_available else "no"),
                 ("jobs_launched", plan.jobs_launched)]
    title = "MANAGED BACKUP" if english else "RESPALDO GESTIONADO"
    lines = [title] + [f"  {k}={v}" for k, v in rows]
    note = ("semantic collections are excluded; restored state is never launched"
            if english else
            "las colecciones semanticas quedan excluidas; el estado restaurado "
            "nunca se ejecuta")
    lines.append(f"  ({note})")
    return "\n".join(lines)


def managed_state_paths(root: Path | None = None) -> dict:
    """Where the managed state lives, relative to the app root (no home path)."""
    base = root or app_root()
    return {"data": str(data_dir(create=False).relative_to(base)),
            "sessions": str(sessions_dir(create=False).relative_to(base)),
            "eligible": [label for label, _ in _ELIGIBLE]}
