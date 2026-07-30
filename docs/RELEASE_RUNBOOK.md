# Release Runbook

**Applies to:** JARVIS `69.61.0` and later
**Guard:** `python jarvis/scripts/check_release_tag_guard.py`

Twenty-two steps, in order. Steps 1–13 are reversible. Steps 17–20 are not: a pushed tag
is fetched by everyone who pulls, and a GitHub Release whose checksums do not match its
artifacts is worse than no release, because it teaches consumers that verification is
theatre.

Nothing in this repository executes steps 17–20. They are typed by the operator, after the
guard passes.

> **State of `69.61.0` at the time of writing.** Merged to `master` as `0bb1a6b`.
> **No tag exists. No GitHub Release exists. No package has been published.** Steps 1–16
> are done; 17 onward are the remaining manual work.

---

## Conventions

- `$ROOT` is the repository root (the directory holding `README.md` and `jarvis/`).
- `$EV` is a **temporary** directory outside the repository. Release artifacts are never
  written into the working tree — `.gitignore` is not a substitute for not creating them
  there.
- Every command below is read-only or writes only to `$EV`, except the four explicitly
  marked **IRREVERSIBLE**.

---

## 1. Synchronize `master`

```bash
cd $ROOT
git switch master
git fetch --all --prune
git pull --ff-only origin master
```

`--ff-only` is deliberate: if this cannot fast-forward, `master` and `origin/master` have
diverged and that is a problem to understand, not to merge past.

## 2. Verify a clean tree

```bash
git status --porcelain          # must print nothing
git diff --check               # must print nothing
git ls-files --others --exclude-standard   # must print nothing
```

A tag on a dirty tree describes bytes nobody else has. The guard in step 17 refuses this,
but check it now — everything after step 2 is wasted work otherwise.

## 3. Verify the expected commit

```bash
git rev-parse HEAD
git rev-parse origin/master              # must be identical
git rev-list --left-right --count origin/master...HEAD   # must be "0	0"
git log --oneline --decorate -1
```

## 4. Create the release branch

Release preparation happens on a branch, never on `master`:

```bash
git switch -c jarvis-v69-m61-release-closure   # or the next stage's name
```

## 5. Run focused tests

Fast feedback while anything is still changing. Do not run the whole suite yet.

```bash
cd $ROOT/jarvis
python -m pytest -q \
  tests/test_version_v69_m611.py \
  tests/test_release_closure_v69_m618.py \
  tests/test_release_evidence_v69_m618.py \
  tests/test_release_artifacts_v69_m618.py \
  tests/test_sbom_v69_m618.py \
  tests/test_bandit_low_baseline_v69_m618.py
cd $ROOT
```

## 6. Run full qualification

```bash
cd $ROOT
python jarvis/scripts/qualify_release_m61.py --full --output "$EV/qualification.json"
```

Read the verdict, do not skim it:

| Verdict | Meaning |
|---|---|
| `PASS` | every mandatory gate green, no warnings |
| `PASS_WITH_WARNINGS` | every mandatory gate green; at least one honest warning (e.g. `live_not_requested`). **Releasable.** Never rewrite it as `PASS`. |
| `FAIL` | stop |
| `INSUFFICIENT_EVIDENCE` | a gate could not be evaluated. Stop and find out why. |

Both supported layouts must be exercised at least once before release:

```bash
python -m pytest -q jarvis/tests tests     # authoritative
cd jarvis && python -m pytest -q tests ../tests && cd ..
```

They may report different pass/skip splits on differently-provisioned environments; both
must report **0 failures**.

## 7. Build the wheel and sdist

Into a temporary directory, using the project's isolated venv or a throwaway one. **Never
into the working tree, and never into the global interpreter.**

```bash
cd $ROOT/jarvis
python -m build --outdir "$EV/dist"
ls "$EV/dist"        # jarvis-69.61.0-py3-none-any.whl  jarvis-69.61.0.tar.gz
cd $ROOT
```

## 8. Generate the SBOM

```bash
python jarvis/scripts/build_sbom.py --output "$EV/dist"
python jarvis/scripts/build_sbom.py --validate "$EV/dist"
```

Generation is offline and byte-reproducible. No profile is installed to produce it.

## 9. Generate the checksums

```bash
python - <<'PY'
import sys, os; sys.path.insert(0, "jarvis")
from pathlib import Path
from core import release_artifacts
target = Path(os.environ["EV"]) / "dist"
print(release_artifacts.write_checksums(target))
PY
cat "$EV/dist/SHA256SUMS"
```

`write_checksums` verifies what it just wrote and refuses to leave a file behind that did
not verify. Steps 7–9 are also performed for you by step 10; do them separately only when
diagnosing one of them.

## 10. Generate the evidence

```bash
python jarvis/scripts/build_release_evidence_m61.py \
  --output "$EV/release" \
  --qualification "$EV/qualification.json"
```

This builds the wheel and sdist, checksums them, generates the SBOMs, re-runs both pytest
layouts, ruff, compileall, the static-analysis gate and the soak, copies the qualification
verdict verbatim, then validates and sanitizes the bundle and moves it into `$EV/release`
atomically.

Required in the output:

```
evidence_completeness = COMPLETE
overall_status        = PASS or PASS_WITH_WARNINGS
```

`INCOMPLETE` means a mandatory gate produced no evidence. The command exits non-zero and
**the release does not proceed** — `--allow-incomplete` exists for diagnosing a partial
run, never for shipping one.

## 11. Verify the package manifest

Already run inside step 10; run it standalone when investigating:

```bash
cd $ROOT/jarvis
python scripts/check_package_manifest.py --dist "$EV/release" --json
cd $ROOT
```

It opens the archives and inspects the real member list — a `MANIFEST.in` rule that is
quietly wrong looks exactly like one that works.

## 12. Verify no secrets

Three independent checks:

```bash
# 1. the artifacts (same scanner, explicit)
python jarvis/scripts/check_package_manifest.py --dist "$EV/release"

# 2. the evidence bundle: schema + private-content deny-scan
python - <<'PY'
import json, os, sys; sys.path.insert(0, "jarvis")
from pathlib import Path
from core.release_evidence import validate_evidence, scan_for_private_content
doc = json.loads((Path(os.environ["EV"])/"release"/"release-evidence.json").read_text("utf-8"))
print("schema:", validate_evidence(doc) or "OK")
print("private:", scan_for_private_content(doc) or "OK")
PY

# 3. the SBOMs
python jarvis/scripts/build_sbom.py --validate "$EV/release"
```

Then read `SHA256SUMS` and the evidence JSON with your own eyes. They are short.

## 13. Open the pull request

```bash
git push -u origin <release-branch>
gh pr create --base master --head <release-branch> \
  --title "<stage> — <summary>" --body-file <prepared-body>
```

Do **not** enable auto-merge. The point of the next step is to look at the result.

## 14. Observe CI

```bash
gh pr checks --watch
```

All seven mandatory jobs must be green:

| Job id | Display name |
|---|---|
| `consistency` | Release truth & dependency authority |
| `lint` | Ruff & compileall (3.11) |
| `tests` | Deterministic suite (3.11, authoritative) |
| `compat` | Compatibility smoke (3.12) |
| `base-install` | Base text-mode install purity |
| `packaging` | Wheel & sdist build + manifest scan |
| `security-scan` | Static security analysis (medium/high — blocking) |

`dependency-audit` ("Dependency audit (advisory)") is advisory — read it, do not gate on
it. See [`DEPENDENCY_SECURITY_POLICY.md`](DEPENDENCY_SECURITY_POLICY.md) §4 for why.

## 15. Merge

Through the GitHub UI or:

```bash
gh pr merge --merge          # a merge commit, not a squash: milestone history is kept
```

## 16. Synchronize local `master`

```bash
git switch master
git fetch --all --prune
git pull --ff-only origin master
git log --oneline --decorate -1     # note the merge commit
```

Record the merge commit in `jarvis/core/release_facts.py` (`RELEASE_MERGE_COMMIT`) if this
release's closure stage has not already done so — the release-truth gate requires the
release documents to name it.

---

## 17. Run the guard — **before** the irreversible steps

```bash
python jarvis/scripts/check_release_tag_guard.py \
  --tag v69.61.0 \
  --evidence "$EV/release" \
  --check-remote
```

It must print `VERDICT: PASS`. It refuses:

- a dirty working tree;
- being on a feature branch rather than `master`;
- `HEAD` differing from `origin/master`, or no `origin/master` ref at all;
- a duplicate tag, locally or on `origin`;
- a missing evidence bundle;
- evidence that is `INCOMPLETE`, `FAIL`, for a different version, generated from a dirty
  tree, or recording a different commit than the one being tagged;
- artifacts whose `SHA256SUMS` does not verify.

The guard **never fetches**. If `origin/master` might be stale, run `git fetch origin`
yourself first so you see the result.

## 18. Create the annotated tag — **IRREVERSIBLE ONCE PUSHED**

```bash
git tag -a v69.61.0 \
  -m "JARVIS V69 M61 production stabilization release"
git show v69.61.0 --stat | head -20      # confirm the commit and the message
```

Annotated (`-a`), not lightweight: a release tag carries an author, a date and a message.

Signing (`-s`) is not required today because no signing key is established for this
project; see [`BRANCH_PROTECTION.md`](BRANCH_PROTECTION.md) §6.

## 19. Push the tag — **IRREVERSIBLE**

```bash
git push origin v69.61.0
```

Push the one tag by name. Not `--tags`, which pushes every local tag including
experiments.

## 20. Create the GitHub Release — **IRREVERSIBLE (outward-facing)**

Explicit in every dimension: title, prepared body, target commit, artifact list. No
`--generate-notes` — generated notes are a commit list, and this release has prepared
notes that state its limitations.

```bash
gh release create v69.61.0 \
  --title "JARVIS v69.61.0 — Production Stabilization, CI & Release Engineering" \
  --notes-file docs/releases/v69.61.0.md \
  --target 0bb1a6b1875773c8fa4944670fd63d95a6299fa0 \
  --verify-tag \
  "$EV/release/jarvis-69.61.0-py3-none-any.whl" \
  "$EV/release/jarvis-69.61.0.tar.gz" \
  "$EV/release/SHA256SUMS" \
  "$EV/release/release-evidence.json" \
  "$EV/release/release-evidence.schema.json" \
  "$EV/release/sbom-cyclonedx-base.json" \
  "$EV/release/sbom-cyclonedx-dev.json" \
  "$EV/release/sbom-cyclonedx-soc.json"
```

`docs/releases/v69.61.0.md` **is** the release body file; there is no second copy to drift.
`--verify-tag` refuses to create the release if the tag does not exist, so step 19 cannot
be skipped by accident.

Attach artifacts **only when their checksums verified in step 12 and the guard passed in
step 17**. Draft first (`--draft`) if you want to read the rendered body before it is
public.

### Publishing to a package index

Out of scope for this release. Nothing in this repository uploads to PyPI or any index,
and `69.61.0` was not published to one. If that changes it needs its own decision, its own
credentials handling and its own runbook section.

## 21. Verify the published release

```bash
gh release view v69.61.0
mkdir -p "$EV/verify" && cd "$EV/verify"
gh release download v69.61.0
sha256sum -c SHA256SUMS                  # must report OK for every artifact
cd $ROOT
python jarvis/scripts/build_sbom.py --validate "$EV/verify"
```

Verify what was *downloaded*, not what was uploaded. That is the only check that covers
the upload itself.

## 22. Rollback

**A pushed tag and a public release are not silently undone.** Prefer moving forward.

| Situation | Action |
|---|---|
| Wrong artifacts attached, tag correct | `gh release delete-asset v69.61.0 <name>`, re-verify, re-upload. No new tag. |
| Body wrong | `gh release edit v69.61.0 --notes-file <corrected>`. No new tag. |
| Release should not be public yet | `gh release edit v69.61.0 --draft` |
| Tag at the wrong commit, nobody has fetched | `git push origin :refs/tags/v69.61.0` then `git tag -d v69.61.0`. Announce it: someone may have fetched. |
| A defect in the released code | **Do not move the tag.** Fix forward: patch-bump `PATCH` in `jarvis/core/version.py` to `69.61.1`, run this runbook again, and edit the `69.61.0` release body to point at the successor. A moved tag means two different trees have shipped under one name — unauditable. |
| The release must be withdrawn | `gh release delete v69.61.0` (leaves the tag), state why in `CHANGELOG.md`, and keep the tag so the history stays honest. |

Operator-side rollback (reinstall, and the four opt-outs that are usually the better
answer) is documented in [`releases/v69.61.0.md`](releases/v69.61.0.md#rollback).

---

## What this repository will never do for you

Enforced by tests, not by convention:

- create or push a tag;
- create, edit or delete a GitHub Release;
- upload to a package index;
- merge a pull request or enable auto-merge;
- write a release artifact into the working tree;
- fetch, pull, commit or otherwise mutate git state from a release script.

`tests/test_release_closure_v69_m618.py` asserts these over the AST and the string
literals of every release script, so a future edit that adds one of them fails the suite.
