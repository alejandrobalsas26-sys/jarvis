"""core/environment_registry.py — V67 M29: operator-controlled environment enrollment.

Discovery in JARVIS is NOT uncontrolled scanning. Before any inventory or probing,
the operator explicitly *enrolls* the environments they are authorized to observe
(the local Windows host, a Docker engine, VMware Workstation, an authorized remote
Linux host, a PNETLab/lab node). This registry is the auditable record of that
consent — it decides *which* environments discovery (:mod:`core.asset_discovery`)
is allowed to touch and *what scope* authorizes real actions there.

Security invariants:
  * **Enrollment is explicit and auditable.** Every enroll/authorize/revoke appends
    to an in-memory audit trail and logs at INFO. Nothing is auto-enrolled.
  * **Authorization is separate from enrollment.** An enrolled environment is inert
    until :meth:`authorize` is called with the operator's scope. Discovery and any
    downstream action refuse an un-authorized environment (fail-closed).
  * **Never store raw credentials.** ``credentials_ref`` is a *reference* (an env
    var name, a vault key, an SSH key *path*) — never a secret. Enrollment rejects
    a value that looks like an actual secret, and no projection ever emits it raw.
  * **Bounded & local-first.** JSON persistence, capped lists, no external DB.

Pure data + deterministic queries; no I/O beyond explicit save/load, no discovery,
no tool execution here (that lives in :mod:`core.asset_discovery`, gated on this).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from loguru import logger

SCHEMA_VERSION = "env-registry-1"
_MAX_AUDIT = 512
_MAX_NOTES = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EnvironmentType(str, Enum):
    LOCAL_WINDOWS = "local_windows"
    DOCKER = "docker"
    VMWARE = "vmware"
    REMOTE_LINUX = "remote_linux"
    LAB_NODE = "lab_node"          # PNETLab / eve-ng / generic lab node
    UNKNOWN = "unknown"


class EnvironmentHealth(str, Enum):
    UNKNOWN = "unknown"            # never probed — not a failure
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    DEGRADED = "degraded"


def _looks_like_secret(value: str) -> bool:
    """Best-effort guard: is this credentials_ref actually a raw secret?

    A *reference* (``env:SSH_KEY``, ``vault:lab/ssh``, ``C:\\keys\\lab.pem``) is
    fine; a raw private key / long high-entropy token is not. Reuses the memory
    discipline predicate when available, plus a couple of structural markers.
    """
    v = (value or "").strip()
    if not v:
        return False
    if "-----BEGIN" in v or "PRIVATE KEY" in v.upper():
        return True
    try:
        from core.memory_router import contains_secret
        if contains_secret(v):
            return True
    except Exception:  # noqa: BLE001
        pass
    # A bare long high-entropy blob with no scheme/path separators smells raw.
    if len(v) >= 40 and (" " not in v) and not any(c in v for c in ":/\\"):
        return True
    return False


@dataclass
class EnvironmentEntry:
    """One enrolled environment. ``credentials_ref`` is a reference, never a secret."""
    env_id: str
    env_type: EnvironmentType
    display_name: str
    endpoint: str = ""                 # local ref / host:port / pipe — NOT a secret
    authorization_scope: str = ""      # AuthorityState scope that authorizes actions here
    discovery_capabilities: frozenset[str] = field(default_factory=frozenset)  # {"inventory","service_scan"}
    collector_bindings: tuple[str, ...] = ()
    credentials_ref: str = ""          # reference only (env var / vault key / key path)
    owner: str = ""
    notes: str = ""
    authorized: bool = False
    health: EnvironmentHealth = EnvironmentHealth.UNKNOWN
    enrolled_at: str = field(default_factory=_now_iso)
    last_seen: str | None = None

    @property
    def has_credentials(self) -> bool:
        return bool(self.credentials_ref)

    def to_dict(self) -> dict:
        """Persistence projection — includes the credentials *reference* (a label,
        never a secret) so a saved registry round-trips."""
        return {
            "env_id": self.env_id,
            "env_type": self.env_type.value,
            "display_name": self.display_name,
            "endpoint": self.endpoint,
            "authorization_scope": self.authorization_scope,
            "discovery_capabilities": sorted(self.discovery_capabilities),
            "collector_bindings": list(self.collector_bindings),
            "credentials_ref": self.credentials_ref,
            "owner": self.owner,
            "notes": self.notes[:_MAX_NOTES],
            "authorized": self.authorized,
            "health": self.health.value,
            "enrolled_at": self.enrolled_at,
            "last_seen": self.last_seen,
        }

    def to_public_dict(self) -> dict:
        """AURA/HUD-safe projection: NEVER emits the credentials reference, only
        whether credentials are configured. Bounded."""
        return {
            "env_id": self.env_id,
            "env_type": self.env_type.value,
            "display_name": self.display_name,
            "endpoint": self.endpoint,
            "authorized": self.authorized,
            "scope": self.authorization_scope,
            "discovery_capabilities": sorted(self.discovery_capabilities),
            "collector_bindings": list(self.collector_bindings)[:12],
            "has_credentials": self.has_credentials,
            "owner": self.owner,
            "health": self.health.value,
            "enrolled_at": self.enrolled_at,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EnvironmentEntry":
        return cls(
            env_id=str(d["env_id"]),
            env_type=EnvironmentType(d.get("env_type", "unknown")),
            display_name=str(d.get("display_name", d["env_id"])),
            endpoint=str(d.get("endpoint", "")),
            authorization_scope=str(d.get("authorization_scope", "")),
            discovery_capabilities=frozenset(d.get("discovery_capabilities", [])),
            collector_bindings=tuple(d.get("collector_bindings", [])),
            credentials_ref=str(d.get("credentials_ref", "")),
            owner=str(d.get("owner", "")),
            notes=str(d.get("notes", "")),
            authorized=bool(d.get("authorized", False)),
            health=EnvironmentHealth(d.get("health", "unknown")),
            enrolled_at=str(d.get("enrolled_at", _now_iso())),
            last_seen=d.get("last_seen"),
        )


class EnrollmentError(ValueError):
    """Raised when an enrollment is rejected (fail-closed)."""


class EnvironmentRegistry:
    """The auditable set of enrolled, operator-authorized environments."""

    def __init__(self) -> None:
        self._envs: dict[str, EnvironmentEntry] = {}
        self._audit: list[dict] = []

    # ── audit ───────────────────────────────────────────────────────────────────
    def _record(self, action: str, env_id: str, detail: str = "") -> None:
        self._audit.append({"action": action, "env_id": env_id,
                            "detail": detail[:200], "at": _now_iso()})
        if len(self._audit) > _MAX_AUDIT:
            self._audit = self._audit[-_MAX_AUDIT:]
        logger.info(f"ENV_REGISTRY: {action} {env_id} {detail}".rstrip())

    def audit_trail(self, limit: int = 50) -> list[dict]:
        return self._audit[-max(0, limit):]

    # ── mutation ────────────────────────────────────────────────────────────────
    def enroll(
        self, env_id: str, env_type: "EnvironmentType | str", display_name: str,
        *, endpoint: str = "", authorization_scope: str = "",
        discovery_capabilities=(), credentials_ref: str = "", owner: str = "",
        notes: str = "", authorized: bool = False,
    ) -> EnvironmentEntry:
        """Enroll (or update) an environment. Rejects raw-secret credentials_ref.

        Enrollment does NOT authorize by default — call :meth:`authorize` (or pass
        ``authorized=True`` for an already-scoped operator action)."""
        env_id = str(env_id).strip()
        if not env_id:
            raise EnrollmentError("env_id is required")
        etype = env_type if isinstance(env_type, EnvironmentType) else EnvironmentType(env_type)
        if _looks_like_secret(credentials_ref):
            raise EnrollmentError(
                "credentials_ref must be a reference (env var / vault key / key path), "
                "never a raw secret"
            )
        entry = EnvironmentEntry(
            env_id=env_id, env_type=etype, display_name=str(display_name or env_id),
            endpoint=str(endpoint), authorization_scope=str(authorization_scope),
            discovery_capabilities=frozenset(discovery_capabilities),
            credentials_ref=str(credentials_ref), owner=str(owner),
            notes=str(notes)[:_MAX_NOTES], authorized=bool(authorized),
        )
        existed = env_id in self._envs
        self._envs[env_id] = entry
        self._record("update" if existed else "enroll", env_id,
                     f"type={etype.value} authorized={entry.authorized}")
        return entry

    def authorize(self, env_id: str, scope: str = "") -> EnvironmentEntry:
        entry = self._require(env_id)
        entry.authorized = True
        if scope:
            entry.authorization_scope = scope
        self._record("authorize", env_id, f"scope={entry.authorization_scope}")
        return entry

    def revoke(self, env_id: str) -> EnvironmentEntry:
        entry = self._require(env_id)
        entry.authorized = False
        self._record("revoke", env_id)
        return entry

    def bind_collector(self, env_id: str, collector_id: str) -> EnvironmentEntry:
        entry = self._require(env_id)
        if collector_id not in entry.collector_bindings:
            entry.collector_bindings = (*entry.collector_bindings, collector_id)
            self._record("bind_collector", env_id, collector_id)
        return entry

    def update_health(self, env_id: str, health: "EnvironmentHealth | str",
                      *, now_iso: str | None = None) -> EnvironmentEntry:
        entry = self._require(env_id)
        entry.health = health if isinstance(health, EnvironmentHealth) else EnvironmentHealth(health)
        entry.last_seen = now_iso or _now_iso()
        return entry

    def remove(self, env_id: str) -> None:
        if env_id in self._envs:
            del self._envs[env_id]
            self._record("remove", env_id)

    # ── queries ──────────────────────────────────────────────────────────────────
    def _require(self, env_id: str) -> EnvironmentEntry:
        entry = self._envs.get(env_id)
        if entry is None:
            raise EnrollmentError(f"unknown environment {env_id!r}")
        return entry

    def get(self, env_id: str) -> EnvironmentEntry | None:
        return self._envs.get(env_id)

    def all(self) -> list[EnvironmentEntry]:
        return list(self._envs.values())

    def by_type(self, env_type: "EnvironmentType | str") -> list[EnvironmentEntry]:
        etype = env_type if isinstance(env_type, EnvironmentType) else EnvironmentType(env_type)
        return [e for e in self._envs.values() if e.env_type is etype]

    def authorized_environments(self) -> list[EnvironmentEntry]:
        return [e for e in self._envs.values() if e.authorized]

    def is_authorized(self, env_id: str) -> bool:
        entry = self._envs.get(env_id)
        return bool(entry and entry.authorized)

    # ── views ────────────────────────────────────────────────────────────────────
    def to_aura_panel(self) -> dict:
        envs = [e.to_public_dict() for e in
                sorted(self._envs.values(), key=lambda x: x.env_id)][:24]
        return {
            "total": len(self._envs),
            "authorized": len(self.authorized_environments()),
            "environments": envs,
        }

    # ── persistence ───────────────────────────────────────────────────────────────
    def save(self, path: "str | Path") -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "environments": [e.to_dict() for e in self._envs.values()],
        }
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: "str | Path") -> "EnvironmentRegistry":
        reg = cls()
        p = Path(path)
        if not p.exists():
            return reg
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ENV_REGISTRY: load failed ({e}); starting empty")
            return reg
        for d in data.get("environments", []):
            try:
                entry = EnvironmentEntry.from_dict(d)
                reg._envs[entry.env_id] = entry
            except Exception as e:  # noqa: BLE001
                logger.debug(f"ENV_REGISTRY: skipping malformed entry: {e}")
        return reg


# Module-level singleton.
env_registry = EnvironmentRegistry()


def get_env_registry() -> EnvironmentRegistry:
    return env_registry
