"""
core/boot_state.py — V68.1 M48: single truthful startup-state snapshot.

A real interactive run showed the boot narration inventing states that
contradicted reality:
  * "Visual cortex online. Moondream loaded." — but VISION is gemma3:4b.
  * "Detection subsystems active. ETW, Sysmon, canaries armed." — but ETW was
    disabled and Sysmon was dormant.
  * "Telegram bridge established." — but Telegram was disabled (no credentials).
  * "All systems nominal." — despite a failed self-test and missing integrations.

The root cause was that Guardian, self-test, boot narration and field readiness
each invented their own view of the world. This module builds ONE read-only
snapshot from the authoritative self-test report plus explicit runtime flags,
and every consumer (logs, spoken narration, AURA, self-test summary, field
readiness) renders from it. It never probes anything itself and never claims a
capability the evidence does not support.

Pure, deterministic, ASCII, dependency-light. Extends the spine.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Operational status taxonomy (mirrors core.self_test.classify_result).
OK = "OK"
ACTIVE = "ACTIVE"
DORMANT = "DORMANT"
OPTIONAL = "OPTIONAL"
DEGRADED = "DEGRADED"
FAILED = "FAILED"
DISABLED = "DISABLED"


@dataclass(frozen=True)
class BootSubsystem:
    key: str
    label: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label,
                "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class BootState:
    """A read-only, truthful snapshot of subsystem states at boot."""

    subsystems: tuple[BootSubsystem, ...] = field(default_factory=tuple)
    vision_model: str = "gemma3:4b"
    etw_enabled: bool = False
    sysmon_active: bool = False
    telegram_configured: bool = False
    postgres_available: bool = False
    failed: int = 0
    degraded: int = 0
    optional_missing: int = 0
    # V69 M54.7 — semantic-continuity truth folded into readiness. The live run
    # reported "SEMANTIC MEMORY [DEGRADED] / jarvis_episodic: REINDEX_REQUIRED" yet
    # still narrated "Episodic memory online" and "All systems nominal". These flags,
    # derived from core.semantic_migration.semantic_boot_summary(), make the ONE
    # snapshot tell the truth: a required-reindex degrades memory and blocks nominal.
    semantic_degraded: bool = False
    episodic_reindex_required: bool = False
    knowledge_vault_active: bool = False

    # ── Derived truth ────────────────────────────────────────────────────────

    def status_of(self, key: str) -> str:
        for s in self.subsystems:
            if s.key == key:
                return s.status
        return "UNKNOWN"

    def is_ok(self, key: str) -> bool:
        return self.status_of(key) in (OK, ACTIVE)

    def all_systems_nominal(self) -> bool:
        """True ONLY when no subsystem FAILED, none is DEGRADED, and semantic memory
        is not degraded. An optional integration being dormant does not, by itself,
        block 'nominal', but a failed/degraded required subsystem — including a
        required episodic-memory reindex — always does (M54.7)."""
        return (self.failed == 0 and self.degraded == 0
                and not self.semantic_degraded)

    def health(self) -> str:
        if self.failed:
            return "DEGRADED"
        if self.degraded:
            return "DEGRADED"
        if self.semantic_degraded:
            return "DEGRADED"
        return "OK"

    # ── Truthful narration ───────────────────────────────────────────────────

    def _detection_line(self) -> str:
        parts: list[str] = []
        if self.is_ok("canary") or self.is_ok("tarpit"):
            parts.append("canaries armed")
        else:
            parts.append("canaries dormant")
        parts.append("ETW active" if self.etw_enabled and self.is_ok("etw") else "ETW disabled")
        parts.append("Sysmon active" if self.sysmon_active else "Sysmon dormant")
        return "Detection online: " + "; ".join(parts) + "."

    def _vision_line(self) -> str:
        if self.is_ok("vision"):
            return f"Visual cortex online. {self.vision_model} loaded."
        return f"Vision model {self.vision_model} not loaded — vision degraded."

    def _comms_line(self) -> str:
        if self.telegram_configured and self.is_ok("telegram"):
            return "Telegram bridge established."
        return "Telegram disabled — credentials not configured."

    def _memory_line(self) -> str:
        """Episodic-memory narration. A required reindex is reported honestly —
        never "Episodic memory online" while jarvis_episodic is REINDEX_REQUIRED."""
        if self.episodic_reindex_required:
            return ("Semantic memory degraded — episodic memory requires migration "
                    "(reindex required); legacy collection preserved.")
        if self.semantic_degraded:
            return "Semantic memory degraded — embedding runtime unavailable."
        if self.is_ok("chromadb"):
            return "Episodic memory online."
        return "Episodic memory degraded — vector store unavailable."

    def _semantic_line(self) -> str:
        """Knowledge Vault + semantic-continuity status line."""
        vault = "Knowledge Vault active." if self.knowledge_vault_active \
            else "Knowledge Vault idle — no documents indexed."
        if self.episodic_reindex_required:
            return vault + " Episodic memory requires migration."
        if self.semantic_degraded:
            return vault + " Semantic memory degraded."
        return vault + " Semantic memory nominal."

    def _ready_line(self) -> str:
        if self.all_systems_nominal():
            base = "All systems nominal. JARVIS at your service."
            if self.optional_missing:
                base += f" ({self.optional_missing} optional integration(s) dormant.)"
            return base
        bits = []
        if self.failed:
            bits.append(f"{self.failed} failed")
        if self.degraded:
            bits.append(f"{self.degraded} degraded")
        if self.semantic_degraded:
            bits.append("degraded semantic memory")
        if self.optional_missing:
            bits.append(f"{self.optional_missing} optional dormant")
        # Preferred phrasing when the ONLY problem is semantic memory (the live case).
        if self.semantic_degraded and not self.failed and not self.degraded:
            base = ("JARVIS is ready with degraded semantic memory. "
                    + ("Knowledge Vault is active. " if self.knowledge_vault_active
                       else "")
                    + "Episodic memory requires migration.")
            if self.optional_missing:
                base += f" {self.optional_missing} optional integration(s) dormant."
            return base
        return (
            f"JARVIS online with reduced capability — {', '.join(bits)}."
        )

    def narration_lines(self) -> list[tuple[str, str]]:
        """Truthful (phase, message) pairs replacing the old hardcoded script."""
        lines: list[tuple[str, str]] = [
            ("hardware", "Hardware profile loaded."),
            ("memory", self._memory_line()),
            ("semantic", self._semantic_line()),
            ("llm",
             "Language models online." if self.is_ok("ollama")
             else "Language model server unavailable — degraded."),
            ("detection", self._detection_line()),
            ("correlation",
             "Correlation engine warm." if self.is_ok("correlator")
             else "Correlation engine dormant."),
            ("vision", self._vision_line()),
            ("persistence",
             "Alert persistence: PostgreSQL connected."
             if self.postgres_available
             else "Alert persistence degraded — PostgreSQL unavailable; "
                  "local durable store active."),
            ("communication", self._comms_line()),
            ("ready", self._ready_line()),
        ]
        return lines

    def to_dict(self) -> dict:
        return {
            "health": self.health(),
            "all_systems_nominal": self.all_systems_nominal(),
            "vision_model": self.vision_model,
            "etw_enabled": self.etw_enabled,
            "sysmon_active": self.sysmon_active,
            "telegram_configured": self.telegram_configured,
            "postgres_available": self.postgres_available,
            "failed": self.failed,
            "degraded": self.degraded,
            "optional_missing": self.optional_missing,
            "semantic_degraded": self.semantic_degraded,
            "episodic_reindex_required": self.episodic_reindex_required,
            "knowledge_vault_active": self.knowledge_vault_active,
            "subsystems": [s.to_dict() for s in self.subsystems],
        }


def _derive_semantic(summary: dict | None) -> tuple[bool, bool, bool]:
    """Fold core.semantic_migration.semantic_boot_summary() into three truthful
    flags: (semantic_degraded, episodic_reindex_required, knowledge_vault_active).

    Robust to a missing/malformed summary (degrades to all-False, never raises).
    'episodic' is matched by logical-name substring so jarvis_episodic's
    REINDEX_REQUIRED/MIGRATING status flips the reindex flag; any non-episodic
    ACTIVE collection is treated as an active Knowledge Vault.
    """
    if not isinstance(summary, dict):
        return (False, False, False)
    overall = str(summary.get("overall", "")).upper()
    semantic_degraded = overall == "DEGRADED"
    episodic_reindex = False
    vault_active = False
    for c in summary.get("collections", []) or []:
        if not isinstance(c, dict):
            continue
        logical = str(c.get("logical_name", "")).lower()
        status = str(c.get("status", "")).upper()
        if "episodic" in logical:
            if status in ("REINDEX_REQUIRED", "MIGRATING", "FAILED"):
                episodic_reindex = True
        elif status == "ACTIVE":
            vault_active = True
    if episodic_reindex:
        semantic_degraded = True
    return (semantic_degraded, episodic_reindex, vault_active)


def assemble_boot_state(
    self_test_report: dict | None,
    *,
    vision_model: str = "gemma3:4b",
    etw_enabled: bool = False,
    sysmon_active: bool = False,
    telegram_configured: bool = False,
    postgres_available: bool = False,
    semantic_summary: dict | None = None,
) -> BootState:
    """Build the single truthful boot snapshot from the self-test report plus
    explicit runtime flags AND the semantic-continuity summary (M54.7). Never
    probes; purely derives. Robust to a missing or malformed report/summary
    (degrades to an empty, honest snapshot)."""
    report = self_test_report or {}
    results = report.get("results", []) if isinstance(report, dict) else []

    subsystems: list[BootSubsystem] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        subsystems.append(BootSubsystem(
            key=str(r.get("id", "")),
            label=str(r.get("name", r.get("id", ""))),
            status=str(r.get("status", "UNKNOWN")),
            detail=str(r.get("detail", "")),
        ))

    failed = int(report.get("failed", 0) or 0)
    degraded = sum(1 for s in subsystems if s.status == DEGRADED)
    optional_missing = int(report.get("optional_missing", 0) or 0)
    semantic_degraded, episodic_reindex, vault_active = _derive_semantic(semantic_summary)

    return BootState(
        subsystems=tuple(subsystems),
        vision_model=vision_model,
        etw_enabled=etw_enabled,
        sysmon_active=sysmon_active,
        telegram_configured=telegram_configured,
        postgres_available=postgres_available,
        failed=failed,
        degraded=degraded,
        optional_missing=optional_missing,
        semantic_degraded=semantic_degraded,
        episodic_reindex_required=episodic_reindex,
        knowledge_vault_active=vault_active,
    )
