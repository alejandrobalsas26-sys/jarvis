"""
core/detection_engineer.py — Autonomous Detection Engineering (v43.0).

Triggered by purple_coordinator when a coverage gap is detected.
Workflow:
  1. Receive gap notification for technique T-XXXX
  2. Pull historical events for that technique from episodic memory
  3. LLM drafts a Sigma detection rule
  4. Rule quality assessment (validity, fields, specificity)
  5. Broadcast to HUD for operator approval
  6. On approval → save to core/sigma_rules/ and hot-reload
"""

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_SIGMA_DIR = Path("core/sigma_rules")
_SIGMA_DIR.mkdir(parents=True, exist_ok=True)

_in_progress: set[str] = set()

_SIGMA_SYSTEM = """You are an elite Detection Engineer at a Tier-1 SOC.
Write a high-quality Sigma rule for the given MITRE technique.
The rule must:
  1. Use realistic field names from Windows Sysmon or ETW
  2. Have low false positive rate (avoid overly generic conditions)
  3. Include at least 2 specific indicators
  4. Follow exact Sigma YAML schema

Output ONLY the raw YAML Sigma rule. No markdown fences. No commentary.

Required Sigma fields:
  title, id, status, description, references, author, date,
  logsource (product + category), detection (selection + condition),
  falsepositives, level, tags (mitre attack.tXXXX format)"""

_VALID_SYSMON_FIELDS = {
    "Image", "OriginalFileName", "CommandLine", "ParentImage",
    "TargetFilename", "SourceIp", "DestinationIp", "DestinationPort",
    "ProcessId", "GrantedAccess", "CallTrace", "Details",
    "EventType", "TargetObject", "Protocol", "Initiated",
    "User", "LogonType", "WorkstationName", "IpAddress",
}


def _validate_sigma_rule(yaml_text: str) -> tuple[bool, str, float]:
    """
    Validate Sigma rule quality.
    Returns (is_valid, reason, quality_score 0.0-1.0).
    """
    score = 0.0
    try:
        import yaml
        rule = yaml.safe_load(yaml_text)
        if not isinstance(rule, dict):
            return False, "Not a valid YAML dict", 0.0

        required = {"title", "logsource", "detection"}
        missing  = required - set(rule.keys())
        if missing:
            return False, f"Missing fields: {sorted(missing)}", 0.0
        score += 0.3

        detection = rule.get("detection", {})
        if not isinstance(detection, dict) or "condition" not in detection:
            return False, "Missing condition in detection", 0.1
        score += 0.2

        title = str(rule.get("title", ""))
        if len(title) > 10:
            score += 0.1
        if rule.get("description"):
            score += 0.1
        if rule.get("tags"):
            score += 0.1
        if rule.get("falsepositives"):
            score += 0.1

        detection_str = str(detection)
        valid_fields  = sum(
            1 for f in _VALID_SYSMON_FIELDS if f in detection_str
        )
        if valid_fields >= 2:
            score += 0.1

        level = str(rule.get("level", ""))
        if level in ("high", "critical", "medium"):
            score += 0.1

        return True, "ok", round(score, 2)

    except Exception as e:
        return False, f"YAML parse error: {e}", 0.0


async def draft_rule_for_gap(
    technique_id: str,
    broadcast_fn,
    ollama_client,
    model: str,
) -> str | None:
    """
    Draft a Sigma detection rule for an undetected technique.
    Called automatically by purple_coordinator on coverage gap.
    """
    if technique_id in _in_progress:
        return None
    _in_progress.add(technique_id)

    try:
        logger.info(
            f"DETECTION_ENG: drafting Sigma rule for gap: {technique_id}"
        )

        historical = ""
        try:
            from core.knowledge import get_vault
            vault   = get_vault()
            results = vault.search(
                f"MITRE {technique_id} attack indicator",
                top_k=3,
            )
            historical = "\n".join(
                str(r.get("content", ""))[:200] for r in results
            )
        except Exception:
            pass

        prompt = (
            f"MITRE Technique: {technique_id}\n"
            f"ATT&CK URL: https://attack.mitre.org/techniques/"
            f"{technique_id.replace('.', '/')}\n\n"
            + (f"Historical JARVIS observations:\n{historical}\n\n"
               if historical else "")
            + "Write a Sigma detection rule for this technique:"
        )

        response = await asyncio.wait_for(
            ollama_client.chat.completions.create(
                model    = model,
                messages = [
                    {"role": "system", "content": _SIGMA_SYSTEM},
                    {"role": "user",   "content": prompt},
                ],
                stream     = False,
                extra_body = {"options": {
                    "num_ctx":     2048,
                    "temperature": 0.1,
                }},
            ),
            timeout=45.0,
        )

        rule_text = response.choices[0].message.content.strip()
        rule_text = re.sub(r'^```ya?ml\s*', '', rule_text,
                           flags=re.IGNORECASE)
        rule_text = re.sub(r'\s*```$', '', rule_text).strip()

        is_valid, reason, score = _validate_sigma_rule(rule_text)

        logger.info(
            f"DETECTION_ENG: rule drafted for {technique_id} — "
            f"valid={is_valid} score={score} reason={reason}"
        )

        draft_path = (
            _SIGMA_DIR /
            f"DRAFT_{technique_id.replace('.', '_')}.yaml"
        )
        draft_path.write_text(rule_text, encoding="utf-8")

        try:
            await broadcast_fn({
                "type":          "sigma_rule_drafted",
                "technique":     technique_id,
                "is_valid":      is_valid,
                "quality_score": score,
                "validation":    reason,
                "draft_path":    str(draft_path),
                "preview":       rule_text[:400],
                "severity":      "HIGH" if is_valid else "WARNING",
                "timestamp":     datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass

        return str(draft_path) if is_valid else None

    except asyncio.TimeoutError:
        logger.warning(f"DETECTION_ENG: LLM timeout for {technique_id}")
        return None
    except Exception as e:
        logger.debug(f"DETECTION_ENG: {e}")
        return None
    finally:
        _in_progress.discard(technique_id)


async def deploy_approved_rule(
    draft_path: str,
    broadcast_fn,
) -> bool:
    """
    Deploy an operator-approved draft rule to the active Sigma ruleset.
    Renames DRAFT_ prefix and hot-reloads the Sigma engine.
    """
    draft = Path(draft_path)
    if not draft.exists():
        return False

    final_name = draft.name.replace("DRAFT_", "")
    final_path = _SIGMA_DIR / final_name
    draft.rename(final_path)

    logger.info(f"DETECTION_ENG: rule deployed → {final_name}")

    try:
        from core.sigma_generator import reload_rules
        await reload_rules()
    except Exception:
        pass

    try:
        await broadcast_fn({
            "type":      "sigma_rule_deployed",
            "rule":      final_name,
            "path":      str(final_path),
            "severity":  "INFO",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass
    return True
