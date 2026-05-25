"""
tools/ioc_extractor.py — STIX 2.1 IOC extraction and export (v33.0).

Converts JARVIS compound incidents into structured STIX 2.1 Bundles:
  - Indicator objects  (IPs, domains, process names, hashes)
  - Attack Pattern    (MITRE ATT&CK techniques)
  - Relationship      (indicator indicates attack-pattern)
  - Bundle            (wraps all objects, ready for MISP/OpenCTI)

Export formats: JSON file, clipboard string.
"""

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_IOC_EXPORT_DIR = Path("logs/stix_exports")
_IOC_EXPORT_DIR.mkdir(parents=True, exist_ok=True)

_JARVIS_IDENTITY_ID = "identity--jarvis-purple-team-v33"


def _stix_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _make_indicator(pattern: str, pattern_type: str,
                    name: str, description: str) -> dict:
    return {
        "type":            "indicator",
        "spec_version":    "2.1",
        "id":              f"indicator--{uuid.uuid4()}",
        "created":         _stix_timestamp(),
        "modified":        _stix_timestamp(),
        "name":            name,
        "description":     description,
        "pattern":         pattern,
        "pattern_type":    pattern_type,
        "valid_from":      _stix_timestamp(),
        "indicator_types": ["malicious-activity"],
        "created_by_ref":  _JARVIS_IDENTITY_ID,
    }


def _make_attack_pattern(technique_id: str, name: str) -> dict:
    return {
        "type":         "attack-pattern",
        "spec_version": "2.1",
        "id":           f"attack-pattern--{uuid.uuid4()}",
        "created":      _stix_timestamp(),
        "modified":     _stix_timestamp(),
        "name":         name,
        "external_references": [{
            "source_name": "mitre-attack",
            "external_id": technique_id,
            "url": f"https://attack.mitre.org/techniques/{technique_id.replace('.', '/')}",
        }],
        "created_by_ref": _JARVIS_IDENTITY_ID,
    }


def _make_relationship(src_id: str, tgt_id: str,
                       rel_type: str = "indicates") -> dict:
    return {
        "type":              "relationship",
        "spec_version":      "2.1",
        "id":                f"relationship--{uuid.uuid4()}",
        "created":           _stix_timestamp(),
        "modified":          _stix_timestamp(),
        "relationship_type": rel_type,
        "source_ref":        src_id,
        "target_ref":        tgt_id,
        "created_by_ref":    _JARVIS_IDENTITY_ID,
    }


_RE_IPV4   = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_RE_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")


def extract_iocs_from_incident(incident: dict) -> dict:
    """
    Extract IOCs from a compound incident dict and return
    a STIX 2.1 Bundle as a Python dict.
    """
    indicators: list[dict] = []
    attack_patterns: list[dict] = []

    for ip in incident.get("involved_hosts", []) or []:
        if ip and _RE_IPV4.match(str(ip)):
            indicators.append(_make_indicator(
                pattern=f"[ipv4-addr:value = '{ip}']",
                pattern_type="stix",
                name=f"Malicious IP: {ip}",
                description=(f"IP observed in JARVIS incident "
                             f"{incident.get('incident_id')} — "
                             f"{incident.get('kill_chain_phase')}"),
            ))

    seen_processes: set[str] = set()
    for evt in (incident.get("sub_events") or []):
        proc = evt.get("process", "")
        if proc and proc not in seen_processes and len(proc) < 100:
            seen_processes.add(proc)
            if any(c.isalpha() for c in proc):
                indicators.append(_make_indicator(
                    pattern=f"[process:name = '{proc}']",
                    pattern_type="stix",
                    name=f"Suspicious process: {proc}",
                    description=(f"Process observed in {incident.get('incident_id')} "
                                 f"during {evt.get('type', '')}"),
                ))

    for technique in (incident.get("mitre_techniques") or []):
        if re.match(r"^T\d{4}(\.\d{3})?$", str(technique)):
            attack_patterns.append(
                _make_attack_pattern(technique, f"ATT&CK {technique}")
            )

    relationships: list[dict] = []
    for ind in indicators:
        for ap in attack_patterns:
            relationships.append(_make_relationship(ind["id"], ap["id"]))

    identity = {
        "type":           "identity",
        "spec_version":   "2.1",
        "id":             _JARVIS_IDENTITY_ID,
        "created":        _stix_timestamp(),
        "modified":       _stix_timestamp(),
        "name":           "JARVIS Purple Team v33.0",
        "identity_class": "system",
    }

    all_objects = [identity] + indicators + attack_patterns + relationships

    return {
        "type":         "bundle",
        "id":           f"bundle--{uuid.uuid4()}",
        "objects":      all_objects,
        "spec_version": "2.1",
    }


async def export_incident_stix(incident: dict, broadcast_fn) -> str | None:
    """
    Extract IOCs from incident, save as STIX 2.1 JSON, broadcast event.
    Returns path to saved file or None on failure.
    """
    try:
        bundle   = extract_iocs_from_incident(incident)
        inc_id   = incident.get("incident_id", "unk")
        filename = f"stix_{inc_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = _IOC_EXPORT_DIR / filename

        filepath.write_text(
            json.dumps(bundle, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

        ioc_count = sum(1 for o in bundle["objects"] if o["type"] == "indicator")
        logger.info(
            f"STIX: exported {ioc_count} IOCs from incident "
            f"{inc_id} → {filename}"
        )

        await broadcast_fn({
            "type":        "stix_exported",
            "incident_id": inc_id,
            "filename":    filename,
            "ioc_count":   ioc_count,
            "filepath":    str(filepath),
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        })

        return str(filepath)

    except Exception as e:
        logger.debug(f"STIX: export failed: {e}")
        return None
