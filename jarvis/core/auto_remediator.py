"""
core/auto_remediator.py — Self-healing mitigation drafter (v39.0).

Generates PowerShell remediation scripts for detected threats.
HUMAN-IN-THE-LOOP: scripts are drafted, saved, and displayed in the HUD.
Execution requires explicit operator approval via NATO OTP challenge.

Draft → Save → HUD display → Operator reviews → NATO OTP → Execute
"""

import asyncio, re
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger

_MITIGATION_DIR = Path("logs/mitigations")
_MITIGATION_DIR.mkdir(parents=True, exist_ok=True)

_SYSTEM_PROMPT = """You are an elite Windows security engineer.
Generate a PowerShell mitigation script for the provided security alert.
Rules:
  - Use only native PowerShell cmdlets (New-NetFirewallRule, Set-Service,
    Stop-Process, Set-MpPreference, netsh, etc.)
  - Add # JARVIS-MITIGATION comment at the top with a one-line summary
  - Add Write-Host status messages so the operator sees progress
  - Script must be idempotent (safe to run twice)
  - Output ONLY raw PowerShell code — no markdown fences, no explanations
  - If the alert does not warrant a script, output: # NO_ACTION_REQUIRED"""

_DANGEROUS_PS_PATTERNS = [
    r"Remove-Item\s+-Recurse\s+-Force\s+[Cc]:\\",
    r"Format-Volume",
    r"Clear-Disk",
    r"(curl|wget|Invoke-WebRequest).*http",
    r"Invoke-Expression",
    r"\$env:COMPUTERNAME.*\|.*Remove",
]


def _validate_powershell(script: str) -> tuple[bool, str]:
    """
    Basic safety validation of generated PowerShell.
    Returns (is_safe, reason).
    """
    for pattern in _DANGEROUS_PS_PATTERNS:
        if re.search(pattern, script, re.IGNORECASE):
            return False, f"Dangerous pattern detected: {pattern}"
    if len(script.strip()) < 20:
        return False, "Script too short — likely empty response"
    return True, "ok"


async def draft_mitigation(
    alert_data: dict,
    ollama_client,
    model: str,
    broadcast_fn,
    incident_id: str = "unknown",
) -> str | None:
    """
    Draft a PowerShell mitigation script for a security alert.
    alert_data is sanitized before LLM injection.
    Returns path to saved .ps1 file, or None on failure.
    """
    # Sanitize alert data before LLM prompt injection
    from core.feed_sanitizer import sanitize_for_llm
    safe_alert = sanitize_for_llm(str(alert_data)[:800], source="auto_remediator")

    prompt = (
        f"SECURITY ALERT REQUIRING MITIGATION:\n{safe_alert}\n\n"
        "Generate the PowerShell mitigation script:"
    )

    try:
        response = await asyncio.wait_for(
            ollama_client.chat.completions.create(
                model    = model,
                messages = [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": prompt},
                ],
                stream = False,
                extra_body = {"options": {
                    "num_ctx": 1024, "temperature": 0.1
                }},
            ),
            timeout=30.0,
        )

        ps_code = response.choices[0].message.content.strip()
        # Strip markdown fences if model added them
        ps_code = re.sub(r'^```powershell\s*', '', ps_code, flags=re.IGNORECASE)
        ps_code = re.sub(r'\s*```$', '', ps_code).strip()

        # Check for no-action response
        if ps_code.strip().startswith("# NO_ACTION_REQUIRED"):
            logger.info("REMEDIATOR: LLM determined no action required")
            return None

        # Safety validation before saving
        is_safe, reason = _validate_powershell(ps_code)
        if not is_safe:
            logger.error(f"REMEDIATOR: unsafe script rejected — {reason}")
            await broadcast_fn({
                "type":    "mitigation_rejected",
                "reason":  reason,
                "severity": "HIGH",
            })
            return None

        # Save script
        ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
        script_name = f"mitigate_{incident_id}_{ts}.ps1"
        script_path = _MITIGATION_DIR / script_name
        script_path.write_text(ps_code, encoding="utf-8")

        logger.info(f"REMEDIATOR: draft saved → {script_name}")

        # Broadcast for HUD approval UI
        await broadcast_fn({
            "type":        "mitigation_drafted",
            "incident_id": incident_id,
            "script_path": str(script_path),
            "script_name": script_name,
            "preview":     ps_code[:400],
            "line_count":  ps_code.count("\n") + 1,
            "severity":    "HIGH",
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        })

        return str(script_path)

    except asyncio.TimeoutError:
        logger.warning("REMEDIATOR: LLM timeout — no script generated")
        return None
    except Exception as e:
        logger.debug(f"REMEDIATOR: draft error: {e}")
        return None


async def execute_mitigation(
    script_path: str,
    broadcast_fn,
    tool_executor,
) -> bool:
    """
    Execute an approved mitigation script.
    Requires NATO OTP via ToolExecutor._challenge() — never auto-executes.
    """
    import subprocess

    path = Path(script_path)
    if not path.exists():
        logger.error(f"REMEDIATOR: script not found: {script_path}")
        return False

    # NATO OTP gate — mandatory
    auth_ok, auth_word = await tool_executor._challenge(
        tool_name = "execute_mitigation",
        preview   = f"Execute: {path.name}",
    )
    if not auth_ok:
        logger.warning(f"REMEDIATOR: execution blocked — OTP failed: {auth_word}")
        await broadcast_fn({
            "type":    "mitigation_blocked",
            "reason":  f"OTP denied: {auth_word}",
            "script":  path.name,
            "severity": "WARNING",
        })
        return False

    logger.warning(f"REMEDIATOR: executing approved mitigation: {path.name}")

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(path)],
            capture_output=True, text=True, timeout=60,
        )

        success = result.returncode == 0
        await broadcast_fn({
            "type":      "mitigation_executed",
            "script":    path.name,
            "success":   success,
            "output":    result.stdout[:300],
            "errors":    result.stderr[:200],
            "severity":  "HIGH" if not success else "INFO",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        if success:
            logger.info("REMEDIATOR: mitigation executed successfully")
        else:
            logger.error(
                f"REMEDIATOR: mitigation failed — "
                f"{result.stderr[:100]}"
            )

        return success

    except subprocess.TimeoutExpired:
        logger.error("REMEDIATOR: execution timeout")
        return False
    except Exception as e:
        logger.debug(f"REMEDIATOR: execution error: {e}")
        return False
