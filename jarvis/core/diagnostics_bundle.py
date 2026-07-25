"""core/diagnostics_bundle.py — V69 M60.6: bounded, redacted diagnostics bundles.

WHAT A BUNDLE IS FOR
--------------------
Answering "what state was this runtime in?" without shipping the conversation. Every
field is a version, a state, a count, a fingerprint or a hash. There is no mode that
includes conversation text, tool arguments, prompts, responses, audio or key events —
``FULL`` does not exist, so it cannot be requested by accident.

THE FINALIZE GATE
-----------------
Before a bundle is written, the whole serialized payload goes through
``core.redaction_policy.scan_structure``. A hit REFUSES the bundle instead of patching
it: a scanner match means the collection path has a leak, and shipping a quietly
repaired file would hide the bug that produced it.

DEFAULT IS PREVIEW
------------------
``build_bundle`` produces the payload; nothing reaches disk until ``write_bundle`` is
called explicitly, and even then only into the MANAGED diagnostics directory under an
application-generated name.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from core.managed_paths import diagnostics_dir, managed_path
from core.redaction_policy import redact_home_paths, scan_structure

BUNDLE_SCHEMA_VERSION = 1
MAX_BUNDLE_BYTES = 256 * 1024
MAX_ERROR_CLASSES = 20


class BundleMode(str, Enum):
    """The closed set. There is deliberately NO secret-bearing mode."""

    PREVIEW = "PREVIEW"                                   # nothing written
    REDACTED = "REDACTED"                                 # runtime state only
    REDACTED_WITH_SESSION_METADATA = "REDACTED_WITH_SESSION_METADATA"


class BundleState(str, Enum):
    PREVIEW = "PREVIEW"
    WRITTEN = "WRITTEN"
    REFUSED_SECRET_SCAN = "REFUSED_SECRET_SCAN"
    REFUSED_TOO_LARGE = "REFUSED_TOO_LARGE"
    WRITE_FAILED = "WRITE_FAILED"


@dataclass
class BundleResult:
    state: BundleState = BundleState.PREVIEW
    mode: BundleMode = BundleMode.PREVIEW
    sections: list[str] = field(default_factory=list)
    files_included: int = 0
    bundle_size: int = 0
    redactions: int = 0
    secret_scan: str = "NOT_RUN"
    leak_categories: list[str] = field(default_factory=list)
    file_name: str = ""
    payload: dict = field(default_factory=dict)
    error: str = ""

    def snapshot(self) -> dict:
        return {"last_bundle_state": self.state.value, "mode": self.mode.value,
                "files_included": self.files_included,
                "bundle_size": self.bundle_size, "redactions": self.redactions,
                "secret_scan_state": self.secret_scan,
                "leak_categories": list(self.leak_categories),
                "sections": list(self.sections), "error": self.error}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guard(fn, default=None):
    """Every collector is independently guarded — one broken subsystem must not stop
    the bundle that would explain why it is broken."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__} if default is None else default


# ══════════════════════════════════════════════════════════════════════════════
#  Section collectors (all read-only, all bounded)
# ══════════════════════════════════════════════════════════════════════════════
def _section_version() -> dict:
    from core.session_continuity import JOURNAL_SCHEMA_VERSION, read_git_commit
    out = {"git_commit": read_git_commit(), "journal_schema": JOURNAL_SCHEMA_VERSION,
           "bundle_schema": BUNDLE_SCHEMA_VERSION}
    try:
        from core.prompt_manifest import SECURITY_POLICY_VERSION
        out["security_policy_version"] = SECURITY_POLICY_VERSION
    except Exception:  # noqa: BLE001
        pass
    return out


def _section_config() -> dict:
    """Sanitized configuration: ALLOWLISTED non-secret keys only.

    An allowlist, not a denylist — a future setting named ``*_api_key`` cannot leak
    into a bundle by being forgotten, because it was never eligible in the first place.
    """
    allow = ("assistant_name", "whisper_model", "whisper_language",
             "fast_context", "response_profile", "response_max_output_tokens",
             "session_persistence_mode", "session_max_sessions",
             "session_max_turns", "recovery_supervisor_enabled")
    out: dict = {}
    try:
        from core.config import settings
    except Exception:  # noqa: BLE001
        return {"error": "config_unavailable"}
    for key in allow:
        value = getattr(settings, key, None)
        if value is not None and isinstance(value, (str, int, float, bool)):
            out[key] = value
    return out


def _section_models() -> dict:
    """Role → model NAMES. Never a key, an endpoint credential or a prompt."""
    try:
        from core.ops_views import model_runtime_panel
        panel = model_runtime_panel() or {}
        roles = panel.get("roles") or {}
        return {"roles": {str(k): str(v)[:48] for k, v in list(roles.items())[:12]}}
    except Exception:  # noqa: BLE001
        return {}


def _section_ollama() -> dict:
    from core.deployment_planner import _probe_host
    try:
        from core.ollama_native import default_base_url
        host = default_base_url()
    except Exception:  # noqa: BLE001
        host = ""
    # The URL itself is reported only as reachability + scheme/port shape — a bundle
    # does not need the operator's exact host string.
    return {"configured": bool(host), "reachable": _probe_host(host) if host else False}


def _section_runtime_health() -> dict:
    try:
        from core.runtime_health import build_live_runtime_health
        snap = build_live_runtime_health() or {}
        return {"overall": snap.get("overall"), "healthy": snap.get("healthy"),
                "degraded": list(snap.get("degraded") or [])[:12],
                "subsystems": [s.get("name") + "=" + str(s.get("status"))
                               for s in (snap.get("subsystems") or [])[:20]]}
    except Exception:  # noqa: BLE001
        return {}


def _section_lifecycle() -> dict:
    try:
        from core.lifecycle import get_lifecycle
        snap = get_lifecycle().snapshot()
        return {"state": snap.get("state"), "is_stopping": snap.get("is_stopping"),
                "text_ready_ms": snap.get("text_ready_ms"),
                "core_ready_ms": snap.get("core_ready_ms"),
                "operational_ready_ms": snap.get("operational_ready_ms")}
    except Exception:  # noqa: BLE001
        return {}


def _section_supervisor() -> dict:
    try:
        from core.recovery_supervisor import get_recovery_supervisor
        snap = get_recovery_supervisor().snapshot()
        snap.pop("services", None)          # names only in the panel, not the bundle
        return snap
    except Exception:  # noqa: BLE001
        return {}


def _section_persistence(journal=None) -> dict:
    try:
        from core.session_continuity import get_session_journal
        j = journal if journal is not None else get_session_journal()
        h = j.health()
        h.pop("store_error", None)          # may embed a path
        h.pop("active_session_id_hash", None)
        return h
    except Exception:  # noqa: BLE001
        return {}


def _section_session_metadata(journal=None) -> dict:
    """Only for REDACTED_WITH_SESSION_METADATA. IDs are HASHED; no text at all."""
    try:
        from core.session_continuity import get_session_journal, id_hash
        j = journal if journal is not None else get_session_journal()
        rows = []
        for s in j.sessions()[:10]:
            rows.append({"id_hash": id_hash(s.session_id), "state": s.state,
                         "language": s.language, "turns": s.turn_count,
                         "created_at": s.created_at,
                         "persistence_mode": s.persistence_mode})
        return {"sessions": rows}
    except Exception:  # noqa: BLE001
        return {}


def _section_recovery(recovery=None) -> dict:
    if recovery is None:
        return {}
    try:
        snap = recovery.snapshot()
        snap.pop("turns", None)             # turn snapshots are already content-free,
        snap.pop("tool_ops", None)          # but a bundle needs only the counts
        return snap
    except Exception:  # noqa: BLE001
        return {}


def _section_qualification() -> dict:
    try:
        from core.runtime_commands import run_quick_qualification
        r = run_quick_qualification()
        return {"verdict": r.get("verdict"), "passed": r.get("passed"),
                "failed": r.get("failed"), "total": r.get("total")}
    except Exception:  # noqa: BLE001
        return {}


def _section_error_classes(errors=None) -> dict:
    """Bounded recent error CLASSES — type names and counts, never messages."""
    if not errors:
        return {"classes": {}}
    counts: dict[str, int] = {}
    for e in list(errors)[-200:]:
        name = e if isinstance(e, str) else type(e).__name__
        key = str(name).split(":")[0][:40]
        counts[key] = counts.get(key, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:MAX_ERROR_CLASSES]
    return {"classes": dict(top)}


def _section_paths() -> dict:
    from core.managed_paths import describe
    return describe()


def _file_manifest(journal=None) -> list[dict]:
    """Managed FILES by name + size + content hash. Never file contents."""
    from core.managed_paths import backups_dir, diagnostics_dir as ddir, sessions_dir
    out: list[dict] = []
    for label, directory in (("sessions", sessions_dir(create=False)),
                             ("backups", backups_dir(create=False)),
                             ("diagnostics", ddir(create=False))):
        try:
            if not directory.is_dir():
                continue
            for p in sorted(directory.iterdir())[:20]:
                if not p.is_file():
                    continue
                try:
                    blob = p.read_bytes()[:1_000_000]
                    digest = hashlib.sha256(blob).hexdigest()[:16]
                    size = p.stat().st_size
                except OSError:
                    digest, size = "", -1
                out.append({"area": label, "name": p.name, "size": size,
                            "sha256_16": digest})
        except OSError:
            continue
    return out[:60]


# ══════════════════════════════════════════════════════════════════════════════
#  Build / write
# ══════════════════════════════════════════════════════════════════════════════
def build_bundle(mode: BundleMode = BundleMode.PREVIEW, *, journal=None,
                 recovery=None, errors=None, deployment_plan=None,
                 max_bytes: int = MAX_BUNDLE_BYTES) -> BundleResult:
    """Build the bundle PAYLOAD. Writes nothing. Runs the secret scan.

    A PREVIEW and a REDACTED bundle contain the SAME sections — preview is not a
    reduced sample, it is the identical payload not written to disk, so what the
    operator approves is exactly what would be shipped.
    """
    result = BundleResult(mode=mode)
    payload: dict = {
        "schema": "jarvis.diagnostics.bundle",
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "mode": mode.value,
        "generated_at": _now_iso(),
        "version": _guard(_section_version),
        "config": _guard(_section_config),
        "models": _guard(_section_models),
        "ollama": _guard(_section_ollama),
        "runtime_health": _guard(_section_runtime_health),
        "lifecycle": _guard(_section_lifecycle),
        "supervisor": _guard(_section_supervisor),
        "persistence": _guard(lambda: _section_persistence(journal)),
        "recovery": _guard(lambda: _section_recovery(recovery)),
        "qualification": _guard(_section_qualification),
        "error_classes": _guard(lambda: _section_error_classes(errors)),
        "paths": _guard(_section_paths),
        "file_manifest": _guard(lambda: _file_manifest(journal), default=[]),
    }
    if mode is BundleMode.REDACTED_WITH_SESSION_METADATA:
        payload["session_metadata"] = _guard(
            lambda: _section_session_metadata(journal))
    if deployment_plan is not None:
        # Always the REDACTED form — a bundle leaves the host.
        payload["deployment_plan"] = deployment_plan.snapshot(redact_home=True)

    result.sections = [k for k in payload if k not in
                       ("schema", "schema_version", "mode", "generated_at")]
    result.files_included = len(payload.get("file_manifest") or [])

    blob = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    # Home-path scrub over the WHOLE serialized bundle: a path may have reached it
    # through any nested section, so the sweep is global rather than per-field.
    blob, home_hits = redact_home_paths(blob)
    result.redactions = home_hits
    if home_hits:
        payload = json.loads(blob)

    leaks = scan_structure(payload)
    result.leak_categories = leaks
    result.secret_scan = "CLEAN" if not leaks else "LEAK_DETECTED"
    result.bundle_size = len(blob.encode("utf-8"))
    result.payload = payload

    if leaks:
        result.state = BundleState.REFUSED_SECRET_SCAN
        result.error = ",".join(leaks)
        return result
    if result.bundle_size > max_bytes:
        result.state = BundleState.REFUSED_TOO_LARGE
        result.error = f"{result.bundle_size} > {max_bytes}"
        return result
    result.state = BundleState.PREVIEW
    return result


def write_bundle(mode: BundleMode = BundleMode.REDACTED, *, journal=None,
                 recovery=None, errors=None, deployment_plan=None,
                 name: str | None = None,
                 max_bytes: int = MAX_BUNDLE_BYTES) -> BundleResult:
    """Write a bundle into the MANAGED diagnostics directory. PREVIEW never writes.

    Atomic rename, and a failed write removes its own temp file — a diagnostics
    directory must not accumulate half-written debris after a disk error.
    """
    result = build_bundle(mode, journal=journal, recovery=recovery, errors=errors,
                          deployment_plan=deployment_plan, max_bytes=max_bytes)
    if mode is BundleMode.PREVIEW or result.state is not BundleState.PREVIEW:
        return result
    stamp = hashlib.sha256(
        result.payload.get("generated_at", "").encode("utf-8")).hexdigest()[:10]
    leaf = name or f"diagnostics_{stamp}"
    try:
        target = managed_path(diagnostics_dir(), leaf, suffix=".json")
    except Exception as exc:  # noqa: BLE001
        result.state = BundleState.WRITE_FAILED
        result.error = type(exc).__name__
        return result
    blob = json.dumps(result.payload, ensure_ascii=False, indent=2, default=str)
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(blob, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        result.state = BundleState.WRITE_FAILED
        result.error = type(exc).__name__
        return result
    result.state = BundleState.WRITTEN
    result.file_name = target.name
    return result


def render_diagnostics_panel(result: BundleResult, *, language: str = "es") -> str:
    """The DIAGNOSTICS panel. States explicitly that no conversation is included."""
    english = str(language or "es").lower().startswith("en")
    rows = [
        ("mode", result.mode.value),
        ("state", result.state.value),
        ("sections", len(result.sections)),
        ("files_included", result.files_included),
        ("bundle_size", result.bundle_size),
        ("redactions", result.redactions),
        ("secret_scan", result.secret_scan),
        ("destination", "managed diagnostics directory"),
        ("file", result.file_name or "-"),
    ]
    if result.error:
        rows.append(("error", result.error))
    title = "DIAGNOSTICS" if english else "DIAGNOSTICO"
    lines = [title] + [f"  {k}={v}" for k, v in rows]
    note = ("no conversation content, tool arguments or secrets are included"
            if english else
            "no incluye contenido de conversacion, argumentos de herramientas "
            "ni secretos")
    lines.append(f"  ({note})")
    return "\n".join(lines)
