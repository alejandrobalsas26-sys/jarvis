# V69 M62 — HISTORY INDEX

**A router, not a record.** This file exists so a future session can find the one thing it
needs without reading a 516 KB archive or opening forty milestone documents. It repeats no
milestone content: every row points somewhere.

| | |
|---|---|
| Immutable archive | `jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md` |
| Archive SHA256 | `e0914054da4dde4b785bbdabc45a40e0f8b590c2aa3612e9432c685c0c79c1bf` |
| Archive size | 516,784 bytes · 6,089 lines |
| Current state | `PROGRESS.md` · `state/m62/current.json` |
| Migration record | `jarvis/docs/V69_M62_S3N1_CONTROL_PLANE_V2_ZERO_TRUST_MIGRATION.md` |

**The archive is append-never-edit.** It is the pre-migration `PROGRESS.md`, byte for byte.
Section numbers below (`§n`) are that document's own headings — search for
`## <n> — ` to land on one.

**Do not read the archive by default.** Open it only when a current authority genuinely
cannot answer the question, and then only the section named here.

---

## Milestones

Every deep document lives in `jarvis/docs/` unless the path says otherwise. Commit ranges
resolve with `git log --oneline <range>`.

| Milestone | Result | Deep authority | Range | Why you would open it |
|---|---|---|---|---|
| **S1** | COMPLETE | archive §4 | `7d1f806`…`c58a69d` | the frozen gym contracts and the sandbox command auditor |
| **S2a** | folded into S3A | archive §4 | `191883f` | why there is no separate S2a commit group |
| **S2b** | COMPLETE | archive §4 | `94b83a9`…`e9273e3` | the eleven deterministic graders; the non-vacuous grader rule (D2) |
| **S2c** | COMPLETE | archive §4 | `96b54b0`…`87f4524` | teacher ensemble, sanitization, human-gated consensus |
| **S2d** | COMPLETE | archive §4 | `b66f157`…`4c8fcb1` | the data factory: candidates → splits → manifests → promotion → exports |
| **S3A** | COMPLETE | `V69_M62_S3A_TRAINING_PLANNER.md` | `191883f`…`b8f7d1c` | the planner, the `TRAIN:` token, and the proof that planning is a dry run |
| **S3B** | COMPLETE | `V69_M62_S3B_TRAINING_EXECUTION.md` | `696dc5b`…`df17d5d` | how a token becomes a run; replay-safe run state; artifact policy |
| **S3C** | COMPLETE | `V69_M62_S3C_ADAPTER_EVALUATION.md` | `4a40ccf`…`e6cf8d0` | paired evaluation, hidden packs, `parity_hash`, the two-backend rule |
| **S3D** | COMPLETE after 3 failures | archive §4 | `1034a78`…`d51ef04` | D11–D15, the transformers-5 compatibility defects |
| **S3D.1** | PASS | `V69_M62_FIRST_LORA_SMOKE.md` | `9a3a370`, `2c35c05` | the first verified real adapter (run-004); D16 checkpoint refusal |
| **S3E** | BLOCKED | `docs/V69_M62_S3E1_LIVE_EVALUATION_INFRASTRUCTURE.md` (top-level `docs/`) | `b9fa160`, `01d73ba` | the unwired `--execute` refusal and the five inherited blockers |
| **S3E.1** | READY | same as above | `aa19eb0`…`57f7a58` | D18–D21; the execution-order guarantees and the 21-state machine |
| **S3E.2** | first real 72-generation measurement | `V69_M62_FIRST_LIVE_ADAPTER_EVALUATION.md` | `dc9763d`…`56d9060` | the run of record for run-004; archive §9–§12 hold its numbers |
| **S3F** | PASS | `V69_M62_S3F_REVIEW_AND_CALIBRATION.md` | `56d9060`…`28f1d45` | **D24 grader saturation** — why `<think>` pinned every score at the floor; D25 |
| **S3F.1** | PASS | `V69_M62_S3F1_STRUCTURED_OUTPUT_AND_REVIEW_EVIDENCE.md` | same range | **D26a/D26b** — the thinking policy and real schema validation |
| **S3F.2** | PASS | `V69_M62_S3F2_OPERATOR_RULINGS_AND_EVAL_V2.md` | same range | the **human rulings H1–H6**, body-free review evidence (D27), eval-v2, D28 |
| **S3G** | COMPLETE | `V69_M62_S3G_QUALITY_TRAINING_CANDIDATE_DESIGN.md` | `28f1d45`…`a167420` | **the predeclared acceptance gates SV/QG/FG/OG** — §6 of that document; train-v1; D29, D30 |
| **S3G.1** | COMPLETE | `V69_M62_S3G1_PRETRAIN_QUALIFICATION.md` | same range | the verified cache and the real tokenizer audit (0 of 128 rows truncate at 512) |
| **S3G.2** | COMPLETE | `V69_M62_S3G2_VALIDATION_WIRING.md` | same range | **D31** — the validation wiring, the cadence, and the checkpoint-safety argument |
| **S3H** | SUCCESS | `V69_M62_S3H_FIRST_QUALITY_LIVE_TRAINING.md` | `a167420`…`4772a2c` | candidate 001's training run of record |
| **S3I.0** | COMPLETE | `V69_M62_S3I0_EVALUATION_RUNTIME_QUALIFICATION.md` | `4772a2c`…`6a3a7fa` | the load benchmark and `KEEP_EXISTING_LOADING_STRATEGY`; **D32, D33** |
| **S3I** | BLOCKED | `V69_M62_S3I_FIRST_QUALITY_HELDOUT_EVALUATION.md` | same range | the pre-token gate that stopped: B1 runtime absent, B2 → **D34** |
| **S3I.1** | COMPLETE | `V69_M62_S3I1_KALI_RUNTIME_AND_CANONICAL_LINEAGE.md` | same range | **D34 fixed** — canonical lineage declared, not discovered; the Kali evaluation runtime |
| **S3I LIVE** | PASS / `NOT_ELIGIBLE` | `V69_M62_S3I_LIVE_QUALITY_HELDOUT_EVALUATION.md` | same range | **candidate 001's run of record** — all nine vetoes pass, QG-2/QG-3/FG-1/FG-2 fail |
| **S3J** | PARTIAL | `V69_M62_S3J_SECOND_QUALITY_CANDIDATE_DESIGN.md` | `6a3a7fa`…`8381c64` | train-v2, eval-v3, candidate 002's configuration; **D35, D36** |
| **S3J.1** | PASS | `V69_M62_S3J1_KALI_TRAINING_RUNTIME_QUALIFICATION.md` | `8381c64`…`4ec4b36` | the separate training runtime and where each dependency version came from |
| **S3K** | SUCCESS | `V69_M62_S3K_SECOND_QUALITY_LIVE_TRAINING.md` | `4ec4b36`…`0827689` | candidate 002's training run of record |
| **S3L** | PASS / `NOT_ELIGIBLE` | `V69_M62_S3L_SECOND_QUALITY_HELDOUT_EVALUATION.md` | `0827689`…`22113a0` | **candidate 002's run of record** — three of nine security vetoes FAILED |
| **S3M** | PASS | `V69_M62_S3M_STRUCTURED_OUTPUT_DIAGNOSIS.md` | `22113a0`…`06480cb` | **why 7/9, twice** — one failure not two, and a termination failure; **D37, D38, D39** |
| **S3M.1** | PASS | `V69_M62_S3M1_D37_TEMPLATE_PARITY_QUALIFICATION.md` | `06480cb`…`475f3c9` | **D37 fixed** — train/eval render parity and `chat_render_policy_hash` |
| **S3M.2** | PASS | `V69_M62_S3M2_D38_OUTPUT_BUDGET_INSTRUMENTATION.md` | `475f3c9`…`4c669fa` | **D38 fixed, observability only** — and why there is no D38 gate |
| **S3N** | PASS | `V69_M62_S3N_FRESH_EVAL_V4_FREEZE.md` | `4c669fa`…`ec446e3` | **eval-v4 `FROZEN_UNUSED`**, the preregistration, and the holdout body firewall (§17) |
| **S3N.1** | PASS | `V69_M62_S3N1_CONTROL_PLANE_V2_ZERO_TRUST_MIGRATION.md` | `ec446e3`…`d8462f5` | **this architecture** — the archive, the snapshot chain, the verifier, the threat model |
| **S3O** | PASS | `V69_M62_S3O_CANDIDATE003_CONTROLLED_DESIGN.md` | `d8462f5`… | **candidate 003 `DESIGNED_UNTRAINED`** — the one-axis diff, the per-lineage id derivation, and the two verifier holes a rename and a missing state arm left open |

---

## Defects

Current state for the defects that still bind operation is in `PROGRESS.md` §7. This table
routes to the *history* of every one, including those long closed.

| Defect | Where it was found | Where it was closed | Archive section |
|---|---|---|---|
| D1–D10 | S1 – S3E.1 | closed in place | §13 |
| D11–D15 | S3D | S3D | §13 |
| D16 | S3D.1 | S3D.1 — `checkpoint_strategy` refused at config validation | §13 |
| D17–D23 | S3E – S3E.2 | closed in place | §13 |
| D24 | S3F — grader saturation | S3F | §13, §14.2 |
| D25 | S3F — report serialisation state | S3F | §13, §14.6 |
| D26a / D26b | S3F.1 — thinking policy, real schema validation | S3F.1 | §13, §14.7 |
| D27 | S3F.2 — notes quote the response | S3F.2 | §13 |
| **D28** | S3F.2 — no tool-call transport | **OPEN** | §13, §14.15 |
| **D29** | S3G — refusal detector phrasing | **accepted limitation** | §13, §14.21 |
| D30 | S3G — cache blocker dropped | S3G | §13, §14.28 |
| D31 | S3G.1 — VALIDATION reached nothing | S3G.2 | §13, §14.31 |
| D32 | S3I.0 — eval-v2 digest | superseded by D34 | §13, §14.45 |
| **D33** | S3I.0 — timeout not enforced | **OPEN** | §13, §14.44 |
| D34 | S3I — lineage-dependent digest | S3I.1 | §13, §14.46 |
| D35 | S3J — operator ruling on spent holdouts | ruling stands | §2 (S3J block), §14.67 |
| D36 | S3J — host-dependent dataset identity | S3J | §13, §14.56 |
| D37 | S3M — train/eval render divergence | S3M.1 | §13, §14.79 |
| D38 | S3M — output budget has no metric | S3M.2 (observability only) | §13, §14.80 |
| **D39** | S3M — order-dependent test isolation | **OPEN** | §13, §14.81 |

---

## Where the long-form material went

| What you might be looking for | Archive section |
|---|---|
| The full per-milestone status matrices (S3E.2 → S3N) | §2 |
| The non-negotiable safety and repository invariants, as originally written | §3 |
| The milestone-by-milestone timeline and the 52-commit index | §4 |
| The run-004 smoke adapter and its 40 structural checks | §6 |
| The held-out corpus history and the v1/v2/v3/v4 identity table | §7 |
| The task-pack builder's enforced properties, and the two-digests distinction | §8 |
| S3E.2's plan, generations, empirical results and security findings | §9, §10, §11 |
| The per-request model-load cost measurement | §12 |
| The complete D1–D39 defect ledger with fixes, commits and regression tests | §13 |
| All 94 recorded limitations, including the struck-through superseded ones | §14 |
| Every historical test baseline and gate result, per milestone and per interpreter | §15 |
| The full Git checkpoint table | §16 |
| The runtime artifact policy in full | §17 |
| Every "what future sessions must NOT redo" rule, per milestone | §18 |
| The complete NEXT history, including all superseded framings | §19 |
| The old fast-start read order (superseded by the bootstrap contract in `PROGRESS.md` §11) | §20 |
| The old update protocol (superseded by `PROGRESS.md` §15) | §21 |

---

## Conventions that changed at S3N.1

| Before | Now | Note |
|---|---|---|
| `PROGRESS.md` held current state *and* history | `PROGRESS.md` is current state only | history is the archive |
| A holdout awaiting use was `FROZEN_UNSEEN` (S3J/S3K) | `FROZEN_UNUSED` | same semantics, one canonical spelling; the old one survives only in the archive |
| Milestone reports were appended to `PROGRESS.md` | a milestone document plus one index row | see `PROGRESS.md` §15 |
| State was prose | prose plus a verified snapshot | see `PROGRESS.md` §0 |
