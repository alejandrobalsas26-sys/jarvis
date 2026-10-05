"""V69 M67A.1 — the invariants the REAL corpus run must satisfy, reproduced synthetically.

The real operator corpus is one Word document of visible assistant-reasoning prose with no
role structure. It cannot appear here: §26 and §4 both forbid copying private bodies into
tracked tests. So every fixture below reproduces the real export's STRUCTURE — a flat run
of unlabelled ``w:p`` paragraphs, no ``w:pStyle``, repeated paragraphs, an embedded image —
and asserts the behaviour that was then measured against the real file.

The measured outcome these tests pin: 281 non-empty paragraphs, 0 style elements, 0 label
matches, 0 conversations ingested, 0 families, a NON-defensible split, and no frozen
holdout. The pipeline refusing to carve a holdout out of an empty corpus is the single most
important thing that happened in this milestone, because the tempting alternative — import
the paragraphs as one conversation and split them — would have produced a corpus-shaped
artifact with no corpus in it.
"""
from __future__ import annotations

import hashlib
import io
import re
import subprocess
import zipfile
from pathlib import Path

import pytest

from reasoning_distillation import pipeline, storage
from reasoning_distillation.config import default_config

REPO_ROOT = Path(__file__).resolve().parents[2]

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# The fixture builders are duplicated from test_reasoning_docx_adapter_m67a1 rather than
# imported. `jarvis/tests` resolves as a namespace package under the authoritative
# repository-root invocation and as a plain directory under `pytest` from `jarvis/`, so a
# relative import works in one and fails in the other. Two six-line helpers cost less than
# a module that only collects from one of the two canonical entrypoints.
def _para(text: str, *, style: str = "", delete: bool = False) -> str:
    tag = "w:delText" if delete else "w:t"
    style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{style_xml}<w:r><{tag} xml:space="preserve">{text}</{tag}></w:r></w:p>'


def docx_bytes(body_xml: str) -> bytes:
    document = (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<w:document xmlns:w="{_W}"><w:body>{body_xml}</w:body></w:document>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def labelled_docx() -> bytes:
    return docx_bytes(
        _para("Human:") + _para("Deploy the service to staging.")
        + _para("Claude:") + _para("I will run the staging deploy and report the result."))


def role_less_docx(paragraphs: int = 40) -> bytes:
    """The real export's shape: unlabelled first-person narration, no styles."""
    body = "".join(
        _para(f"Now I am weighing consideration {i}, which affects the approach here.")
        for i in range(paragraphs))
    return docx_bytes(body)


# ── the real outcome, reproduced ─────────────────────────────────────────────────────

def test_a_role_less_docx_corpus_ingests_nothing_and_says_why(tmp_path):
    source = tmp_path / "pasted.docx"
    source.write_bytes(role_less_docx())
    outcome = pipeline.ingest_paths([source], config=default_config())
    assert outcome.conversations == () or len(outcome.conversations) == 0
    assert len(outcome.skipped) == 1
    assert "no recoverable role structure" in outcome.skipped[0][1]


def test_an_empty_corpus_produces_no_frozen_holdout(tmp_path):
    """§9/§16: freezing an empty holdout would record that a holdout exists."""
    source = tmp_path / "pasted.docx"
    source.write_bytes(role_less_docx())
    build = pipeline.run([source], config=default_config(), freeze_holdout=True, distil=False)
    assert build.split.holdout_available is False
    assert build.split.defensible is False
    assert build.frozen is None, "no frozen record may exist for an empty corpus"
    assert any("empty" in s or "no defensible holdout" in s
               for s in build.split.shortfalls)


def test_a_corpus_below_the_family_floor_refuses_a_holdout_rather_than_carving_one(tmp_path):
    """One real conversation is not 24 families. The floor is not advisory."""
    source = tmp_path / "chat.docx"
    source.write_bytes(labelled_docx())
    build = pipeline.run([source], config=default_config(), freeze_holdout=True, distil=False)
    assert build.split.holdout_available is False
    assert build.frozen is None
    assert default_config().split.min_families_for_holdout == 24


def test_the_build_is_idempotent_over_unchanged_input(tmp_path):
    """§17: a rerun that changes the dataset without a source change is a bug."""
    source = tmp_path / "chat.docx"
    source.write_bytes(labelled_docx())
    first = pipeline.run([source], config=default_config(), distil=False)
    second = pipeline.run([source], config=default_config(), distil=False)
    assert first.manifest.structural_digest == second.manifest.structural_digest
    assert first.counts() == second.counts()


def test_processing_never_modifies_the_source_export(tmp_path):
    """§25: derived data goes elsewhere; the operator's export is read-only."""
    source = tmp_path / "chat.docx"
    source.write_bytes(labelled_docx())
    before = (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_size)
    pipeline.run([source], config=default_config(), distil=False)
    after = (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_size)
    assert before == after


def test_duplicate_paragraphs_inside_one_export_are_counted_not_silently_merged(tmp_path):
    """The real export repeats 34 paragraphs; the dedupe stage must own that fact."""
    body = (_para("Human:") + _para("repeated request text") + _para("Claude:")
            + _para("answer one")
            + _para("Human:") + _para("repeated request text") + _para("Claude:")
            + _para("answer two"))
    source = tmp_path / "dupes.docx"
    source.write_bytes(docx_bytes(body))
    build = pipeline.run([source], config=default_config(), distil=False)
    assert build.dedupe_report.family_count <= 1, (
        "two copies of one request belong to ONE family, or a later split could put them "
        "on both sides of the holdout boundary")


# ── privacy: the DOCX path cannot skip classification ────────────────────────────────

def test_a_docx_ingested_conversation_still_carries_explicit_export_status(tmp_path):
    """Absent-control: could a NEW adapter reach the corpus without classification?"""
    source = tmp_path / "chat.docx"
    source.write_bytes(labelled_docx())
    outcome = pipeline.ingest_paths([source], config=default_config())
    assert outcome.conversations
    for conversation in outcome.conversations:
        for turn in conversation.turns:
            assert turn.privacy.export_status is not None
            assert turn.privacy.export_status.value in (
                "export_safe", "export_blocked", "export_unknown")


def test_a_secret_bearing_docx_cannot_reach_a_safe_export_status(tmp_path):
    """Synthetic secret only. The detector must fire through the DOCX container too."""
    body = (_para("Human:") + _para("here is the key")
            + _para("Claude:")
            + _para("Using AKIAIOSFODNN7EXAMPLE and "
                    "aws_secret_access_key=wJalrXUtnFEMI0K7MDENGbPxRfiCYEXAMPLEKEY now."))
    source = tmp_path / "secret.docx"
    source.write_bytes(docx_bytes(body))
    outcome = pipeline.ingest_paths([source], config=default_config())
    statuses = {t.privacy.export_status.value
                for c in outcome.conversations for t in c.turns}
    assert statuses, "the fixture produced no classified turn, so it proves nothing"
    assert "export_safe" not in statuses or any(
        t.privacy.categories for c in outcome.conversations for t in c.turns), (
        "a credential-shaped string reached a clean export status through the DOCX path")


def test_the_privacy_detector_is_non_vacuous_on_the_docx_path(tmp_path):
    """Non-vacuity: a clean DOCX must classify DIFFERENTLY from a secret-bearing one."""
    clean = tmp_path / "clean.docx"
    clean.write_bytes(labelled_docx())
    clean_cats = {c for conv in pipeline.ingest_paths(
        [clean], config=default_config()).conversations
        for t in conv.turns for c in t.privacy.categories}
    body = (_para("Human:") + _para("q") + _para("Claude:")
            + _para("token: Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCD"))
    dirty = tmp_path / "dirty.docx"
    dirty.write_bytes(docx_bytes(body))
    dirty_cats = {c for conv in pipeline.ingest_paths(
        [dirty], config=default_config()).conversations
        for t in conv.turns for c in t.privacy.categories}
    assert dirty_cats != clean_cats, (
        "the classifier returned the same categories for clean and secret-bearing input, "
        "so it is not reading the DOCX text at all")


# ── nothing private reaches Git ──────────────────────────────────────────────────────

def _tracked_text_files() -> "list[Path]":
    out = subprocess.run(["git", "ls-files", "-z", "docs", "state", "jarvis/docs",
                          "PROGRESS.md", "CHANGELOG.md"],
                         cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    paths = [REPO_ROOT / p for p in out.stdout.split("\0") if p]
    return [p for p in paths if p.is_file()]


#: Patterns that must never appear in tracked governance text. Each is a §24 "forbidden in
#: tracked receipt" item expressed as something a scan can actually find.
_LEAK_PATTERNS: "list[tuple[str, re.Pattern[str]]]" = [
    ("email address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}")),
]


@pytest.mark.parametrize("label,pattern", _LEAK_PATTERNS, ids=[p[0] for p in _LEAK_PATTERNS])
def test_no_tracked_governance_document_carries_a_private_marker(label, pattern):
    """§24/§41: a corpus path or identity in tracked text is permanent once pushed."""
    offenders: list[str] = []
    scanned = 0
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):  # pragma: no cover
            continue
        scanned += 1
        for match in pattern.finditer(text):
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)[:24]}...")
    assert scanned > 5, f"only {scanned} tracked documents scanned; the sweep is too narrow"
    assert not offenders, f"{label} in tracked text: {offenders[:5]}"


def test_the_leak_scanner_fires_on_a_synthetic_violation(tmp_path):
    """NON-VACUITY for the leak scan. Five patterns that match nothing prove nothing."""
    sample = ("owned by a@b.co using AKIAIOSFODNN7EXAMPLE and "
              "Bearer abcdefghijklmnopqrstuvwxyz012345 plus\n"
              "-----BEGIN RSA PRIVATE KEY-----")
    fired = [label for label, pattern in _LEAK_PATTERNS if pattern.search(sample)]
    assert len(fired) == len(_LEAK_PATTERNS), f"only {fired} fired"


def test_the_default_corpus_root_is_outside_the_repository():
    """§4: raw conversations must not sit where a `git add -A` could reach them."""
    assert REPO_ROOT not in storage.DEFAULT_CORPUS_ROOT.parents
    assert storage.DEFAULT_CORPUS_ROOT.is_absolute()


def test_the_in_repo_fallback_root_is_gitignored():
    """Measured with git, not read from .gitignore: a line is a claim, not a check."""
    probe = REPO_ROOT / storage.REPO_RELATIVE_ROOT / "conversation.json"
    result = subprocess.run(["git", "check-ignore", "-q", "--no-index", str(probe)],
                            cwd=REPO_ROOT, check=False)
    assert result.returncode == 0, (
        f"{storage.REPO_RELATIVE_ROOT} is NOT ignored by Git; derived corpus data written "
        f"there would be committable")


def test_an_unignored_corpus_root_is_refused(tmp_path):
    """Non-vacuity for the ignore check: it must REFUSE a trackable root."""
    inside = REPO_ROOT / "docs"  # tracked, certainly not ignored
    with pytest.raises(storage.StorageError, match="NOT ignored|could not determine"):
        storage.assert_root_is_gitignored(inside)


def test_running_the_pipeline_adds_no_untracked_file_to_the_repository(tmp_path):
    """§37: corpus processing must not dirty the worktree."""
    before = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT,
                            capture_output=True, text=True, check=False).stdout
    source = tmp_path / "chat.docx"
    source.write_bytes(labelled_docx())
    pipeline.run([source], config=default_config(), distil=False)
    after = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT,
                           capture_output=True, text=True, check=False).stdout
    assert before == after, "the pipeline changed the Git worktree state"

def test_no_tracked_file_references_the_private_derived_corpus_root():
    """§24/§31: a real corpus path in a tracked document is the leak that matters.

    TWO earlier drafts of this control were WRONG, and both failures are the reason it is
    scoped the way it is:

      * matching ``(?:/home/|/Users/)<name>/`` fired on ``/home/jarvis/.cache`` in M66B's
        Docker volume table — an illustrative CONTAINER path naming a service account.
      * matching this operator's own home path fired on
        ``docs/v69_M66B_PLATFORM_CAPABILITY_MATRIX.md``, and then on
        ``jarvis/core/containment.py`` and the M66B escape-matrix tests, where the real
        home path is **load-bearing security configuration**: the sandbox proves the host
        home is ABSENT inside the jail, which it cannot do without naming it.

    So "an absolute path exists" is not the hazard. The hazard is a path that points at
    CORPUS data. The private derived root is the one path that is always both known and
    always forbidden in tracked text, and it is derived at runtime so nothing is hardcoded.
    """
    forbidden = str(storage.DEFAULT_CORPUS_ROOT)
    assert forbidden, "no derived corpus root to check against"
    offenders = []
    scanned = 0
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=False)
    for rel in out.stdout.split("\0"):
        if not rel:
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        if forbidden in text:
            offenders.append(rel)
    assert scanned > 100, f"only {scanned} tracked files scanned; the sweep is too narrow"
    assert not offenders, (
        f"the private derived corpus root {forbidden} is referenced in tracked file(s) "
        f"{offenders[:5]}; a reader could follow it to private data")


def test_the_derived_root_scan_is_non_vacuous():
    """NON-VACUITY: the substring must be found when it IS present."""
    forbidden = str(storage.DEFAULT_CORPUS_ROOT)
    assert forbidden in f"derived corpus written to {forbidden}/development"
    assert forbidden not in "a document naming no corpus paths at all"


def test_the_m67a1_receipt_carries_no_absolute_path_and_no_corpus_filename():
    """The tracked receipt is where a leak would actually land, so check it directly."""
    doc = REPO_ROOT / "docs" / "v69_M67A1_REAL_LEGACY_CORPUS_QUALIFICATION.md"
    if not doc.exists():  # pragma: no cover - the doc lands in the same commit
        pytest.skip("M67A.1 document not written yet")
    text = doc.read_text(encoding="utf-8")
    assert "/home/" not in text and "/Users/" not in text, (
        "the M67A.1 receipt must describe the corpus in aggregates, never by path")
    # A FILENAME reference ends at the extension; a version identifier continues past it
    # (`m67a1.docx.1`). Matching the extension alone rejected this document's own adapter
    # version string, so the control distinguishes the two instead of being relaxed.
    filename_like = re.search(r"[A-Za-z0-9_ -]+\.docx(?![.\w])", text)
    assert filename_like is None, (
        f"a .docx filename in the receipt would name the operator's export: "
        f"{filename_like.group(0)!r}" if filename_like else "")
    for pattern_label, pattern in _LEAK_PATTERNS:
        assert not pattern.search(text), f"{pattern_label} in the M67A.1 receipt"


def test_placement_score_is_stable_across_calls_and_the_seed_participates():
    """§28/§29: a frozen holdout must not reshuffle on rerun.

    Tested at `placement_score` rather than through a built corpus on purpose. The real
    corpus produced ZERO families, and a one-family corpus forces every family into
    development, so neither exercises placement at all — a stability test built on either
    would pass while placement was entirely random. This asserts the function that actually
    decides partitions, over enough families for a reshuffle to be visible.
    """
    from reasoning_distillation.splitting import placement_score

    families = [f"family-{i:04d}" for i in range(50)]
    first = [placement_score(f, seed=20260927) for f in families]
    second = [placement_score(f, seed=20260927) for f in families]
    assert first == second, (
        "placement is not stable across calls, so a rerun could move a family out of the "
        "frozen holdout")
    other_seed = [placement_score(f, seed=1) for f in families]
    assert other_seed != first, "the seed does not participate, so it records nothing"
    assert all(0.0 <= score < 1.0 for score in first)
    assert len(set(first)) > 40, "placement collapses families onto few points"
