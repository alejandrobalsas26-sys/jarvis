"""core/triage.py — Static forensic triage engine for neutralized commands."""

import asyncio
import json
import math
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    total = len(s)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


_MITRE_SIGNATURES: list[tuple[str, str, list[str]]] = [
    (
        "T1059.001",
        "PowerShell / Script Execution",
        [
            r"iex\b", r"invoke-expression", r"invoke-command", r"downloadstring",
            r"-encodedcommand", r"-enc\b", r"bypass", r"frombase64string",
        ],
    ),
    (
        "T1055",
        "Process Injection",
        [
            r"virtualalloc", r"writeprocessmemory", r"createremotethread",
            r"ntcreatethread", r"shellcode", r"reflectiveloader",
        ],
    ),
    (
        "T1083",
        "System Directory Enumeration",
        [
            r"\\windows\\system32", r"\\windows\\syswow64",
            r"c:\\users\\.*\\appdata", r"programdata", r"\\temp\\",
        ],
    ),
    (
        "T1562.001",
        "Security Software Tampering",
        [
            r"sc\s+stop", r"net\s+stop", r"set-mppreference",
            r"disable.*firewall", r"taskkill.*defender",
            r"bcdedit.*recoveryenabled.*no",
        ],
    ),
]

_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
)


def analyze_neutralized(
    command: str,
    validation_error: str,
    yara_hits: list[dict] | None = None,
) -> dict:
    """Pure static analysis — never executes command content."""
    entropy = _shannon_entropy(command)

    detections: list[dict] = []
    for technique_id, technique_name, patterns in _MITRE_SIGNATURES:
        matched = [p for p in patterns if re.search(p, command, re.IGNORECASE)]
        if matched:
            detections.append({
                "technique": technique_id,
                "name": technique_name,
                "matched_patterns": matched,
            })

    return {
        "entropy": round(entropy, 4),
        "high_entropy": entropy > 4.5,
        "mitre_detections": detections,
        "extracted_ips": _IPV4_RE.findall(command),
        "validation_error": validation_error,
        "yara_hits": yara_hits or [],
    }


def write_manifest(triage_result: dict, command: str) -> Path:
    queue_dir = Path("triage_queue")
    queue_dir.mkdir(exist_ok=True)

    ts = datetime.now(timezone.utc)
    entropy_tag = f"E{triage_result['entropy']:.2f}".replace(".", "_")
    techniques = [
        d["technique"].replace(".", "_")
        for d in triage_result.get("mitre_detections", [])
    ]
    technique_tag = techniques[0] if techniques else "T0000"
    filename = f"{ts.strftime('%Y%m%dT%H%M%S')}_{entropy_tag}_{technique_tag}.json"

    manifest = {
        "uuid": str(uuid.uuid4()),
        "timestamp": ts.isoformat(),
        "status": "PENDING_REVIEW",
        "command": command,
        "triage": triage_result,
        "vmware": {
            "snapshot_target": None,
            "vmx_path": None,
            "auto_approved": False,
        },
    }

    path = queue_dir / filename
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    # Schedule episodic memory storage — fire-and-forget, no block on failure
    try:
        from core.episodic_memory import store_episode
        loop = asyncio.get_running_loop()
        loop.create_task(store_episode(
            json.dumps(manifest),
            "triage",
            severity="HIGH",
            mitre_tags=triage_result.get("mitre_match", []),
        ))
    except RuntimeError:
        pass  # called from executor thread — executor.py also schedules via run_coroutine_threadsafe
    except Exception:
        pass

    return path
