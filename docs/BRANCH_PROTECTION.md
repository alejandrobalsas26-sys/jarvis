# Recommended branch protection for `master`

**Status: a recommendation, not a record.** This document describes protection that
*should* be configured on `master`. Nothing in this repository applies it, and no tooling
here has permission to. Applying it is a repository-admin action taken deliberately by a
human through the GitHub UI or an explicitly-run API call.

The check names below are **not invented**. Every one is copied from the `name:` field of
a job in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml). A required check whose
name does not exactly match a job name never runs, and GitHub reports it as permanently
pending — which blocks every pull request while proving nothing. If a job is renamed, this
document and the protection settings must be updated together.

---

## 1. Required status checks

GitHub matches required checks by the **job name** as it appears in the checks UI, not by
the job id. Both are listed so a reader can find the job in the workflow file.

| Job id | Required check name (use this) | What it proves |
|---|---|---|
| `consistency` | `Release truth & dependency authority` | version, documentation-truth and dependency declarations agree; runs with **no dependencies installed**, so it guards the release even on a bare runner |
| `lint` | `Ruff & compileall (3.11)` | correctness lint and full byte-compilation |
| `tests` | `Deterministic suite (3.11, authoritative)` | the deterministic suite over both trees — the authoritative test result for a commit |
| `compat` | `Compatibility smoke (3.12)` | consistency, grammar and the M61 suites on the next interpreter |
| `base-install` | `Base text-mode install purity` | the base profile installs and imports with **no** optional package present |
| `packaging` | `Wheel & sdist build + manifest scan` | the distribution builds and contains no secret or stray private file |
| `security-scan` | `Static security analysis (medium/high — blocking)` | Bandit medium and high findings — **blocking** |

### 1.1 Advisory — do **not** require

| Job id | Check name | Why it is not required |
|---|---|---|
| `dependency-audit` | `Dependency audit (advisory)` | `pip-audit` reports against a live advisory database, so a finding can appear on a commit that has not changed. Requiring it makes an unrelated upstream disclosure block every unrelated pull request. It is deliberately declared with no `needs:` so it reports even when `security-scan` is red — its value is visibility, and making it blocking would trade that for pressure to silence it. |

Its findings are not ignored: triage is tracked as a known limitation in
`core/release_facts.KNOWN_LIMITATIONS` (`pip_audit_findings_not_triaged`) and the policy
is in [`DEPENDENCY_SECURITY_POLICY.md`](DEPENDENCY_SECURITY_POLICY.md).

---

## 2. Recommended settings

| Setting | Recommended | Why |
|---|---|---|
| Require a pull request before merging | **on** | direct pushes bypass every check below |
| Required approvals | **1** (0 acceptable for a single-operator repository) | for a solo repository this is a self-review checkpoint, not a second pair of eyes; requiring 1 with no second maintainer will *block you*, so choose deliberately |
| Dismiss stale approvals on new commits | **on** | an approval describes the diff it was given for |
| Require status checks to pass | **on**, with the seven checks in §1 | the reason this document exists |
| Require branches to be up to date before merging | **on** | otherwise two independently-green branches can merge into a red `master` |
| Require conversation resolution | **on** | cheap, and prevents merging over an unanswered objection |
| Require linear history | **on** (optional) | matches how this repository already merges; makes `git log --oneline` on `master` readable |
| Require signed commits | optional | valuable, but enable it only once signing is set up on every machine you push from — otherwise it locks you out of your own repository |
| Allow force pushes | **off** | a force push to `master` destroys the history every tag and Release points at |
| Allow deletions | **off** | |
| Do not allow bypassing the above | **on** for anyone, including admins | an exception that is always available is not a control; if a genuine emergency needs it, turning it off for the duration is a visible, auditable act |

### 2.1 Tag protection

Release tags (`v69.61.0` and successors) point at published artifacts. A moved tag makes
every published checksum a lie.

Add a tag protection rule for the pattern `v*` so tags cannot be deleted or overwritten by
non-admins. Note that a *protected* tag can still be created by anyone with write access —
protection prevents mutation, not creation. Creation is guarded procedurally instead, by
`jarvis/scripts/check_release_tag_guard.py` and the runbook.

---

## 3. Applying it — GitHub UI

1. **Settings → Branches → Add branch protection rule** (or *Add ruleset*, on newer
   accounts — the equivalent fields live under *Rules → Rulesets*).
2. Branch name pattern: `master`.
3. Tick **Require a pull request before merging**; set approvals per §2.
4. Tick **Require status checks to pass before merging**, then **Require branches to be up
   to date before merging**.
5. In the search box, add each of the seven check names from §1 **exactly**. They only
   appear in the picker once the workflow has run at least once on a pull request — if a
   name is missing, open a pull request first and come back.
   *Do not* add `Dependency audit (advisory)`.
6. Tick **Require conversation resolution before merging**.
7. Tick **Require linear history** if you want it.
8. Leave **Allow force pushes** and **Allow deletions** unticked.
9. Tick **Do not allow bypassing the above settings**.
10. Save.
11. **Settings → Tags → New rule**, pattern `v*`.

---

## 4. Applying it — `gh api` (optional, not executed here)

Nothing in this repository runs these. They are written out so the configuration is
reviewable as text rather than as a screenshot, and so a reader can diff intent against
what is actually set.

> These are **effectful administrative changes** to repository settings. Read each one
> before running it, and substitute your own `OWNER/REPO`.

Inspect the current protection first:

```bash
gh api repos/OWNER/REPO/branches/master/protection
```

Apply the recommendation:

```bash
gh api -X PUT repos/OWNER/REPO/branches/master/protection \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "Release truth & dependency authority",
      "Ruff & compileall (3.11)",
      "Deterministic suite (3.11, authoritative)",
      "Compatibility smoke (3.12)",
      "Base text-mode install purity",
      "Wheel & sdist build + manifest scan",
      "Static security analysis (medium/high — blocking)"
    ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "required_approving_review_count": 1
  },
  "required_conversation_resolution": true,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "restrictions": null
}
JSON
```

Tag protection:

```bash
gh api -X POST repos/OWNER/REPO/tags/protection -f pattern='v*'
```

Verify afterwards — a settings change that was not read back is an assumption:

```bash
gh api repos/OWNER/REPO/branches/master/protection \
  --jq '{strict: .required_status_checks.strict,
         checks: .required_status_checks.contexts,
         admins: .enforce_admins.enabled,
         force_push: .allow_force_pushes.enabled}'
```

---

## 5. Keeping this document true

If a job in `.github/workflows/ci.yml` is renamed, added or removed:

1. update the table in §1;
2. update the `contexts` array in §4;
3. update the branch protection settings on GitHub — **this is the step that is easy to
   forget, and the one that matters**. A required check whose job was renamed does not
   fail; it hangs, forever, and the usual response is to un-require it, which quietly
   removes the protection.

The mandatory and advisory job sets are also declared in
`core/release_facts.CI_MANDATORY_JOBS` and `CI_ADVISORY_JOBS`, and
`tests/test_release_closure_v69_m618.py` asserts that those declarations, this document
and the actual workflow file all agree. That check catches a rename inside the repository.
**It cannot see GitHub's settings**, so step 3 remains a human responsibility.

---

## 6. Related

- [`RELEASE_RUNBOOK.md`](RELEASE_RUNBOOK.md) — the guarded release procedure
- [`DEPENDENCY_SECURITY_POLICY.md`](DEPENDENCY_SECURITY_POLICY.md) — dependency and
  static-analysis policy, including the Bandit Low baseline
- [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) — the authority for every
  check name above
