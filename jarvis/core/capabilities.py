"""
core/capabilities.py — V63: typed security-tool capability layer.

An extensible, typed registry for external security tooling. The point is NOT to
ship a wrapper for every tool in the world — it is to give the runtime a single,
honest, gated interface that:

  * knows which tools are *actually installed* on this host (AvailabilityProbe
    via shutil.which — no execution) and their version (VersionProbe, cached);
  * builds a **validated argv vector** (never a shell string, never interpolated
    user input — Purple-Team posture) for the capabilities it ships an adapter
    for;
  * parses raw output into a :class:`StructuredResult` where practical;
  * captures an :class:`EvidenceArtifact` (raw output on disk, hashed);
  * carries a risk class and a scope-bound flag so execution routes through the
    SAME operator-authority / risk / HITL / audit gates as every other action.

No-bypass contract: :func:`execute_capability` only ever runs a *registered*,
*available* capability, via ``shell=False`` argv, through an injectable runner.
There is no shell string, no ``os.system``, no arbitrary-binary path. It is a
sibling of ToolExecutor's gated shell path, not a parallel bypass — and the
executor integration (``ToolExecutor.run_capability``) applies the authority
scope check + NATO HITL challenge + audit before it runs.

Honesty contract (directive rule #14): capabilities for tools that are not
installed are registered as **inventory only** (``build_argv=None``) — the
registry reports them as unavailable and refuses to execute them. They are never
fake wrappers that pretend to work. Only the genuinely-present tools on this host
(nslookup, openssl) ship a real, tested execution adapter.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

from loguru import logger

from core.risk_classes import RiskClass

_ARTIFACT_BASE = Path(__file__).resolve().parent.parent / "logs" / "capabilities"
# The only directory cert_inspect may read from (sandbox — no arbitrary file read).
_CERT_BASE = _ARTIFACT_BASE / "certs"

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9_](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9_])?\.?)+$")
_DNS_TYPES = frozenset({"A", "AAAA", "MX", "TXT", "NS", "CNAME", "SOA", "PTR", "SRV"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CapabilityCategory(str, Enum):
    RECON = "recon"
    WEB = "web"
    DFIR = "dfir"
    REVERSE_ENGINEERING = "reverse_engineering"
    ACTIVE_DIRECTORY = "active_directory"
    CRYPTO = "crypto"


# ── validation helpers (safe argv construction) ──────────────────────────────
class CapabilityInputError(ValueError):
    """Raised when capability parameters fail validation (fail-closed)."""


def _valid_host(value: str) -> str:
    v = (value or "").strip()
    if not v or len(v) > 253:
        raise CapabilityInputError("host/target missing or too long")
    try:
        ipaddress.ip_address(v)
        return v
    except ValueError:
        pass
    if not _HOSTNAME_RE.match(v):
        raise CapabilityInputError(f"invalid host/target: {v!r}")
    return v


def _valid_port(value) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise CapabilityInputError("port must be an integer")
    if not 1 <= port <= 65535:
        raise CapabilityInputError("port out of range 1..65535")
    return port


# ── probes ────────────────────────────────────────────────────────────────────
class AvailabilityProbe:
    """shutil.which-based presence detection. Pure (no execution), cached."""

    _cache: dict[str, str | None] = {}

    @classmethod
    def path(cls, binary: str) -> str | None:
        if binary not in cls._cache:
            cls._cache[binary] = shutil.which(binary)
        return cls._cache[binary]

    @classmethod
    def available(cls, binary: str) -> bool:
        return cls.path(binary) is not None

    @classmethod
    def clear(cls) -> None:
        cls._cache.clear()


class VersionProbe:
    """Runs ``[binary, *version_args]`` (fixed args, NO user input) with a short
    timeout to read a version banner. Cached. shell=False; never touched by
    model/tool input, so it is not an execution surface for injection."""

    _cache: dict[str, str] = {}

    @classmethod
    def version(cls, binary: str, version_args: tuple[str, ...]) -> str:
        if binary in cls._cache:
            return cls._cache[binary]
        path = AvailabilityProbe.path(binary)
        if path is None:
            cls._cache[binary] = ""
            return ""
        try:
            proc = subprocess.run(
                [path, *version_args], capture_output=True, text=True,
                timeout=5.0, shell=False,
            )
            banner = (proc.stdout or proc.stderr or "").strip().splitlines()
            cls._cache[binary] = (banner[0][:120] if banner else "")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"CAPABILITY: version probe failed for {binary}: {e}")
            cls._cache[binary] = ""
        return cls._cache[binary]

    @classmethod
    def clear(cls) -> None:
        cls._cache.clear()


# ── result / artifact types ───────────────────────────────────────────────────
@dataclass
class EvidenceArtifact:
    path: str
    sha256: str
    size: int
    capability: str
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {
            "path": self.path, "sha256": self.sha256, "size": self.size,
            "capability": self.capability, "created_at": self.created_at,
        }

    @classmethod
    def capture(cls, capability: str, raw: str, base_dir: Path | None = None) -> "EvidenceArtifact | None":
        try:
            base = base_dir or _ARTIFACT_BASE
            base.mkdir(parents=True, exist_ok=True)
            data = (raw or "").encode("utf-8", errors="replace")
            digest = hashlib.sha256(data).hexdigest()
            # deterministic, collision-resistant filename (no timestamp needed)
            out = base / f"{capability}_{digest[:16]}.txt"
            out.write_bytes(data)
            return cls(path=str(out), sha256=digest, size=len(data), capability=capability)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"CAPABILITY: artifact capture failed: {e}")
            return None


@dataclass
class StructuredResult:
    capability: str
    ok: bool
    argv: list[str] = field(default_factory=list)
    summary: str = ""
    records: list[dict] = field(default_factory=list)
    raw_excerpt: str = ""
    error: str | None = None
    elapsed_s: float = 0.0
    artifact: EvidenceArtifact | None = None
    timestamp: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {
            "capability": self.capability, "ok": self.ok,
            "argv": list(self.argv), "summary": self.summary,
            "records": list(self.records), "raw_excerpt": self.raw_excerpt,
            "error": self.error, "elapsed_s": self.elapsed_s,
            "artifact": self.artifact.to_dict() if self.artifact else None,
            "timestamp": self.timestamp,
        }

    @classmethod
    def failure(cls, capability: str, error: str, argv: list[str] | None = None) -> "StructuredResult":
        return cls(capability=capability, ok=False, error=error, argv=argv or [])


# ── capability definition ─────────────────────────────────────────────────────
ArgvBuilder = Callable[[dict], list[str]]
OutputParser = Callable[[str], list[dict]]


@dataclass(frozen=True)
class ToolCapability:
    name: str
    binary: str
    category: CapabilityCategory
    risk_class: RiskClass
    version_args: tuple[str, ...] = ("--version",)
    scope_bound: bool = False
    target_param: str | None = None       # which param carries the network target
    build_argv: ArgvBuilder | None = None  # None → inventory-only (never executes)
    parse: OutputParser | None = None
    timeout_s: float = 30.0
    notes: str = ""

    @property
    def executable(self) -> bool:
        return self.build_argv is not None

    def available(self) -> bool:
        return AvailabilityProbe.available(self.binary)

    def probe(self) -> dict:
        avail = self.available()
        return {
            "name": self.name, "binary": self.binary,
            "category": self.category.value, "risk_class": self.risk_class.value,
            "available": avail, "executable": self.executable,
            "scope_bound": self.scope_bound,
            "version": VersionProbe.version(self.binary, self.version_args) if avail else "",
            "notes": self.notes,
        }


# ════════════════════════════════════════════════════════════════════════════
#  Real adapters for the tools ACTUALLY present on this host
# ════════════════════════════════════════════════════════════════════════════
def _build_dns_lookup(params: dict) -> list[str]:
    name = _valid_host(str(params.get("name", "")))
    rtype = str(params.get("type", "A")).strip().upper()
    if rtype not in _DNS_TYPES:
        raise CapabilityInputError(f"unsupported DNS record type {rtype!r}")
    argv = ["nslookup", f"-type={rtype}", name]
    server = params.get("server")
    if server:
        argv.append(_valid_host(str(server)))
    return argv


def _parse_dns_lookup(out: str) -> list[dict]:
    records: list[dict] = []
    for line in (out or "").splitlines():
        line = line.strip()
        m = re.match(r"^(?:Name|Address(?:es)?|canonical name|"
                     r"[A-Za-z0-9_.-]+\s+(?:internet address|has address))\b", line)
        if "Address:" in line and not line.startswith("Server"):
            addr = line.split("Address:", 1)[1].strip()
            if addr:
                records.append({"type": "address", "value": addr})
        elif line.lower().startswith("name:"):
            records.append({"type": "name", "value": line.split(":", 1)[1].strip()})
        elif "canonical name" in line.lower():
            records.append({"type": "cname", "value": line.split("=")[-1].strip().rstrip(".")})
        elif m:
            records.append({"type": "record", "value": line})
    return records


def _build_cert_inspect(params: dict) -> list[str]:
    raw = str(params.get("path", "")).strip()
    if not raw:
        raise CapabilityInputError("cert 'path' required")
    # Sandbox: resolve and confirm the file lives under the allowed cert dir.
    try:
        resolved = Path(raw).resolve()
        _CERT_BASE.mkdir(parents=True, exist_ok=True)
        resolved.relative_to(_CERT_BASE.resolve())
    except (ValueError, OSError):
        raise CapabilityInputError(
            f"cert path must be inside {_CERT_BASE} (sandbox)")
    if not resolved.is_file():
        raise CapabilityInputError("cert file not found")
    return ["openssl", "x509", "-in", str(resolved), "-noout",
            "-subject", "-issuer", "-dates", "-fingerprint", "-sha256"]


def _parse_cert_inspect(out: str) -> list[dict]:
    records: list[dict] = []
    for line in (out or "").splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            records.append({"field": key.strip().lower(), "value": val.strip()})
    return records


# ── the registry ──────────────────────────────────────────────────────────────
class CapabilityRegistry:
    def __init__(self) -> None:
        self._caps: dict[str, ToolCapability] = {}

    def register(self, cap: ToolCapability) -> None:
        self._caps[cap.name] = cap

    def get(self, name: str) -> ToolCapability | None:
        return self._caps.get(name)

    def names(self) -> list[str]:
        return sorted(self._caps)

    def all(self) -> list[ToolCapability]:
        return list(self._caps.values())

    def available(self) -> list[ToolCapability]:
        """The subset actually installed on this host (honest inventory)."""
        return [c for c in self._caps.values() if c.available()]

    def executable_available(self) -> list[ToolCapability]:
        """Installed AND shipping a real execution adapter."""
        return [c for c in self._caps.values() if c.available() and c.executable]

    def inventory(self) -> list[dict]:
        return [c.probe() for c in self._caps.values()]

    def build_argv(self, name: str, params: dict) -> list[str]:
        cap = self.get(name)
        if cap is None:
            raise CapabilityInputError(f"unknown capability {name!r}")
        if not cap.executable:
            raise CapabilityInputError(f"capability {name!r} has no execution adapter")
        return cap.build_argv(params or {})  # type: ignore[misc]


def build_default_registry() -> CapabilityRegistry:
    """The default registry: real adapters for present tools + honest inventory
    metadata for the rest (probed, never faked)."""
    reg = CapabilityRegistry()

    # ── real, available, executing adapters ──
    reg.register(ToolCapability(
        name="dns_lookup", binary="nslookup", category=CapabilityCategory.RECON,
        risk_class=RiskClass.READ_ONLY, version_args=("-version",),
        scope_bound=True, target_param="name",
        build_argv=_build_dns_lookup, parse=_parse_dns_lookup, timeout_s=15.0,
        notes="DNS enumeration via the system resolver (nslookup).",
    ))
    reg.register(ToolCapability(
        name="cert_inspect", binary="openssl", category=CapabilityCategory.CRYPTO,
        risk_class=RiskClass.READ_ONLY, version_args=("version",),
        scope_bound=False, target_param=None,
        build_argv=_build_cert_inspect, parse=_parse_cert_inspect, timeout_s=15.0,
        notes="Parse an X.509 cert from the sandboxed cert dir (openssl x509).",
    ))

    # ── inventory-only metadata (probed availability, NO execution adapter) ──
    # These are honest capability descriptors — the registry reports whether the
    # tool is installed and its version, but ships no wrapper until a real,
    # tested adapter is justified. build_argv=None → execute_capability refuses.
    _inv = [
        ("nmap", "nmap", CapabilityCategory.RECON, RiskClass.HIGH_IMPACT, ("--version",)),
        ("masscan", "masscan", CapabilityCategory.RECON, RiskClass.LAB_ONLY, ("--version",)),
        ("nuclei", "nuclei", CapabilityCategory.WEB, RiskClass.HIGH_IMPACT, ("-version",)),
        ("ffuf", "ffuf", CapabilityCategory.WEB, RiskClass.HIGH_IMPACT, ("-V",)),
        ("gobuster", "gobuster", CapabilityCategory.WEB, RiskClass.HIGH_IMPACT, ("version",)),
        ("yara", "yara", CapabilityCategory.DFIR, RiskClass.READ_ONLY, ("--version",)),
        ("tshark", "tshark", CapabilityCategory.DFIR, RiskClass.LAB_ONLY, ("--version",)),
        ("zeek", "zeek", CapabilityCategory.DFIR, RiskClass.READ_ONLY, ("--version",)),
        ("suricata", "suricata", CapabilityCategory.DFIR, RiskClass.READ_ONLY, ("-V",)),
        ("volatility", "vol", CapabilityCategory.DFIR, RiskClass.READ_ONLY, ("--version",)),
        ("capa", "capa", CapabilityCategory.REVERSE_ENGINEERING, RiskClass.READ_ONLY, ("--version",)),
        ("radare2", "r2", CapabilityCategory.REVERSE_ENGINEERING, RiskClass.READ_ONLY, ("-v",)),
        ("netexec", "nxc", CapabilityCategory.ACTIVE_DIRECTORY, RiskClass.LAB_ONLY, ("--version",)),
    ]
    for name, binary, cat, risk, vargs in _inv:
        reg.register(ToolCapability(
            name=name, binary=binary, category=cat, risk_class=risk,
            version_args=vargs, notes="inventory-only (no shipped adapter)",
        ))
    return reg


# ── gated execution ───────────────────────────────────────────────────────────
# rc, stdout, stderr
CapabilityRunner = Callable[[list[str], float], Awaitable["tuple[int, str, str]"]]


async def _default_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Real subprocess runner: shell=False, argv vector, hard timeout, in a
    worker thread so the event loop is never blocked (Rule of Silicon)."""
    import asyncio

    def _run() -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, shell=False,
            )
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"
        except FileNotFoundError:
            return 127, "", "binary not found"

    return await asyncio.to_thread(_run)


async def execute_capability(
    registry: CapabilityRegistry,
    name: str,
    params: dict,
    *,
    runner: CapabilityRunner | None = None,
    authority=None,
    audit=None,
    artifacts_dir: Path | None = None,
) -> StructuredResult:
    """Run a registered, available capability through the no-bypass gated path.

    Order: exists → executable → installed → validated argv → authority scope
    (fail-closed for scope-bound targets) → run (shell=False) → parse → artifact
    → audit. Any gate failure returns a StructuredResult, never raises, never a
    shell string. The NATO HITL challenge is applied by the ToolExecutor wrapper
    (:meth:`ToolExecutor.run_capability`) before this is reached in production."""
    import time

    cap = registry.get(name)
    if cap is None:
        return StructuredResult.failure(name, "unknown capability")
    if not cap.executable:
        return StructuredResult.failure(name, "no execution adapter (inventory-only)")
    if not cap.available():
        return StructuredResult.failure(name, f"tool '{cap.binary}' not installed")

    try:
        argv = cap.build_argv(params or {})  # type: ignore[misc]
    except CapabilityInputError as e:
        return StructuredResult.failure(name, f"invalid input: {e}")

    # Authority scope preflight (fail-closed) for target-bound capabilities.
    if cap.scope_bound and authority is not None and cap.target_param:
        target = str((params or {}).get(cap.target_param, "")).strip()
        try:
            if authority.enforcement_active() and not authority.is_in_scope(target):
                msg = f"target '{target}' outside authorized scope — refused"
                if audit is not None:
                    audit.log_action(f"capability:{name}", "", "blocked:scope", "blocked", msg[:200])
                return StructuredResult.failure(name, msg, argv)
        except Exception:  # noqa: BLE001 — an authority error must fail closed
            return StructuredResult.failure(name, "authority check failed — refused", argv)

    run = runner or _default_runner
    start = time.monotonic()
    rc, out, err = await run(argv, cap.timeout_s)
    elapsed = round(time.monotonic() - start, 2)

    combined = out if out.strip() else err
    records = cap.parse(out) if (cap.parse and rc == 0) else []
    artifact = EvidenceArtifact.capture(name, combined, artifacts_dir)
    ok = rc == 0
    result = StructuredResult(
        capability=name, ok=ok, argv=argv,
        summary=f"{name} rc={rc} ({len(records)} records)",
        records=records, raw_excerpt=combined[:500],
        error=None if ok else (err.strip()[:200] or f"exit {rc}"),
        elapsed_s=elapsed, artifact=artifact,
    )
    if audit is not None:
        audit.log_action(f"capability:{name}", "", "capability", "success" if ok else "error",
                         result.summary[:200])
    return result


# Module singleton — the default capability inventory.
registry = build_default_registry()
