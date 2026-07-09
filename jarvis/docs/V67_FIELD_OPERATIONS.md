# JARVIS V67 — Field Operations & Operator Onboarding

This is the practical operator workflow for the V67 *Field Intelligence* release. Every
command here is read-only or dry-run unless it explicitly passes through the guarded
control plane (ToolExecutor + Authority + Scope + Risk + HITL + Audit). External
telemetry is always treated as data — it can never expand scope, authorize a tool, or
execute an action.

Run everything from the `jarvis/` directory.

---

## 1. Check field readiness first

```
python scripts/field_readiness.py
```

Prints a table built entirely from **real checks** — nothing is green by default:

```
JARVIS FIELD READINESS
======================
CORE RUNTIME       OK
OLLAMA             OK
FAST MODEL         qwen3:8b
DEEP MODEL         qwen3:14b
VISION MODEL       gemma3:4b
COLLECTORS         4 ACTIVE / 5 DORMANT
ASSETS             12 OBSERVED
SENSORS            3 CONNECTED
AURA               READY
PERSISTENCE        VOLATILE (in-memory)
DOCKER             DORMANT
VMWARE             AVAILABLE
AUTHORIZED SCOPE   LAB-A
RUNBOOK EXECUTION  DRY-RUN READY

VERDICT: FIELD READY
```

- `--no-ollama` skips the network probe. `--json` for machine output.
- `--collectors` shows the unified runtime + collector health detail.
- Exit code is non-zero only when a **critical** line fails (core runtime, FAST/DEEP
  models). Ollama down or Docker absent lowers capability but still permits
  deterministic monitoring — the report says so honestly. `PERSISTENCE VOLATILE` is
  expected on a field host with no Postgres; it is not a failure, it is a fact.

---

## 2. Enroll and authorize an environment (M29)

Discovery never scans blindly. The operator explicitly **enrolls** the environments
they are authorized to observe, and enrollment is separate from **authorization**.

```python
from core.environment_registry import env_registry, EnvironmentType

# 1) enroll (inert until authorized). credentials_ref is a REFERENCE, never a secret.
env_registry.enroll("docker-local", EnvironmentType.DOCKER, "Local Docker",
                    endpoint="npipe:////./pipe/docker_engine")
env_registry.enroll("edge-01", EnvironmentType.REMOTE_LINUX, "Edge host",
                    credentials_ref="env:LAB_SSH_KEY_PATH")   # an env-var NAME / key path

# 2) authorize with the operator's scope (this is the consent step)
env_registry.authorize("docker-local", scope="LAB-A")
```

- A `credentials_ref` that looks like a raw secret (a private key, a long high-entropy
  blob) is **rejected** at enrollment. No projection ever emits the reference to AURA
  or logs — only whether credentials are configured.
- `env_registry.audit_trail()` is the auditable record of every enroll/authorize/revoke.
- `env_registry.save(path)` / `EnvironmentRegistry.load(path)` persist the registry;
  a missing or corrupt store loads to an explicit empty registry (never fabricated).

---

## 3. Discover assets into the graph (M29)

Discovery folds an **already-fetched** inventory into the existing evidence-backed asset
graph with provenance (DOCKER_INSPECT / LAB_MANAGER / SENSOR_MESH / OPERATOR_DECLARATION)
and is **fail-closed** on an un-authorized environment.

```python
from core.asset_discovery import apply_discovery, probe_docker_inventory
import asyncio

entry = env_registry.get("docker-local")            # must be authorized
inv = asyncio.run(probe_docker_inventory())         # shell=False; None if docker absent
if inv:
    result = apply_discovery(entry, graph, inv)     # writes containers/services + provenance
```

Unknown stays unknown — absent inventory fields are not guessed, and a disagreeing
observation is surfaced as a conflict, never silently overwritten.

---

## 4. Replay an end-to-end scenario (M30)

Deterministic detection-to-response over the **real** spine. Nothing can touch the host
— the runbook engine runs without a ToolExecutor, so every scenario can only dry-run.

```
python scripts/scenario_runner.py --list
python scripts/scenario_runner.py auth_sequence
python scripts/scenario_runner.py new_service_exposure       # HITL-gated scan shown
python scripts/scenario_runner.py all --json
```

Scenarios: `auth_sequence`, `new_service_exposure`, `sensor_loss`, `container_failure`,
`duplicate_out_of_order`. Each prints the full chain (event → finding → incident → drift
→ situation → recommended runbook → dry-run plan → verification → AURA events) and a
pass/fail against declared facts *and* non-facts. Exit code is non-zero on any failure.

---

## 5. Start AURA — the live operator command center (M31)

Start JARVIS (`python main.py`) and open the AURA HUD. Over the existing WebSocket, the
HUD can request the unified, bounded, redacted command center:

```json
{ "cmd": "ops_command_center" }
```

It returns SYSTEM STATUS, ASSETS, INCIDENTS, DRIFT, SENSOR HEALTH, COLLECTORS,
CORRELATIONS, RUNBOOKS, CURRENT SITUATION, MODEL/RUNTIME — plus the operator digest:
**what is happening / what changed / what matters / what is uncertain / what to do next**.

Payloads are bounded and redacted: no credentials, tokens, private keys, command lines,
raw telemetry, PCAP, or memory dumps ever reach the HUD. The build is a pure in-memory
projection and never blocks the event loop, so AURA stays responsive during a DEEP
inference.

---

## 6. Ask grounded operational questions (M32)

Read-only, grounded in structured state — no invented assets/incidents/services/evidence.

```json
{ "cmd": "ops_query", "args": { "question": "what is happening right now?" } }
```

Try: "what changed in the last ten minutes?", "which assets are unhealthy?", "why is
this incident important?", "what evidence supports this finding?", "which services are
exposed?", "which sensors are blind?", "what is uncertain?", "what runbook do you
recommend?", "show the timeline of incident X".

Empty state is honest: **"I do not have evidence of an active incident."** — never
"everything is secure". Unknown is not safe; absence of evidence is not proof of absence.

---

## 7. Voice operational control (M33)

Uses the existing STT/TTS and barge-in. Voice text resolves ONLY to a fixed set of typed
intents — it is never turned into arbitrary shell.

- Read-only (answered directly): "Jarvis, system status." / "what changed?" /
  "summarize incidents." / "what is uncertain?" / "show unhealthy assets." /
  "recommend a runbook."
- Dry-run (planning only): "Jarvis, dry-run the recommended runbook."
- Refused from voice: "Jarvis, execute the runbook." → routed to the out-of-band HITL +
  authority + scope + audit gate. Voice never auto-runs a world effect.

---

## 8. Runtime & collector health (M34)

```
python scripts/field_readiness.py --collectors
```

or over AURA: `{ "cmd": "runtime_health" }`. Statuses use the fabric vocabulary
(OK / WARMING / DORMANT / OPTIONAL / DEGRADED / FAILED / STOPPING / BACKPRESSURE) across
collectors, resource (CPU/RAM), tasks, inference latency, model runtime, and the spine.
DORMANT/OPTIONAL are not failures; an unmeasured metric is reported as such, not faked.

---

## 9. Security posture (never weakened)

Reasoning freedom is not execution authority. Every world effect stays behind the single
guarded path (ToolExecutor + Authority + Scope + Risk + HITL + Audit + cancellation),
limited to the local machine and explicitly enrolled, authorized scopes (CTF /
TRUSTED_LAB / PURPLE_TEAM / INCIDENT_RESPONSE). High-impact actions require operator
approval; a denied approval or a cancelled token blocks the action with no bypass.

For chaos/failure behavior and the safe-degradation guarantees, see
`tests/test_resilience_v67.py`.
