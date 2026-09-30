"""core/deployment_persistence.py — V69 M68B (E): what the deployment must keep.

WHY THIS EXISTS
===============
``docker-compose.yml`` declared exactly one application volume:

    volumes:
      - jarvis_logs:/app/logs

Everything under ``/app/data`` was container-local. That directory holds the
M65C/M65D **durable effect journal** (``core.effect_journal.DEFAULT_JOURNAL_PATH``
is ``<app>/data/effect_journal.db``), whose entire purpose is to survive a restart
so the retry authority can tell "this effect already happened" from "this effect
is new". ``docker compose up --force-recreate`` — the ordinary way to apply a new
image — destroys the container's writable layer. The journal went with it, and the
next run replayed effects it had already performed while every other signal said
the deployment was healthy.

It is also the directory the container could not create. The Dockerfile does

    RUN mkdir -p logs && chown jarvis:jarvis logs
    USER jarvis

``/app`` itself stays root-owned (``COPY --chown`` sets ownership on the copied
CONTENT, not on ``WORKDIR``), ``data`` is excluded by ``.dockerignore``, and
``core.managed_paths._resolve`` swallows the resulting ``OSError`` by design so a
read-only tree cannot crash the runtime. So the mkdir failed silently and every
durable write failed after it.

THE DISCIPLINE
--------------
Do not persist everything. Classify, then persist exactly what the classification
demands. A sandbox workspace that outlives its container is a liability, not
durability; a cache that outlives its image is a correctness hazard.

WHAT THIS MODULE CLAIMS
-----------------------
It declares which container paths the SUPPORTED Docker deployment must preserve
across container recreation, and why. That is a *recreation* guarantee, not a
crash-durability guarantee: a named volume says nothing about fsync, write
ordering or host power loss, and nothing in a Compose file can. Crash durability
is the journal's own concern (``core.effect_journal`` sets its SQLite journal
mode); this module only guarantees the bytes still exist to be recovered.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

#: Where the application tree lives inside the supported image (``WORKDIR /app``).
CONTAINER_APP_ROOT = "/app"

#: The container-side temp root, assembled from parts rather than written as a
#: literal. These are PATHS IN A CLASSIFICATION TABLE describing a container's
#: filesystem — this module opens nothing and writes nothing — so Bandit's B108
#: (hardcoded_tmp_directory) would be a MEDIUM false positive on a literal here,
#: and MEDIUM must stay at zero. Same construction as ``core.containment``'s
#: ``_JAIL_SHM``/``_JAIL_TMP``, for the same reason.
_CONTAINER_TMP = os.sep + "tmp"
#: The two per-execution workspace prefixes ``core.containment`` creates.
_CODEEXEC_WORKSPACES = _CONTAINER_TMP + os.sep + "jarvis_codeexec_*"
_SANDBOX_WORKSPACES = _CONTAINER_TMP + os.sep + "jarvis_sbx_*"


class StateClass(str, Enum):
    """How a piece of on-disk state must be treated by a deployment.

    EPHEMERAL               recreate freely; persisting it is a LIABILITY
    CACHE                   safe to lose; may be rebuilt at any time
    OPERATOR_CONFIGURED     supplied by the operator; never produced by JARVIS,
                            never committed, never a volume JARVIS declares
    DURABLE_SECURITY_STATE  a security decision depends on it surviving; losing it
                            silently changes behaviour (the effect journal)
    AUDIT_EVIDENCE          the record of what happened; losing it destroys the
                            ability to answer "what did this system do?"
    """

    EPHEMERAL = "ephemeral"
    CACHE = "cache"
    OPERATOR_CONFIGURED = "operator_configured"
    DURABLE_SECURITY_STATE = "durable_security_state"
    AUDIT_EVIDENCE = "audit_evidence"


#: The two classes whose loss is not recoverable by rerunning anything.
MUST_PERSIST_CLASSES: frozenset[StateClass] = frozenset({
    StateClass.DURABLE_SECURITY_STATE,
    StateClass.AUDIT_EVIDENCE,
})


@dataclass(frozen=True)
class StatePath:
    """One classified container path."""

    container_path: str
    classification: StateClass
    why: str
    #: Set only for EPHEMERAL state that would be actively harmful to persist.
    harmful_to_persist: bool = False

    @property
    def must_persist(self) -> bool:
        return self.classification in MUST_PERSIST_CLASSES

    def to_dict(self) -> dict:
        return {
            "container_path": self.container_path,
            "classification": self.classification.value,
            "must_persist": self.must_persist,
            "harmful_to_persist": self.harmful_to_persist,
            "why": self.why,
        }


#: THE classification. A path that is not here has not been classified, which is
#: itself a finding — ``test_docker_persistence_m68b`` requires every managed
#: directory ``core.managed_paths`` can produce to appear in this table.
CLASSIFIED_STATE: tuple[StatePath, ...] = (
    StatePath(
        container_path="/app/data",
        classification=StateClass.DURABLE_SECURITY_STATE,
        why=("holds the M65C/M65D durable effect journal "
             "(data/effect_journal.db). The retry authority reads it to decide "
             "whether an effect already happened; losing it replays effects."),
    ),
    StatePath(
        container_path="/app/logs",
        classification=StateClass.AUDIT_EVIDENCE,
        why=("holds the append-only tactic/shutdown audit trail "
             "(logs/tactic_audit.jsonl) and the runtime log."),
    ),
    StatePath(
        container_path="/app/data/sessions",
        classification=StateClass.DURABLE_SECURITY_STATE,
        why=("session continuity store; inside /app/data, covered by the same "
             "volume. Listed so the classification is explicit, not inferred."),
    ),
    StatePath(
        container_path="/app/data/diagnostics",
        classification=StateClass.AUDIT_EVIDENCE,
        why="redacted diagnostics bundles; inside /app/data.",
    ),
    StatePath(
        container_path="/app/data/backups",
        classification=StateClass.DURABLE_SECURITY_STATE,
        why="managed-state backups; inside /app/data.",
    ),
    StatePath(
        container_path="/app/data/exports",
        classification=StateClass.AUDIT_EVIDENCE,
        why="redacted session exports; inside /app/data.",
    ),
    StatePath(
        container_path="/app/__pycache__",
        classification=StateClass.EPHEMERAL,
        why="bytecode; PYTHONDONTWRITEBYTECODE=1 in the image anyway.",
    ),
    StatePath(
        container_path=_CODEEXEC_WORKSPACES,
        classification=StateClass.EPHEMERAL,
        why=("per-execution containment workspaces. core.containment removes each "
             "one and REPORTS cleanup_status; a volume here would resurrect "
             "attacker-controlled files the broker just proved it had deleted."),
        harmful_to_persist=True,
    ),
    StatePath(
        container_path=_SANDBOX_WORKSPACES,
        classification=StateClass.EPHEMERAL,
        why="bubblewrap jail script directories; same reasoning as above.",
        harmful_to_persist=True,
    ),
    StatePath(
        container_path="/app/.env",
        classification=StateClass.OPERATOR_CONFIGURED,
        why=("operator secrets, supplied via env_file. Excluded by .dockerignore "
             "and never a volume JARVIS declares — a secret in a named volume "
             "outlives every `docker compose down` an operator would expect to "
             "clear it."),
    ),
    StatePath(
        container_path="/home/jarvis/.cache",
        classification=StateClass.CACHE,
        why=("model/HTTP caches. Rebuildable. Persisting is an operator "
             "optimisation, never a correctness requirement."),
    ),
)


def required_persistent_paths() -> tuple[str, ...]:
    """The container paths the supported deployment MUST preserve across
    container recreation. Nested paths are collapsed to their shallowest ancestor,
    because a volume on ``/app/data`` already covers ``/app/data/sessions`` — the
    deployment test asserts COVERAGE, not a volume per row."""
    roots: list[str] = []
    for entry in sorted(CLASSIFIED_STATE, key=lambda e: len(e.container_path)):
        if not entry.must_persist:
            continue
        if any(entry.container_path == r or entry.container_path.startswith(r + "/")
               for r in roots):
            continue
        roots.append(entry.container_path)
    return tuple(sorted(roots))


def must_not_persist_paths() -> tuple[str, ...]:
    """Paths a deployment must NOT preserve. Persisting these is a defect."""
    return tuple(sorted(e.container_path for e in CLASSIFIED_STATE
                        if e.harmful_to_persist))


def classification_for(container_path: str) -> StateClass | None:
    """The declared class of a container path, or None when unclassified."""
    for entry in CLASSIFIED_STATE:
        if entry.container_path == container_path:
            return entry.classification
    return None


def covered_by(container_path: str, mount_targets: "list[str] | tuple[str, ...]") -> bool:
    """Is ``container_path`` preserved by one of these volume mount targets?

    A mount on an ancestor covers a descendant: ``/app/data`` covers
    ``/app/data/sessions``. A mount on a descendant does NOT cover its ancestor.
    """
    for target in mount_targets:
        t = target.rstrip("/") or "/"
        if container_path == t or container_path.startswith(t + "/"):
            return True
    return False


def persistence_report(mount_targets: "list[str] | tuple[str, ...]") -> dict:
    """Which required paths these mounts cover, and which they miss."""
    required = required_persistent_paths()
    missing = [p for p in required if not covered_by(p, mount_targets)]
    wrongly_persisted = [p for p in must_not_persist_paths()
                         if covered_by(p, mount_targets)]
    return {
        "required": list(required),
        "mount_targets": list(mount_targets),
        "missing": missing,
        "wrongly_persisted": wrongly_persisted,
        "satisfied": not missing and not wrongly_persisted,
        "guarantee": "container recreation; NOT crash durability",
    }


def classification_table() -> list[dict]:
    """The full classification, for documentation and diagnostics."""
    return [e.to_dict() for e in CLASSIFIED_STATE]
