# JARVIS V69 M61.8 — Release Closure & Verifiable Release Evidence

**Branch:** `jarvis-v69-m61-release-closure`
**Base:** `0bb1a6b` — *merge: JARVIS V69 M61 production stabilization and release
engineering* (V69 M61, merged to `master`)
**Canonical version:** `69.61.0` (`core/version.py`) — **unchanged by this stage**
**Stage:** M61.8 is an *internal closure stage* of release `69.61.0`. It is not a
version, not a package, and not M62.
**Tag / Release state:** no tag exists, no GitHub Release exists, nothing published.

---

## 1. What M61.8 is for

M61 shipped the engineering surface — CI, packaging, dependency authority, the Bandit
gate, the qualification harness. What it did *not* ship was a way for anyone outside the
session that ran those gates to check that they were run, or what they said.

The release truth lived in prose. A number in `CHANGELOG.md`, a different number in a
milestone document, a third in a README table — each written by hand, at a different
time, from a different measurement, with nothing to notice when they diverged. The
qualification harness produced a real verdict and then that verdict was *retyped* into
documentation. Every restatement was a place drift could enter silently.

M61.8 closes that gap in three moves:

1. **One declaration.** `core/release_facts.py` is the single place a release fact is
   written down. Every other file either imports it or is checked against it.
2. **Machine-checked documentation.** `core/release_check.py` grew from five truth
   families to eight. Documentation claims are now *audited*, not trusted.
3. **Verifiable artifacts.** A sanitized evidence bundle, SHA-256 checksums and
   CycloneDX SBOMs, each with a schema and each reproducible by a third party from the
   commands recorded inside them.

M61.8 adds **no** offensive, autonomous, surveillance, persistence, exploitation or
remote-execution capability. It adds no runtime feature at all. Every module it
introduces is read-only with respect to the running system: it hashes files, parses
declarations, and writes reports into a directory the caller names.

---

## 2. The eight release-truth families

`core/release_check.audit()` returns a per-family problem count. A family is a *class*
of documentation lie, not a file. M61 shipped five; M61.8 added three and extended a
fourth.

| Family | What a failure means | Added |
|---|---|---|
| `version` | a document states a version other than the canonical `69.61.0` | M61 |
| `models` | a document names a model tag the router cannot route to | M61 |
| `claims` | a document advertises autonomy the system does not have | M61 |
| `status` | a release document contradicts the merge state of a milestone at or below the canonical one; **or** claims the tag/Release already exists | M61, extended M61.8 |
| `install` | a README install command points at a file that does not exist | M61 |
| `counts` | two documents state different validated test/finding counts, or state one that disagrees with `release_facts.py` | **M61.8** |
| `security` | a document states a Bandit result that contradicts the declared gate outcome | **M61.8** |
| `posture` | an operator-facing document advertises a capability the code refuses, or `SECURITY.md` omits a declared posture | **M61.8** |

### 2.1 Why `models` is now derived, not listed

The M61 implementation compared documentation against a hand-maintained tuple of model
tags. That tuple was itself documentation, so it could drift from the router in exactly
the way the check existed to prevent.

M61.8 derives the authoritative tag set by **AST-parsing** `core/model_router.py` and
`core/hardware_model_profile.py` for their string constants. Parsing rather than
importing is deliberate: the `consistency` CI job installs no dependencies, and those
modules import `httpx` and `psutil`. A check that only runs where the runtime is
installed is not a check that guards the release.

The derived set at this commit is: `gemma3:4b`, `llama3.2-vision:11b`,
`nomic-embed-text:latest`, `qwen2.5-coder:32b`, `qwen2.5-coder:latest`, `qwen3:14b`,
`qwen3:32b`, `qwen3:8b`.

### 2.2 Why `posture` scans operator documents only

The `posture` family looks for sentences advertising three capabilities the code
refuses: dynamic plugin execution, unconditional all-interface binding, and automatic
dependency installation.

Its scope is the two READMEs, `JARVIS.md`, the installation guide and the troubleshooting
guide — documents that describe how the system behaves *now*. Release and milestone
documents are deliberately **out of scope**, because they legitimately narrate
before-and-after: "these four services bound all interfaces unconditionally, and now do
not" is an accurate sentence that a naive scanner reads as an advertisement.

Even inside operator documents a match is only a problem if it is not negated. A 44
character backward window is checked for a denial (`no`, `never`, `disabled`, `removed`,
`refuses`, `opt-in`, `report-only`, …). Without that guard, every accurate sentence about
a removed capability would have to be written around the checker — which is the
brittleness the family exists to avoid.

The postures themselves are required to be *declared* in `SECURITY.md`, under four
structural anchors. "We quietly stopped doing it" is not a posture an operator can rely
on.

---

## 3. Verifiable artifacts

### 3.1 Release evidence — `core/release_evidence.py`

A JSON bundle recording what was run, what it said, and in what kind of environment.
Schema version `m61.8.1`, validated against a draft 2020-12 JSON Schema by a hand-rolled
stdlib validator (same dependency constraint as above).

Nine mandatory sections: `pytest_root_layout`, `pytest_app_layout`, `ruff`, `compileall`,
`bandit`, `packaging`, `package_scan`, `soak`, `qualification`. One optional section:
`live_validation`.

Each section carries a status from a fixed vocabulary — `PASS`, `PASS_WITH_WARNINGS`,
`FAIL`, `SKIPPED`, `INSUFFICIENT_EVIDENCE`. The last two are what makes the bundle
honest: a gate that did not run says so, and a gate that ran without producing a parseable
result says *that*, instead of quietly contributing a pass.

**Sanitization is a refusal, not a filter.** Before a bundle is written it is walked
string by string and scanned for absolute paths, home directories, usernames, the machine
hostname, environment-variable values, and credential shapes. A hit raises
`EvidenceRejected` and **nothing is written**. The alternative — redacting and writing
anyway — would produce a file whose cleanliness depended on the scanner having been
complete, which is not a property anyone can verify from the file.

The write is atomic: staged to a temporary file in the destination directory, validated
against the schema, then moved into place. A rejected or invalid bundle leaves no partial
file behind.

**Environment category, not environment.** The bundle records `windows-amd64` /
`linux-amd64` — a category. It does not record the host name, the user, the working
directory or any absolute path.

### 3.2 Checksums — `core/release_artifacts.py`

SHA-256 only. `SHA256SUMS` in the standard `sha256sum` binary-mode layout, so a third
party verifies with the tool they already have and no JARVIS code at all:

```
sha256sum -c SHA256SUMS
```

MD5 and SHA-1 are not merely unused — a digest of 32 or 40 hex characters is *rejected on
parse* with a named error, so a weak-hash file cannot be verified by this code even by
accident.

The parser refuses: entries whose name contains a path separator, `..`, an absolute path,
a drive letter or a NUL; duplicate entries for the same name; malformed lines; and
symlinked targets. `write_checksums` verifies the file it just wrote and unlinks it if
verification fails, so a corrupt checksum file is never left on disk.

### 3.3 SBOMs — `core/sbom.py`

CycloneDX 1.5 JSON, one document per dependency profile. Mandatory: `base`, `dev`, `soc`.
Optional: `voice`, `docs`, `lab`.

**Declared authority, and the limit of it.** Components come from the dependency
declarations in `pyproject.toml`, not from a resolved environment. That makes the
documents deterministic, offline-reproducible and identical on every machine — and it
means they describe the *declared* dependency set, **not the transitive closure**. That
limitation is recorded inside every document, as a `jarvis:dependency_closure` property
with the value `declared-only`. A `--observe-installed` flag additionally records the
version actually installed alongside the declared specifier; it changes
`jarvis:resolution_mode` to `declared+observed` so a reader can tell which kind of
document they are holding.

Serial numbers are UUIDv5 over the document content, so an unchanged dependency set
produces a byte-identical SBOM. Two builds that differ prove the dependencies differ.

---

## 4. Validated results

The authoritative declaration lives in `core/release_facts.py`. This section quotes it;
it does not own it. If a number here disagrees with that module, the `counts` family
fails the build and this document is wrong.

### 4.1 What is environment-independent

**Zero failures in both supported pytest layouts.** This is the claim that holds
regardless of which interpreter, which optional profiles, and which host you measure on,
and it is the claim the release rests on.

### 4.2 What is environment-dependent

Pass and skip *totals* are not portable, and it would be dishonest to present them as if
they were. Several modules call `pytest.importorskip` at module level: on a host where
the optional dependency is present they contribute dozens of passing tests, and on a host
where it is absent they collapse into a **single** skip. The same commit therefore
reports materially different totals on two correctly-configured machines, with no defect
in either.

Rather than pick one and imply universality, the declaration carries the measurement
environment with the numbers:

| Fact | Value |
|---|---|
| Deterministic suite | **0 failed** |
| Passed / skipped (local measurement) | see `release_facts.DETERMINISTIC_TESTS_PASSED` / `_SKIPPED` |
| Local measurement environment | `release_facts.MEASUREMENT_ENVIRONMENT` |
| Authoritative environment | GitHub Actions, job `tests` — *Deterministic suite (3.11, authoritative)* |
| Root layout (authoritative) | `python -m pytest -q jarvis/tests tests` |
| App layout (equivalent) | `python -m pytest -q tests ../tests` |

The CI job named *authoritative* is authoritative for a reason: it is the environment
whose configuration is version-controlled, so its totals are reproducible by anyone who
reads the workflow. A local total is evidence about a machine; the CI total is evidence
about the commit.

### 4.3 Static analysis

| Fact | Value |
|---|---|
| Gate | `bandit -r core tools -ll -q` — medium+high **blocking** |
| Result | **0 Medium and 0 High** |
| Low findings | 488, against an approved non-increasing baseline of 488 |
| Per-line suppressions | 2, both inventoried and justified |

The Low baseline is a **ceiling, not a target**. `current_low <= baseline` passes, so a
decrease is always allowed and never has to be negotiated; an increase fails and has to be
either fixed or explicitly re-approved. `docs/DEPENDENCY_SECURITY_POLICY.md` §5 records
what the 488 consist of and why they are not blocking.

During M61.8 this gate did its job on M61.8's own code: three new Low findings appeared
(two `B105` on enum members named `PASS` and `PASS_WITH_WARNINGS`, one `B110` bare
handler). They were **fixed**, not baselined — the enum is now built through the
functional API so the member names derive from one tuple, and the bare handler has an
explicit fallback.

### 4.4 Qualification

`python jarvis/scripts/qualify_release_m61.py --full` →
**`PASS_WITH_WARNINGS`**, warning `live_not_requested`.

This is stated as it is. `live_not_requested` is not upgraded to a pass, and the ten
mandatory gates it covers are listed in `release_facts.MANDATORY_GATES`.

---

## 5. Live validation

**Status: `NOT_REQUESTED`.**

No live Ollama validation was executed for this closure. The bundle records
`live_validation` as an optional section with an honest status rather than omitting it,
because a missing section is ambiguous — a reader cannot tell "not run" from "forgotten".

No claim of Windows or Linux *live* support is made anywhere in this release. The
deterministic suite runs on both; that is a different claim and it is the only one made.

---

## 6. What this stage explicitly does not claim

- **No enterprise readiness claim.** This is a single-operator system.
- **No plugin sandbox.** One does not exist. Dynamic source plugins are **disabled
  fail-closed**: the loader refuses to execute plugin source rather than executing it in
  a weaker mode. That is a refusal, not a sandbox, and the distinction matters.
- **No transitive dependency closure.** See §3.3.
- **No tag and no GitHub Release.** Neither exists at this commit. Documentation that
  claimed otherwise would fail the `status` family, which is checked.
- **No published package.** Nothing has been uploaded to any index.
- **No `pip-audit` triage.** The `dependency-audit` job is advisory and its findings are
  visible but not yet assessed. That is recorded as a known limitation, not hidden.

The complete list is `release_facts.KNOWN_LIMITATIONS`, seven entries, and the release
notes reproduce it.

---

## 7. Files

**Added**

| File | Role |
|---|---|
| `core/release_facts.py` | the single declaration of every release fact |
| `core/release_evidence.py` | sanitized, schema-validated, atomically written evidence |
| `core/release_artifacts.py` | SHA-256 checksums with tamper and traversal refusal |
| `core/sbom.py` | CycloneDX 1.5 SBOMs, one per dependency profile |
| `scripts/build_release_evidence_m61.py` | orchestrates a full evidence build |
| `scripts/build_sbom.py` | builds or validates SBOMs |
| `scripts/check_bandit_low_baseline.py` | the non-increasing Low ceiling |
| `scripts/check_release_tag_guard.py` | read-only guard: refuses to proceed if a tag or Release already exists |
| `docs/DEPENDENCY_SECURITY_POLICY.md` | dependency and static-analysis policy |
| `docs/RELEASE_RUNBOOK.md` | the guarded, manual release procedure |
| `docs/BRANCH_PROTECTION.md` | recommended `master` protection, using the real CI job names |
| `docs/releases/v69.61.0.md` | release notes, and the GitHub Release body |

**Extended**

`core/release_check.py` (three new families, one extended, model tags now derived),
`scripts/qualify_release_m61.py` (the M61.8 suites added to the deterministic gate),
`README.md`, `jarvis/README.md`, `SECURITY.md`, `CHANGELOG.md`,
`docs/TROUBLESHOOTING.md`, `V69_M61_PRODUCTION_STABILIZATION.md`.

**Tests**

`tests/test_release_artifacts_v69_m618.py`, `tests/test_release_evidence_v69_m618.py`,
`tests/test_sbom_v69_m618.py`, `tests/test_release_closure_v69_m618.py`,
`tests/test_bandit_low_baseline_v69_m618.py`, and the extended
`tests/test_version_v69_m611.py`.

---

## 8. What the closure suite refuses to let happen

`tests/test_release_closure_v69_m618.py` parses the AST and the string literals of every
release script and asserts that none of them can create or push a tag, create, edit or
delete a GitHub Release, upload to a package index, merge a pull request, run `os.system`,
`eval`, `exec` or `subprocess` with `shell=True`, or invoke git with any mutating
subcommand. The release procedure is a runbook a human executes, and the tooling is
constrained so it cannot execute it on their behalf.

The suite also asserts that running the tests creates no tag, writes no artifact into the
working tree, and publishes nothing.

---

## 9. Next

M62 is a separate milestone and is **not** started here. The immediate next steps for
release `69.61.0` are entirely manual and entirely described in
[`docs/RELEASE_RUNBOOK.md`](../../docs/RELEASE_RUNBOOK.md): open the pull request, observe
CI, merge, run the tag guard, then tag and publish. Each of those is a decision for an
operator, not an action for this repository.
