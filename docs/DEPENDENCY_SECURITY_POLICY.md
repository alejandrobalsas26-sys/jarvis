# Dependency & Vulnerability Policy

**Applies to:** JARVIS `69.61.0` and later
**Status:** active
**Owner:** the maintainer (this is a personal project — see [SECURITY.md](../SECURITY.md))

This policy says which findings block a release, which do not, and what evidence is
required before a finding is set aside. It exists because the alternative — deciding
case by case — is how `|| true` ends up in a CI workflow.

---

## 1. Dependency authority

`jarvis/requirements/<profile>.txt` is the **single declared authority** (V69 M61.3).
`pyproject.toml` extras **mirror** it and are verified byte-for-byte by
`core.dependency_authority.audit()`, which runs as a mandatory CI gate. The direction is
one-way: add or change a dependency in the profile **first**, then mirror it.

The legacy monolithic `jarvis/requirements.txt` is a deprecated pointer at
`requirements/all.txt`. It must never regain package declarations of its own.

### Supported profiles

| Profile | Purpose | Release scope |
|---|---|---|
| `base` | text mode floor — no audio, OCR, ML or lab tooling | **highest priority** |
| `dev` | test/lint toolchain (pytest, ruff, bandit, pip-audit) | high — it produces the evidence |
| `soc` | defensive SOC/DFIR/detection stack | high |
| `voice` | STT/TTS | normal |
| `docs` | file parsing and OCR | normal |
| `lab` | **offensive-capable, isolated lab only** | normal, never in `base` |
| `all` | the union plus AURA/RAG/rendering extras | derived |

**Base-profile priority.** A vulnerability reachable from a `base` install affects every
operator, including one running text mode with nothing else installed. It is triaged
first and held to the strictest standard in this document. `core.dependency_authority`
additionally asserts that no GUI-automation, MITM, C2 or exploitation package is
reachable from `base`, and that those packages are still present in `lab` — absent from
`base` is only correct if it is still installable where intended.

**Optional-profile handling.** A finding confined to `voice`, `docs`, `lab` or the `all`
extras does not block a release, because the affected code is not installed for the
majority of operators. It is recorded, and the release notes state the profile. `lab`
findings are additionally weighed against the fact that `lab` is already documented as
isolated-lab-only and gated behind `JARVIS_TRUSTED_LAB`.

---

## 2. Direct vs transitive findings

- **Direct** — the package appears in a `requirements/<profile>.txt`. We control the
  specifier, so we can act: raise the floor, pin, or replace the package.
- **Transitive** — the package is pulled in by a direct dependency. We usually cannot
  fix it; we can raise the direct dependency's floor once it ships a fixed resolution,
  or add a constraint.

The SBOM (`jarvis/scripts/build_sbom.py`) labels every component `direct` or
`inherited:<profile>`, and states in the document that the **transitive installation
closure is not resolved** — that needs a live resolver, a fixed platform and a fixed
interpreter. A per-platform pinned SBOM is a different artifact with different
guarantees and is deferred, not implied.

---

## 3. Code scanning (Bandit) — blocking

| Severity | Disposition |
|---|---|
| **High** | **Blocks.** No release, no merge to `master`. |
| **Medium** | **Blocks.** Same gate, same threshold. |
| Low | Does not block. **Baselined and enforced** — see §5. |

The gate is exactly:

```bash
cd jarvis && bandit -r core tools -ll -q
```

`-ll` means *medium and above*, and both severities block. The CI job
`security-scan` ("Static security analysis (medium/high — blocking)") runs that command
and nothing else; its exit code is the verdict.

Current result: **0 Medium and 0 High** over ~75k lines, exit code 0, no exclusions and
no skipped tests.

### Prohibited ways of making the gate green

These are policy violations, not style preferences. Each is enforced by a test.

- **No broad suppression.** A per-line Bandit suppression comment must name its exact
  test id (`B601`, `B104`, …). A bare one disables every check on its line.
  `tests/test_bandit_gate_v69_m617.py` refuses any suppression without an id, pins the
  whole inventory by file and id, caps the total at four, and cross-checks its own text
  scan against Bandit's `skipped_tests` accounting — if the two disagree, one of them is
  wrong about what is suppressed, which is the condition an allowlist exists to prevent.
- **No `|| true`, no `continue-on-error`, no captured-and-discarded exit status** in a
  mandatory CI job. `tests/test_ci_workflow_v69_m612.py` asserts this over the workflow
  and permits exactly one advisory step, which must report its outcome.
- **No `exclude_dirs`, no repo-wide skip list, no Bandit config file** narrowing the
  scan. Asserted, together with non-vacuity floors: a clean report over a gutted scan is
  not a clean report.
- **No lowering the threshold to match a label.** When the M61.2 label claimed
  "high only" while `-ll` blocked 21 Medium findings, the label was corrected and the 29
  findings were fixed. The threshold was not moved.

### Evidence required to suppress a finding

All five, in the same change:

1. the finding's **classification** — `REAL_VULNERABILITY`, `UNSAFE_DEFAULT`,
   `INTENTIONAL_NON_SECURITY_USE` or `FALSE_POSITIVE`;
2. why **no safer expression exists** — not "it is inconvenient to change";
3. the suppression is **precise**: one line, one named test id;
4. a **justification comment beside the line**, not in a changelog;
5. a **regression test proving the property that licenses it** — structurally where
   possible. The two existing suppressions each have one: the SSH command argument is
   proven a compile-time constant over the AST, and the binding-constants module is
   proven to contain no socket call at all.

An entry in `ALLOWED_SUPPRESSIONS` must be added by hand, which is the review checkpoint.

---

## 4. Dependency scanning (pip-audit) — advisory

The CI job `dependency-audit` ("Dependency audit (advisory)") runs
`pip-audit -r requirements/base.txt` with `continue-on-error`, which **preserves the real
exit status and the full output** and writes the outcome to the run summary.

**Why it is advisory, precisely.** pip-audit resolves against live advisory feeds. A CVE
published against a transitive dependency turns an unchanged, correct commit red with no
code change available to fix it. That is a maintenance signal, not a correctness signal,
and a gate that fails for reasons the author cannot act on gets ignored — which is worse
than an advisory one that gets read.

Advisory does **not** mean discretionary:

- the result is visible in every run and in the run summary;
- the job has no `needs:`, so it reports even when `security-scan` is red;
- a **High or Critical advisory against a direct `base` dependency** is treated as
  release-blocking by this policy even though CI does not enforce it. The release notes
  must state it and the remediation.

### No-fix advisories

An advisory with no fixed version available cannot be remediated by a version bump. In
that case:

1. record the advisory id, the package, the profile and whether it is direct;
2. assess **reachability** — is the vulnerable code path reachable from JARVIS at all,
   and from `base` specifically;
3. if reachable from `base` and no mitigation exists, the release states it as an
   **accepted residual risk** with the reasoning. It is not omitted;
4. re-check on the next release. A no-fix advisory is not closed, it is carried.

---

## 5. The 488 Low Bandit findings

**These are not claimed to be harmless.** They are recorded, bounded and enforced.

| | |
|---|---|
| Approved baseline | **488** (`core.release_facts.BANDIT_LOW_BASELINE`) |
| Currently observed | **488**, same command, no exclusions |
| Enforcement | `jarvis/scripts/check_bandit_low_baseline.py` and `tests/test_bandit_low_baseline_v69_m618.py` |
| Rule | `observed <= baseline` |

- a **decrease passes** — the baseline is a ceiling, not a fixed point, and progress must
  never fail a build;
- an **increase fails**, naming the delta. Either triage the new findings or raise
  `BANDIT_LOW_BASELINE` deliberately, in a diff a reviewer sees;
- a clean count over a scan covering fewer than 40,000 lines is
  `INSUFFICIENT_EVIDENCE`, not a pass.

M61.8 held to this itself: the release-evidence code initially added three Low findings
(two `B105` matches on an enum member named `PASS`, one `B110` bare `except/pass`). They
were **removed rather than baselined** — the status vocabulary is now derived from a
single tuple, and the bare handler gained a real fallback.

**Reduction is scheduled, bounded maintenance work — not part of this release.** M61.8
deliberately does not attempt to eliminate 488 findings: that is a wide, low-value-per-
change sweep across ~280 files, and bundling it with release closure would make both
unreviewable. The intended shape of that later work:

1. group the findings by test id and count each group (the JSON report already does this);
2. take the groups where the fix is mechanical and behaviour-preserving, one id per
   change, each with the baseline lowered in the same commit;
3. for each remaining group, record the classification in this document rather than
   leaving it implicit;
4. never lower the baseline without the scan to prove it.

---

## 6. Remediation expectations

| Situation | Expectation |
|---|---|
| Bandit Medium/High appears | Fixed before merge. It already blocks. |
| Bandit Low count grows | Triaged before the baseline is raised. |
| High/Critical advisory, direct `base` dependency, fix available | Raise the floor in `requirements/base.txt`, mirror into `pyproject.toml`, release note it. |
| High/Critical advisory, direct `base` dependency, no fix | Assess reachability; document as accepted residual risk with reasoning. |
| Any advisory, transitive | Raise the direct dependency's floor when a fixed resolution exists; otherwise carry and re-check. |
| Any advisory, optional profile only | Record; state the profile in the release notes. |

### False positives

A finding dismissed as a false positive is **documented with the reason**, not silently
dropped. The M61.7 triage is the standard to match: of twelve `B104` findings, seven
compared an address rather than binding a socket — including one where Bandit flagged
`core/asset_discovery.py` for *detecting* that a discovered listener is bound to all
interfaces, and one where `core/network_quarantine.py`'s address set is a **safety
interlock** stopping JARVIS from quarantining its own host. Changing that set's semantics
to satisfy a scanner could let JARVIS isolate itself off the network. All seven now
reference named constants so the network semantics are byte-for-byte unchanged while the
literal is declared once.

---

## 7. Changing a constraint

`jarvis/requirements/constraints-ci.txt` bounds **only** the test/lint toolchain —
pytest, pytest-asyncio, ruff, bandit, pip-audit — upper-exclusive at the next major. Its
own test asserts an exact five-name set.

Runtime packages needing platform-specific resolution (`pyaudio`, `yara-python`,
`capstone`, `opencv-python`, `torch-directml`, `pywintrace`) are deliberately **not**
pinned. Hard-pinning them across an untested matrix trades a reproducibility problem for
an installability problem. A test asserts they stay unpinned.

**Procedure for a normal dependency change**

1. edit `requirements/<profile>.txt`;
2. mirror into the matching `pyproject.toml` extra;
3. `python jarvis/scripts/check_release_consistency.py` — must pass;
4. `python -m pytest -q jarvis/tests tests` — must pass;
5. regenerate the SBOM if a release is imminent;
6. state the change and its reason in `CHANGELOG.md`.

**Emergency security update**

Same steps, compressed, and nothing is skipped:

1. raise the floor in the authoritative profile and mirror it;
2. run the consistency gate and the full suite — an emergency is not a reason to ship an
   unverified tree;
3. run `bandit -r core tools -ll -q` and the Low baseline check;
4. regenerate the release evidence so the bundle describes the tree that is shipping;
5. patch-bump `PATCH` in `jarvis/core/version.py` (`69.61.1`) — a security fix gets a
   version, because "the same version, but fixed" is unauditable;
6. state the advisory id, the package and the profile in the release notes.

---

## 8. Release-blocking criteria

A release is **blocked** by any of:

- a Bandit Medium or High finding;
- a Bandit Low count above the approved baseline;
- a High/Critical advisory against a direct `base` dependency with a fix available;
- a dependency-authority drift failure (`consistency` CI job);
- a failing deterministic suite in either supported layout;
- a release-evidence bundle whose `evidence_completeness` is not `COMPLETE`;
- artifact checksums that do not verify.

A release is **not** blocked by:

- an advisory confined to an optional profile;
- a no-fix advisory that is documented as accepted residual risk;
- the advisory `pip-audit` job being red for a reason nobody can act on;
- optional live validation not having been executed — that is recorded as a warning and
  the verdict stays `PASS_WITH_WARNINGS`, never `PASS`.

---

## 9. Accepted residual risk

Carried into `69.61.0`, deliberately and with the reasoning stated:

1. **488 Low Bandit findings**, baselined and enforced (§5). Not eliminated.
2. **The advisory `pip-audit` result is not triaged.** M61 made the result visible;
   acting on it is separate work. This is the highest-value item for the next
   maintenance pass.
3. **No plugin sandbox exists.** Dynamic source-plugin execution is refused
   fail-closed, with no environment variable to re-enable it. That is a removal, not a
   sandbox, and the documentation says so.
4. **The transitive dependency closure is not resolved** in the SBOM. Declared
   dependencies are complete; the installed closure is not described.
5. **Base-install purity is proven in CI, not locally.** A development machine has every
   profile installed, so the local check is necessary and not sufficient.

---

## References

- [SECURITY.md](../SECURITY.md) — security model, declared postures, reporting
- [docs/THREAT_MODEL.md](THREAT_MODEL.md) — threat model
- [docs/RELEASE_RUNBOOK.md](RELEASE_RUNBOOK.md) — the release procedure and its guards
- `jarvis/docs/V69_M61_PRODUCTION_STABILIZATION.md` §13 — the full M61.7 Bandit triage
- `jarvis/core/release_facts.py` — the declared values this policy refers to
