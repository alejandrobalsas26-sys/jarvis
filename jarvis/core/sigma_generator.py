"""
core/sigma_generator.py — LLM-powered Sigma rule auto-generation (v33.0).

Takes a compound incident or raw telemetry event, constructs a detailed
prompt, and uses the local Ollama model to generate a valid Sigma rule YAML.
Generated rules are saved to core/sigma_rules/ with a UUID filename.
Existing Sigma tooling (sigma-cli) can convert to SIEM query languages.

Output format: Sigma v1.0 YAML
Offline: uses local Ollama — no internet required.
"""

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

SIGMA_RULES_DIR = Path(__file__).parent / "sigma_rules"
SIGMA_RULES_DIR.mkdir(exist_ok=True)

_SIGMA_SYSTEM_PROMPT = """You are a Sigma rule expert. Generate a valid Sigma detection rule YAML.
Rules MUST follow Sigma v1 specification exactly.
Output ONLY the raw YAML — no markdown fences, no explanation, no preamble.
Required fields: title, id, status, description, references, logsource, detection, condition, level, tags."""

_SIGMA_FEW_SHOT = """
Example Sigma rule for LSASS access:
title: Suspicious LSASS Access
id: a3e1b4c2-1234-5678-abcd-ef0123456789
status: experimental
description: Detects unauthorized LSASS memory access consistent with credential dumping
logsource:
    product: windows
    category: process_access
detection:
    selection:
        TargetImage|endswith: '\\lsass.exe'
        GrantedAccess|contains:
            - '0x1010'
            - '0x1410'
    condition: selection
level: high
tags:
    - attack.credential_access
    - attack.t1003.001
"""


async def generate_sigma_rule(
    incident: dict,
    broadcast_fn,
    model: str | None = None,
) -> str | None:
    """
    Generate a Sigma rule from a compound incident or raw event.
    Returns the YAML string or None on failure.
    """
    inc_summary = json.dumps({
        "incident_id":      incident.get("incident_id", ""),
        "kill_chain_phase": incident.get("kill_chain_phase", ""),
        "mitre_techniques": incident.get("mitre_techniques", []),
        "sub_events": [
            {k: v for k, v in e.items()
             if k in ("type", "process", "pid", "event_id",
                      "attacker_ip", "technique")}
            for e in (incident.get("sub_events") or [])[:5]
        ],
        "severity": incident.get("severity_score", 0),
    }, indent=2, default=str)

    prompt = f"""{_SIGMA_SYSTEM_PROMPT}

{_SIGMA_FEW_SHOT}

Now generate a Sigma rule for this incident:
{inc_summary}

Generate a precise Sigma rule targeting the most specific indicators.
Use appropriate logsource (windows/sysmon/process_creation/process_access/network_connection).
Map all techniques to correct ATT&CK tags (attack.tXXXX format).
"""

    try:
        from openai import AsyncOpenAI

        ollama_client = AsyncOpenAI(
            base_url="http://localhost:11434/v1",
            api_key="ollama",
        )

        if not model:
            try:
                from core.hardware_profile import get_cached_profile
                hw = get_cached_profile()
                model = hw.model_deep if hw else "qwen2.5:14b-instruct-q4_K_M"
            except Exception:
                model = "qwen2.5:14b-instruct-q4_K_M"

        response = await asyncio.wait_for(
            ollama_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                stream=False,
                extra_body={"options": {"num_ctx": 2048, "temperature": 0.1}},
            ),
            timeout=45.0,
        )

        yaml_text = (response.choices[0].message.content or "").strip()
        yaml_text = re.sub(r'^```ya?ml\s*', '', yaml_text, flags=re.IGNORECASE)
        yaml_text = re.sub(r'\s*```$', '', yaml_text)
        yaml_text = yaml_text.strip()

        if "title:" not in yaml_text or "detection:" not in yaml_text:
            logger.warning("SIGMA_GEN: generated text doesn't look like valid Sigma YAML")
            return None

        rule_id = str(uuid.uuid4())
        filename = f"auto_{incident.get('incident_id', 'unk')}_{rule_id[:8]}.yaml"
        rule_path = SIGMA_RULES_DIR / filename
        rule_path.write_text(yaml_text, encoding="utf-8")

        logger.info(f"SIGMA_GEN: rule generated → {filename}")

        await broadcast_fn({
            "type":         "sigma_rule_generated",
            "filename":     filename,
            "incident_id":  incident.get("incident_id", ""),
            "rule_preview": yaml_text[:300],
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        })

        return yaml_text

    except asyncio.TimeoutError:
        logger.warning("SIGMA_GEN: LLM timeout — rule not generated")
        return None
    except Exception as e:
        logger.debug(f"SIGMA_GEN: {e}")
        return None


def list_generated_rules() -> list[dict]:
    """List all auto-generated Sigma rules with metadata."""
    rules: list[dict] = []
    for path in sorted(SIGMA_RULES_DIR.glob("auto_*.yaml")):
        try:
            text = path.read_text(encoding="utf-8")
            title = next(
                (l.replace("title:", "").strip()
                 for l in text.splitlines() if l.startswith("title:")),
                path.stem,
            )
            rules.append({
                "filename":   path.name,
                "title":      title,
                "size_bytes": path.stat().st_size,
                "created":    datetime.fromtimestamp(
                    path.stat().st_ctime, timezone.utc
                ).isoformat(),
            })
        except Exception:
            continue
    return rules
