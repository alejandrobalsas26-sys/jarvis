# JARVIS V68 — Cognitive Command Center, Durable Memory & Decision Intelligence

V68 turns the V67 field-aware runtime into a **persistent, explainable, operator-centered**
intelligence system. It adds durable operational memory, temporal/telemetry intelligence,
evidence-conscious causal reasoning, grounded LLM synthesis, sensor trust, transparent
decision support, and a live cognitive command center in AURA.

Every V68 module **extends** the V66/V67 spine — it never forks it. Everything is read-only
or advisory unless it passes through the existing guarded control plane
(ToolExecutor + Authority + Scope + Risk + HITL/NATO-OTP + Audit). External telemetry is
always data: it can never expand scope, authorize a tool, or execute an action.

Run everything from the `jarvis/` directory.

---

## Epistemic contract (enforced in code, not prose)

V68's core discipline is that the system's certainty is always legible and never inflated:

- **Correlation != proof. Co-occurrence != cause.** `causal_timeline.causal_verdict()`
  returns `NOT_CAUSAL` for temporally-associated / correlated / inferred links.
- **Hypothesis != fact.** A causal link is `UNPROVEN` until an *independent verification*
  resolves it to `PROVEN`/`DISPROVEN`. There is exactly one promotion path out of
  `HYPOTHESIZED`.
- **Unknown != safe. No observation != healthy.** Telemetry reports `DORMANT`/`BLIND`
  (uncertainty), never an inferred failure or compromise; the grounding validator rejects
  "all secure".
- **No invented facts.** The LLM may phrase, not invent — unsupported specifics fail
  grounding and the deterministic answer stands.
- **No fake precision.** Decision dimensions are ordinal `LOW/MED/HIGH`, never decimals.
- **Reasoning freedom != execution authority.** Decision support ranks and explains; it
  never executes and never auto-selects the top option.
- **Never claim durable if only volatile.** The store reports `durable=False` honestly and
  falls back to a safe in-memory mode.

---

## 1. Durable operational state (M38) — `core/operational_store.py`

One coherent SQLite-backed store (WAL, content-hash dedup, schema versioning) for
environments, the asset graph, incidents, twin baselines, and drift/verification/decision/
situation journals.

- **Primary available -> durable writes; unavailable -> explicit degraded + safe volatile
  fallback**, never a false claim of durability.
- **Idempotent replay** (unchanged payload = no-op; changed = version bump = conflict
  visibility), **journal dedup + bounded retention**, **corruption isolation** on read, and
  **strict reconciliation** that quarantines non-JSON input.

```
python -c "from core.operational_store import get_store; print(get_store().health())"
```

## 2. Telemetry intelligence (M39) — `core/telemetry_intel.py`

Per-collector rolling intelligence over a fixed-size arrival ring (memory never grows with
uptime): events/sec+min, median source->ingest lag, clock skew, freshness, out-of-order /
error / dedup / drop ratios, restart rate — plus a derived state:
`HEALTHY / LAGGING / STALE / NOISY / BACKPRESSURED / FLAPPING / RECOVERING / BLIND / DORMANT`.
Quiet (low-volume, recent) is distinguished from broken (silent past the horizon). Fed at
the fabric's single ingest seam; surfaced via `fabric.telemetry_snapshot()` and
`runtime_health`.

## 3. Sensor trust, health & coverage (M41) — `core/sensor_intel.py`

Four orthogonal dimensions over the sensor mesh (+ M39 freshness):
- **Connection** `CONNECTED / IDLE / DISCONNECTED`
- **Health** `PRODUCING / QUIET / STALE / SILENT` (quiet != broken)
- **Trust** `VERIFIED > SIGNED > DECLARED > UNVERIFIED > UNTRUSTED` — an unsigned
  localhost agent is `DECLARED` (observe-only), never `VERIFIED`.
- **Coverage** `COVERED / DEGRADED / UNCOVERED` over authorized environments.

## 4. Causal & change intelligence (M42) — `core/causal_timeline.py`

Assembles events, correlation findings, drift, and verifications into one time-ordered
timeline with every link labeled on the epistemic ladder
(`OBSERVED -> TEMPORALLY_ASSOCIATED -> CORRELATED -> INFERRED -> HYPOTHESIZED ->
VERIFIED / REFUTED`). No silent promotion.

## 5. Grounded cognitive synthesis (M40) — `core/cognitive_synthesis.py`

LLM narrative over a bounded fact bundle, held to account by a **deterministic** grounding
validator (invented specifics, absolute-safety language, causal overreach, evidence
coverage). Ungrounded output is discarded for the deterministic grounded answer; a model
timeout/crash degrades instantly. The model never gets the last word over the evidence.

## 6. Transparent operator decision support (M43) — `core/decision_support.py`

Ranks operator-supplied candidate actions on five ordinal dimensions
(risk, impact, reversibility, info-gain, uncertainty-reduction) with an in-code, transparent
heuristic. Near-ties surface as "no clear winner". Advisory only — no execution path,
`auto_execute` always `False`.

## 7. Cognitive command center (M37) — `aura/`

Six read-only, allowlisted HUD commands render live V68 panels in AURA (open with
**Ctrl+Shift+K**): `collector_telemetry`, `sensor_intel`, `causal_timeline`,
`operational_state_health`, `cognitive_synthesis`, `decision_support`. All payloads are
bounded/redacted server-side; the client escapes and caps. None is risk-gated because none
takes a world-effect.

---

## 8. Production readiness & long-run soak (M44)

`tests/test_v68_soak.py` runs a **deterministic, accelerated 24-hour soak** (an injected
clock is stepped; no real sleeping, no wall-clock) and asserts every domain stays bounded
(Rule of Silicon), correct, and honest: the telemetry ring stays capped while lifetime
counters total everything, the store journal stays pruned to its retention cap, replay is
idempotent, corruption stays isolated, and the sensor / timeline outputs stay within caps.
A production-validation pass runs every live builder over cold singletons without crashing.

```
python -m pytest tests/test_v68_soak.py -q
```

## Test surface

```
python -m pytest tests/test_operational_store_v68.py \
                 tests/test_telemetry_intel_v68.py \
                 tests/test_sensor_intel_v68.py \
                 tests/test_causal_timeline_v68.py \
                 tests/test_decision_support_v68.py \
                 tests/test_cognitive_synthesis_v68.py \
                 tests/test_aura_cognitive_panels_v68.py \
                 tests/test_v68_soak.py -q
```

All V68 modules are deterministic (explicit epochs; the synthesizer is injectable), bounded,
and emit ASCII (Windows console / cp1252 safe).
