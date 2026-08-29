# V69 M63 — Situational World State Fabric

## What this milestone is

One canonical answer to *"what appears to be true about the authorized
environment right now?"*, plus the read-only connectors that populate it, a
diagnostic that explains why the machine cannot run something, and a bridge that
turns environment change into **proposals** rather than actions.

## What it deliberately is NOT

A rewrite. An audit of the runtime before any code was written found that most
of the intended capability already existed and was already wired:

| Capability | Found as | Action taken |
|---|---|---|
| System graph | `core/asset_graph.py` `AssetGraph`, live-populated by `CorrelatorV2` | **reused**; 7 entity kinds + 3 relations added |
| Normalized observation | `core/ops_events.py` `OperationalEvent` / `normalize_event()` | **reused**, unchanged |
| Event fabric | `aura/server.py broadcast()` → `correlator_v2` → WS fan-out | **reused**; no second bus was built |
| Status/query surface | `core/ops_query.py` `answer_question()` | **reused**; a model-free surface added beside it |
| Presence ladder | `core/presence.py` `PresenceEngine` | **reused**, unchanged |
| Local host / Docker discovery | `core/asset_discovery.py` — fully built, **zero callers** | **wired**, not rewritten |
| Twin drift → Presence | `digital_twin.drift_to_presence_event()` — **zero callers** | **wired** |

Building a second entity store beside `AssetGraph` would have produced two
answers to "is host X online?" and no rule for which wins. `core/world_state.py`
therefore **owns no entities**: it projects the graph into the situational
contract and adds the three things the graph does not have — a staleness policy,
a bounded change log, and dependency-impact queries.

## Modules

| Module | Responsibility |
|---|---|
| `core/world_bounds.py` | Every finite ceiling, in one file. Overrides are clamped, never removed. |
| `core/world_state.py` | Entity projection, observation contract, change detection, graph queries, impact analysis. |
| `core/world_connectors.py` | One read-only connector protocol + local host, Docker, Proxmox, Wazuh, Zeek, HTTP/TCP health. |
| `core/world_presence.py` | Change → Presence proposal (capped at ASK), and the memory boundary. |
| `core/world_status.py` | The structured answers that must work with no model loaded. |
| `core/world_runtime.py` | The supervised refresh loop that joins the above. |
| `core/runtime_doctor.py` | Environment diagnosis. Diagnostic only — it repairs nothing. |

## The safety properties, and how they are enforced

**Environment observations can never cause an effect.** Two independent
mechanisms, both tested:

1. `world_presence.MAX_WORLD_LEVEL` is `ASK`, and every event is clamped to it.
2. Environment events never set `action_tool` / `action_target`, which are the
   only inputs that make `PresenceEngine.evaluate()` consider an ACT proposal.

`core/world_presence.py` and `core/world_runtime.py` import nothing from
`tools/`, asserted by an AST test rather than a comment.

**No target expansion.** Every connector target comes from
`EnvironmentRegistry.authorized_environments()`. Enrolled-but-unauthorized is
skipped. There is no subnet sweep, ARP scan or port scan; an AST test asserts
the primitives are absent. TLS verification has no off switch.

**Failures are honest.** A missing external service is `UNAVAILABLE`, a broken
one is `DEGRADED`, an unconfigured one is `MISCONFIGURED`. None of them is ever
an empty `AVAILABLE` result — "I found nothing" and "I could not look" are
different facts. Truncation downgrades a result to `DEGRADED` rather than
passing as success.

**External text is data.** Untrusted payload strings route through
`core/injection_firewall.py` before reaching a prompt or memory.

**The status surface needs no model.** Every answer carries
`requires_llm: False`, and an AST test asserts `world_status` and `world_state`
import nothing LLM-shaped.

## Runtime Doctor

Detects the failure that actually breaks homelab installs: a Python 3.X package
environment invoked from a Python 3.Y interpreter. `check_interpreter()` takes
both versions as injectable arguments so the mismatch is tested without needing
two interpreters present.

Statuses are `PASS` / `DEGRADED` / `BLOCKED` / `OPTIONAL_MISSING`, and
`OPTIONAL_MISSING` never degrades the overall verdict — a homelab without
Proxmox is healthy, not broken.

## Known limitations

1. **Docker inventory is `docker ps` only.** Stopped containers that have been
   pruned are not observed; they age to `STALE` rather than being marked `GONE`.
2. **Proxmox and Wazuh connectors are unexercised against live endpoints.** No
   such service exists on the development machine, so they are covered by unit
   tests for their configured/misconfigured/refused paths only. They are
   `OPTIONAL_MISSING` here, and that is recorded rather than claimed as a pass.
3. **`ENTITY_DISAPPEARED` is never inferred.** Absence of evidence ages an
   entity to `STALE`; only a positive observation of removal (`mark_gone`) makes
   it `GONE`. This is deliberate and means a genuinely deleted VM stays `STALE`
   until something says otherwise.
4. **The twin-drift half of change detection is wired to Presence but not to an
   automatic desired-state source.** Expected/desired state remains
   operator-owned, so drift only fires where an operator has declared intent.
5. **Cross-holdout comparability does not apply here** — this milestone touches
   no evaluation or training material.

## Scope boundary

This milestone changed no M62 scientific state. `state/m62/**`, the candidate
adapters, receipts, witnesses, eval-v5/v6 lifecycle, gates and graders are
untouched.
