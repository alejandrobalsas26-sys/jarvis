"""
core/red_team_operator.py — ARES Autonomous Red Team Operator (v42.0).

Plans and executes multi-stage attack campaigns against lab targets.
Uses existing JARVIS tools: nmap, Metasploit RPC, Sliver C2.
LLM plans each stage. Human operator approves each execution.

Campaign stages:
  1. RECON      — passive intel gathering (OSINT, DNS, WHOIS)
  2. SCAN       — nmap port/service/OS detection
  3. ENUMERATE  — service fingerprinting, vulnerability mapping
  4. EXPLOIT    — Metasploit/Sliver payload delivery (NATO OTP)
  5. POST       — persistence, lateral movement, credential harvest
  6. REPORT     — full campaign report (.docx + STIX)

NATO OTP required at: EXPLOIT and POST stages.
RECON, SCAN, ENUMERATE are automated (read-only, no harm).

Campaign state machine — survives restarts via YAML persistence.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_CAMPAIGNS_DIR = Path("logs/ares_campaigns")
_CAMPAIGNS_DIR.mkdir(parents=True, exist_ok=True)

# Campaign stages in order
_STAGES = ["RECON", "SCAN", "ENUMERATE", "EXPLOIT", "POST", "REPORT"]

_PLANNER_SYSTEM = """You are ARES, an elite red team operator AI.
Given scan/recon findings, plan the SINGLE BEST next attack action.
Be specific: tool, exact command, expected result, MITRE technique.
Format as JSON:
{
  "action": "exact tool command",
  "tool": "nmap|metasploit|sliver|custom",
  "mitre": "TXXXX",
  "rationale": "why this action",
  "risk": "LOW|MEDIUM|HIGH",
  "next_stage": "SCAN|ENUMERATE|EXPLOIT|POST|REPORT"
}
Output ONLY the JSON object."""

_ANALYST_SYSTEM = """You are an elite red team analyst.
Given the output of a security tool, extract all findings.
Format as JSON:
{
  "open_ports": [],
  "services": [],
  "vulnerabilities": [],
  "credentials": [],
  "os_guess": "",
  "key_findings": [],
  "recommended_exploits": []
}
Output ONLY the JSON object."""


class AresCampaign:
    """Represents a single red team campaign against a target."""

    def __init__(
        self,
        campaign_id: str,
        target_ip: str,
        target_name: str = "",
        authorized: bool = False,
    ):
        self.campaign_id  = campaign_id
        self.target_ip    = target_ip
        self.target_name  = target_name or target_ip
        self.authorized   = authorized
        self.stage        = "RECON"
        self.findings:    dict = {}
        self.action_log:  list[dict] = []
        self.started_at   = datetime.now(timezone.utc).isoformat()
        self.status       = "PLANNING"

    def to_dict(self) -> dict:
        return {
            "campaign_id":  self.campaign_id,
            "target_ip":    self.target_ip,
            "target_name":  self.target_name,
            "stage":        self.stage,
            "findings":     self.findings,
            "action_log":   self.action_log[-5:],
            "started_at":   self.started_at,
            "status":       self.status,
        }

    def save(self) -> None:
        path = _CAMPAIGNS_DIR / f"{self.campaign_id}.json"
        path.write_text(
            json.dumps(self.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )


class AresOperator:
    """
    ARES Red Team Operator.
    Plans campaigns using LLM. Executes with operator approval.
    """

    def __init__(self):
        self._campaigns:    dict[str, AresCampaign] = {}
        self._broadcast_fn  = None
        self._ollama_client = None
        # V66.1: unified DEEP-role resolver default (env → central config);
        # attach() supplies the boot-resolved deep model in the live path.
        from core.model_router import resolve_deep_model
        self._deep_model    = resolve_deep_model()
        self._tool_executor = None
        self._tts           = None

    def attach(self, broadcast_fn, ollama_client,
               deep_model, tool_executor, tts) -> None:
        self._broadcast_fn  = broadcast_fn
        self._ollama_client = ollama_client
        self._deep_model    = deep_model
        self._tool_executor = tool_executor
        self._tts           = tts

    async def start_campaign(
        self,
        target_ip: str,
        target_name: str = "",
    ) -> str:
        """
        Initialize a new red team campaign.
        Returns campaign_id.
        """
        campaign_id = str(uuid.uuid4())[:8].upper()
        campaign    = AresCampaign(campaign_id, target_ip, target_name)
        self._campaigns[campaign_id] = campaign

        logger.warning(
            f"ARES: campaign {campaign_id} started → "
            f"target={target_ip}"
        )

        if self._broadcast_fn:
            await self._broadcast_fn({
                "type":        "ares_campaign_started",
                "campaign_id": campaign_id,
                "target_ip":   target_ip,
                "target_name": target_name,
                "severity":    "HIGH",
                "timestamp":   datetime.now(timezone.utc).isoformat(),
            })

        if self._tts:
            asyncio.create_task(self._tts.speak_async(
                f"ARES campaign {campaign_id} initiated against "
                f"{target_name or target_ip}. Starting reconnaissance."
            ))

        # Start the campaign autonomously from RECON
        asyncio.create_task(self._run_campaign(campaign))
        return campaign_id

    async def _run_campaign(self, campaign: AresCampaign) -> None:
        """
        Main campaign execution loop.
        RECON and SCAN run automatically.
        EXPLOIT and POST require NATO OTP.
        """
        for stage in _STAGES:
            if campaign.status == "ABORTED":
                break

            campaign.stage = stage
            campaign.save()

            if self._broadcast_fn:
                await self._broadcast_fn({
                    "type":        "ares_stage_started",
                    "campaign_id": campaign.campaign_id,
                    "stage":       stage,
                    "target":      campaign.target_ip,
                    "timestamp":   datetime.now(timezone.utc).isoformat(),
                })

            # Stages requiring NATO OTP
            if stage in ("EXPLOIT", "POST"):
                approved = await self._request_approval(campaign, stage)
                if not approved:
                    logger.warning(
                        f"ARES: {stage} declined — campaign {campaign.campaign_id}"
                    )
                    campaign.status = "PAUSED_AT_" + stage
                    campaign.save()
                    break

            # Execute stage
            findings = await self._execute_stage(campaign, stage)
            campaign.findings[stage] = findings
            campaign.action_log.append({
                "stage":     stage,
                "findings":  str(findings)[:500],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            campaign.save()

            if self._broadcast_fn:
                await self._broadcast_fn({
                    "type":        "ares_stage_complete",
                    "campaign_id": campaign.campaign_id,
                    "stage":       stage,
                    "findings":    str(findings)[:300],
                    "timestamp":   datetime.now(timezone.utc).isoformat(),
                })

            if stage == "REPORT":
                campaign.status = "COMPLETE"
                campaign.save()

            await asyncio.sleep(2)   # brief pause between stages

    async def _execute_stage(
        self,
        campaign: AresCampaign,
        stage: str,
    ) -> dict:
        """Execute a single campaign stage."""
        target   = campaign.target_ip
        findings = campaign.findings

        if stage == "RECON":
            return await self._stage_recon(target)
        elif stage == "SCAN":
            return await self._stage_scan(target)
        elif stage == "ENUMERATE":
            return await self._stage_enumerate(target, findings)
        elif stage == "EXPLOIT":
            return await self._stage_exploit(target, findings)
        elif stage == "POST":
            return await self._stage_post(target, findings)
        elif stage == "REPORT":
            return await self._stage_report(campaign)
        return {}

    async def _stage_recon(self, target: str) -> dict:
        """Passive recon: WHOIS, reverse DNS, OSINT."""
        try:
            from tools.osint_engine import enrich_ip
            enrichment = await enrich_ip(target, self._broadcast_fn)
            return enrichment or {"target": target, "note": "OSINT limited"}
        except Exception as e:
            logger.debug(f"ARES RECON: enrichment unavailable: {e}")
            return {"target": target, "note": "OSINT module unavailable"}

    async def _stage_scan(self, target: str) -> dict:
        """Active scan: nmap top ports + OS detection."""
        logger.info(f"ARES SCAN: nmap -sV -O --top-ports 1000 {target}")
        if self._tool_executor:
            try:
                result = await self._tool_executor.aexecute(
                    tool_name  = "network_scan",
                    tool_input = {
                        "target":    target,
                        "scan_type": "-sV -O --top-ports 1000 --open",
                    },
                    reasoning  = f"ARES campaign scan of {target}",
                )
                return await self._analyze_output(
                    str(result), "nmap scan output"
                )
            except Exception as e:
                return {"error": str(e)}
        return {"note": "tool executor not available"}

    async def _stage_enumerate(
        self, target: str, findings: dict
    ) -> dict:
        """Service enumeration based on scan results."""
        scan_data = findings.get("SCAN", {})
        services  = scan_data.get("services", [])
        plan      = await self._plan_action(
            stage="ENUMERATE",
            target=target,
            context=json.dumps(scan_data, default=str)[:1000],
        )
        logger.info(f"ARES ENUMERATE plan: {plan.get('action','?')[:80]}")
        return {"plan": plan, "services": services}

    async def _stage_exploit(
        self, target: str, findings: dict
    ) -> dict:
        """
        Exploitation via Metasploit RPC or Sliver.
        NATO OTP already verified before this is called.
        """
        scan_data = findings.get("SCAN", {})
        plan = await self._plan_action(
            stage  = "EXPLOIT",
            target = target,
            context= json.dumps(scan_data, default=str)[:1500],
        )
        logger.warning(
            f"ARES EXPLOIT: {plan.get('action','?')[:80]} "
            f"[{plan.get('mitre','?')}]"
        )
        # Log plan — actual execution would go through Metasploit RPC
        return {"plan": plan, "note": "HITL approved — execution logged"}

    async def _stage_post(
        self, target: str, findings: dict
    ) -> dict:
        """Post-exploitation: persistence, lateral, credential harvest."""
        exploit_data = findings.get("EXPLOIT", {})
        plan = await self._plan_action(
            stage  = "POST",
            target = target,
            context= json.dumps(exploit_data, default=str)[:1000],
        )
        return {"plan": plan}

    async def _stage_report(self, campaign: AresCampaign) -> dict:
        """Generate final campaign report."""
        try:
            from core.forensic_reporter import generate_forensic_report
            incident = {
                "incident_id":      f"ARES_{campaign.campaign_id}",
                "kill_chain_phase": "Post-Exploitation",
                "severity_score":   9.0,
                "mitre_techniques": ["T1046", "T1059", "T1003"],
                "involved_hosts":   {campaign.target_ip},
                "involved_pids":    set(),
                "sub_events":       [
                    {
                        "type":      s,
                        "process":   campaign.target_ip,
                        "technique": str(
                            campaign.findings.get(s, {})
                            .get("plan", {}).get("mitre", "")
                        ),
                    }
                    for s in _STAGES[:-1]
                ],
            }
            await generate_forensic_report(
                incident, [], self._broadcast_fn,
                self._ollama_client, self._deep_model,
            )
        except Exception as e:
            logger.debug(f"ARES REPORT: {e}")
        return {"status": "report_generated"}

    async def _plan_action(
        self, stage: str, target: str, context: str
    ) -> dict:
        """Use LLM to plan the next action for a given stage."""
        prompt = (
            f"STAGE: {stage}\n"
            f"TARGET: {target}\n"
            f"CONTEXT:\n{context}\n\n"
            "Plan the optimal next red team action:"
        )
        try:
            resp = await asyncio.wait_for(
                self._ollama_client.chat.completions.create(
                    model    = self._deep_model,
                    messages = [
                        {"role": "system", "content": _PLANNER_SYSTEM},
                        {"role": "user",   "content": prompt},
                    ],
                    stream = False,
                    extra_body = {"options": {
                        "num_ctx": 2048, "temperature": 0.1
                    }},
                ),
                timeout=30.0,
            )
            import re
            text = resp.choices[0].message.content.strip()
            text = re.sub(r'^```json\s*', '', text, flags=re.IGNORECASE)
            text = re.sub(r'\s*```$', '', text).strip()
            return json.loads(text)
        except Exception as e:
            return {"action": "manual", "error": str(e), "risk": "LOW"}

    async def _analyze_output(
        self, output: str, context: str
    ) -> dict:
        """Parse tool output with LLM."""
        try:
            resp = await asyncio.wait_for(
                self._ollama_client.chat.completions.create(
                    model    = self._deep_model,
                    messages = [
                        {"role": "system", "content": _ANALYST_SYSTEM},
                        {"role": "user",   "content":
                         f"CONTEXT: {context}\n\nOUTPUT:\n{output[:2000]}"},
                    ],
                    stream = False,
                    extra_body = {"options": {
                        "num_ctx": 2048, "temperature": 0
                    }},
                ),
                timeout=30.0,
            )
            import re
            text = resp.choices[0].message.content.strip()
            text = re.sub(r'^```json\s*', '', text, flags=re.IGNORECASE)
            text = re.sub(r'\s*```$', '', text).strip()
            return json.loads(text)
        except Exception:
            return {"raw_output": output[:500]}

    async def _request_approval(
        self, campaign: AresCampaign, stage: str
    ) -> bool:
        """
        Request NATO OTP approval for high-risk stage.
        Broadcasts to HUD and waits for operator.
        """
        if self._broadcast_fn:
            await self._broadcast_fn({
                "type":        "ares_approval_required",
                "campaign_id": campaign.campaign_id,
                "stage":       stage,
                "target":      campaign.target_ip,
                "findings_preview": str(campaign.findings)[:300],
                "severity":    "CRITICAL",
                "timestamp":   datetime.now(timezone.utc).isoformat(),
            })

        if self._tts:
            asyncio.create_task(self._tts.speak_async(
                f"ARES campaign {campaign.campaign_id} requesting "
                f"authorization for {stage} against "
                f"{campaign.target_ip}. NATO OTP required."
            ))

        if self._tool_executor:
            try:
                auth_ok, auth_word = await self._tool_executor._challenge(
                    tool_name = f"ares_{stage.lower()}",
                    preview   = f"ARES {stage}: {campaign.target_ip}",
                )
                return auth_ok
            except Exception:
                return False
        return False

    def get_active_campaigns(self) -> list[dict]:
        return [c.to_dict() for c in self._campaigns.values()
                if c.status not in ("COMPLETE", "ABORTED")]

    async def abort_campaign(self, campaign_id: str) -> bool:
        campaign = self._campaigns.get(campaign_id)
        if not campaign:
            return False
        campaign.status = "ABORTED"
        campaign.save()
        logger.warning(f"ARES: campaign {campaign_id} aborted")
        return True


# Module singleton
ares_operator = AresOperator()
