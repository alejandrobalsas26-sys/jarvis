"""
core/agent_orchestrator.py — Sequential multi-agent orchestrator (v36.0).

Spawns expert sub-agents for complex Purple Team analysis.
Sequential execution — one agent at a time (U-Series CPU constraint).

Available agents:
  MalwareAnalyst    → binary analysis, YARA, entropy, string extraction
  NetworkRecon      → nmap, service enumeration, topology mapping
  ThreatIntelligence→ IOC correlation, threat actor attribution, feed lookup
  IncidentResponder → triage, containment, remediation recommendations
  CodeAnalyst       → script/shellcode analysis, deobfuscation, translation

Usage:
  result = await orchestrator.run_task(
      task_description="Analyze this suspicious binary",
      agents=["MalwareAnalyst", "ThreatIntelligence"],
      context={"file": "malware.exe", "incident_id": "INC-A3F1"},
  )
"""

import asyncio
import time
from datetime import datetime, timezone
from loguru import logger


_AGENT_PROMPTS: dict[str, str] = {
    "MalwareAnalyst": """You are an elite malware analyst.
Analyze the provided binary/script indicators with surgical precision.
Focus on: behavioral patterns, evasion techniques, persistence mechanisms,
C2 indicators, YARA rule candidates. Respond in structured format.
Use MITRE ATT&CK technique IDs. Be technically precise, no fluff.""",

    "NetworkRecon": """You are an expert network reconnaissance specialist.
Analyze network indicators, port/service data, and topology information.
Focus on: attack surface, unusual services, potential pivot points,
lateral movement paths, network segmentation weaknesses.
Respond with specific actionable intelligence.""",

    "ThreatIntelligence": """You are a senior threat intelligence analyst.
Correlate provided IOCs against known threat actors and campaigns.
Focus on: threat actor TTPs, campaign attribution, historical context,
predicted next moves based on actor playbook. Reference specific APT groups
when confidence is high. Distinguish confirmed from inferred attribution.""",

    "IncidentResponder": """You are a Purple Team incident response lead.
Provide immediate, prioritized response recommendations.
Focus on: containment actions (ordered by urgency), evidence preservation,
attacker eviction sequence, recovery steps, lessons learned.
Format: numbered priority list. Be decisive, no hedging.""",

    "CodeAnalyst": """You are an expert reverse engineer and code analyst.
Analyze scripts, shellcode, and obfuscated code.
Focus on: deobfuscation, functionality identification, IOC extraction,
dangerous API calls, network indicators, persistence mechanisms.
Provide clean pseudocode where applicable.""",
}


class SubAgent:
    """A specialized expert agent with its own context window."""

    def __init__(self, name: str, system_prompt: str) -> None:
        self.name          = name
        self.system_prompt = system_prompt
        self.history:      list[dict] = []

    async def analyze(
        self,
        task: str,
        context: dict,
        model: str,
        ollama_client,
    ) -> str:
        """Run a single analysis task. Returns expert assessment string."""
        context_str = "\n".join(
            f"{k}: {v}" for k, v in (context or {}).items()
            if v is not None and str(v).strip()
        )
        user_msg = (
            f"TASK: {task}\n\n"
            f"CONTEXT:\n{context_str}\n\n"
            "Provide your expert analysis:"
        )

        try:
            response = await asyncio.wait_for(
                ollama_client.chat.completions.create(
                    model    = model,
                    messages = [
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user",   "content": user_msg},
                    ],
                    stream     = False,
                    extra_body = {"options": {
                        "num_ctx":     2048,
                        "temperature": 0.2,
                    }},
                ),
                timeout=60.0,
            )
            return response.choices[0].message.content.strip()
        except asyncio.TimeoutError:
            return f"[{self.name}: timeout — analysis incomplete]"
        except Exception as e:
            return f"[{self.name}: error — {e}]"


class AgentOrchestrator:
    def __init__(self) -> None:
        self._broadcast_fn  = None
        self._ollama_client = None
        self._fast_model    = "qwen2.5:7b-instruct-q5_K_M"
        self._deep_model    = "qwen2.5:14b-instruct-q4_K_M"
        self._running       = False

    def attach(
        self,
        broadcast_fn,
        ollama_client,
        fast_model: str,
        deep_model: str,
    ) -> None:
        self._broadcast_fn  = broadcast_fn
        self._ollama_client = ollama_client
        self._fast_model    = fast_model
        self._deep_model    = deep_model

    async def run_task(
        self,
        task_description: str,
        agents: list[str],
        context: dict | None = None,
        synthesize: bool = True,
    ) -> dict:
        """
        Run sequential multi-agent analysis.
        Each agent analyzes the task independently.
        Optionally synthesize results with the deep model.
        """
        if self._running:
            return {"error": "Orchestrator already running a task"}
        if self._ollama_client is None or self._broadcast_fn is None:
            return {"error": "Orchestrator not attached"}

        context = context or {}
        self._running = True
        start_ts = time.monotonic()
        results: dict[str, str] = {}

        try:
            try:
                await self._broadcast_fn({
                    "type":      "agent_task_started",
                    "task":      task_description[:100],
                    "agents":    agents,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            except Exception:
                pass
            logger.info(
                f"ORCHESTRATOR: starting task with agents {agents}"
            )

            for agent_name in agents:
                prompt = _AGENT_PROMPTS.get(agent_name)
                if not prompt:
                    logger.warning(f"ORCHESTRATOR: unknown agent '{agent_name}'")
                    continue

                agent = SubAgent(agent_name, prompt)
                logger.info(f"ORCHESTRATOR: → {agent_name} analyzing…")

                try:
                    await self._broadcast_fn({
                        "type":      "agent_running",
                        "agent":     agent_name,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                except Exception:
                    pass

                analysis = await agent.analyze(
                    task_description, context,
                    self._fast_model, self._ollama_client,
                )
                results[agent_name] = analysis
                logger.info(
                    f"ORCHESTRATOR: {agent_name} complete "
                    f"({len(analysis)} chars)"
                )

                try:
                    await self._broadcast_fn({
                        "type":      "agent_complete",
                        "agent":     agent_name,
                        "preview":   analysis[:200],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                except Exception:
                    pass

            synthesis = ""
            if synthesize and len(results) > 1:
                synthesis = await self._synthesize(
                    task_description, results
                )

            elapsed = round(time.monotonic() - start_ts, 1)
            output  = {
                "task":      task_description,
                "agents":    list(results.keys()),
                "results":   results,
                "synthesis": synthesis,
                "elapsed_s": elapsed,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            try:
                await self._broadcast_fn({
                    "type":              "agent_task_complete",
                    "task":              task_description[:80],
                    "agents":            list(results.keys()),
                    "elapsed_s":         elapsed,
                    "synthesis_preview": synthesis[:300] if synthesis else "",
                    "timestamp":         datetime.now(timezone.utc).isoformat(),
                })
            except Exception:
                pass

            return output

        finally:
            self._running = False

    async def _synthesize(
        self,
        task: str,
        agent_results: dict[str, str],
    ) -> str:
        """Use deep model to synthesize all agent assessments."""
        combined = "\n\n".join(
            f"=== {agent} ASSESSMENT ===\n{result}"
            for agent, result in agent_results.items()
        )
        synthesis_prompt = (
            f"ORIGINAL TASK: {task}\n\n"
            f"EXPERT ASSESSMENTS:\n{combined}\n\n"
            "Synthesize these assessments into a unified, prioritized "
            "intelligence product. Resolve conflicts. Identify highest "
            "confidence findings. Provide actionable conclusions."
        )
        try:
            response = await asyncio.wait_for(
                self._ollama_client.chat.completions.create(
                    model    = self._deep_model,
                    messages = [
                        {"role": "user", "content": synthesis_prompt}
                    ],
                    stream     = False,
                    extra_body = {"options": {
                        "num_ctx":     4096,
                        "temperature": 0.1,
                    }},
                ),
                timeout=90.0,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.debug(f"ORCHESTRATOR: synthesis error: {e}")
            return ""


# Module singleton
orchestrator = AgentOrchestrator()
