"""tests/test_release_artifacts_v69_m618.py — V69 M61.8: checksums that can fail.

A checksum file is only worth publishing if the verifier fails for the right reasons.
"It said PASS on the happy path" is not evidence of that — every interesting property
here is a REFUSAL, so every test below breaks something on purpose and requires the
failure.

The five tamper cases the release closure specifically owes evidence for:

  1. one byte altered in an artifact       -> mismatch;
  2. a listed artifact removed             -> missing;
  3. a duplicate entry added               -> parse refusal;
  4. an absolute path inserted             -> parse refusal;
  5. an entry pointing outside the release directory -> parse refusal.

Nothing here writes into the repository: every fixture is a ``tmp_path``.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from core import release_artifacts as ra

_WHEEL = "jarvis-69.61.0-py3-none-any.whl"
_SDIST = "jarvis-69.61.0.tar.gz"


@pytest.fixture()
def dist(tmp_path: Path) -> Path:
    """A release directory with two plausible artifacts and a verified SHA256SUMS."""
    (tmp_path / _WHEEL).write_bytes(b"PK\x03\x04 pretend wheel payload")
    (tmp_path / _SDIST).write_bytes(b"\x1f\x8b pretend sdist payload")
    ra.write_checksums(tmp_path)
    return tmp_path


# ── the happy path, stated once ─────────────────────────────────────────────
def test_a_fresh_checksum_file_verifies(dist: Path):
    assert ra.verify_checksums(dist) == []


def test_the_file_is_the_sha256sum_layout(dist: Path):
    """Consumable by `sha256sum -c` — two spaces, basename, sorted, LF, one trailing."""
    raw = (dist / ra.CHECKSUM_FILENAME).read_bytes().decode("utf-8")
    assert "\r\n" not in raw
    assert raw.endswith("\n") and not raw.endswith("\n\n")
    lines = raw.splitlines()
    assert len(lines) == 2
    names = []
    for line in lines:
        digest, sep, name = line.partition("  ")
        assert sep == "  ", f"not two-space separated: {line!r}"
        assert len(digest) == 64 and digest == digest.lower()
        assert "/" not in name and "\\" not in name
        names.append(name)
    assert names == sorted(names)
    assert set(names) == {_WHEEL, _SDIST}


def test_digests_are_the_real_sha256_of_the_bytes(dist: Path):
    entries = ra.parse_checksums((dist / ra.CHECKSUM_FILENAME).read_text("utf-8"))
    for name, digest in entries.items():
        assert digest == hashlib.sha256((dist / name).read_bytes()).hexdigest()


def test_sizes_are_recorded_for_the_evidence_bundle(dist: Path):
    inventory = ra.artifact_inventory(dist)
    assert set(inventory) == {_WHEEL, _SDIST}
    assert inventory[_WHEEL]["kind"] == "wheel"
    assert inventory[_SDIST]["kind"] == "sdist"
    for entry in inventory.values():
        assert entry["size_bytes"] > 0
        assert len(entry["sha256"]) == 64


# ── tamper case 1: one byte ─────────────────────────────────────────────────
def test_a_single_altered_byte_fails_verification(dist: Path):
    target = dist / _WHEEL
    payload = bytearray(target.read_bytes())
    payload[-1] ^= 0x01                       # exactly one bit, exactly one byte
    target.write_bytes(bytes(payload))
    problems = ra.verify_checksums(dist)
    assert problems == [f"{_WHEEL}: sha256 mismatch"]


def test_a_same_length_replacement_still_fails(dist: Path):
    """Length-preserving substitution: only the digest can catch this."""
    original = (dist / _SDIST).read_bytes()
    (dist / _SDIST).write_bytes(b"X" * len(original))
    assert any("sha256 mismatch" in p for p in ra.verify_checksums(dist))


# ── tamper case 2: a missing artifact ───────────────────────────────────────
def test_a_removed_artifact_fails_verification(dist: Path):
    (dist / _SDIST).unlink()
    assert ra.verify_checksums(dist) == [f"{_SDIST}: listed but missing"]


def test_an_extra_unlisted_artifact_fails_verification(dist: Path):
    """An unsigned wheel may not sit beside a release while the sums still say PASS."""
    (dist / "jarvis-69.61.0-py3-none-manylinux1_x86_64.whl").write_bytes(b"rogue")
    problems = ra.verify_checksums(dist)
    assert any("unlisted" in p for p in problems), problems


def test_completeness_can_be_waived_explicitly_and_only_explicitly(dist: Path):
    (dist / "extra-1.0-py3-none-any.whl").write_bytes(b"rogue")
    assert ra.verify_checksums(dist, require_complete=False) == []
    assert ra.verify_checksums(dist) != []


# ── tamper case 3: a duplicate entry ───────────────────────────────────────
def test_a_duplicate_entry_is_refused(dist: Path):
    sums = dist / ra.CHECKSUM_FILENAME
    lines = sums.read_text("utf-8").splitlines()
    sums.write_text("\n".join([*lines, lines[0]]) + "\n", encoding="utf-8")
    problems = ra.verify_checksums(dist)
    assert len(problems) == 1 and "duplicate entry" in problems[0]


def test_a_duplicate_with_a_different_digest_is_refused(dist: Path):
    """The dangerous form: two claims about one file, only one of them true."""
    sums = dist / ra.CHECKSUM_FILENAME
    lines = sums.read_text("utf-8").splitlines()
    forged = f"{'0' * 64}  {_WHEEL}"
    sums.write_text("\n".join([*lines, forged]) + "\n", encoding="utf-8")
    assert any("duplicate entry" in p for p in ra.verify_checksums(dist))


# ── tamper case 4: an absolute path ────────────────────────────────────────
@pytest.mark.parametrize("name", [
    "C:/Windows/System32/drivers/etc/hosts",
    "C:\\Windows\\win.ini",
    "/etc/passwd",
    "//server/share/payload.whl",
    "~/secrets.whl",
])
def test_an_absolute_or_home_path_entry_is_refused(dist: Path, name: str):
    sums = dist / ra.CHECKSUM_FILENAME
    sums.write_text(f"{'a' * 64}  {name}\n", encoding="utf-8")
    problems = ra.verify_checksums(dist)
    assert len(problems) == 1, problems
    assert any(marker in problems[0] for marker in
               ("path separator", "absolute", "home directory")), problems


# ── tamper case 5: pointing outside the release directory ──────────────────
@pytest.mark.parametrize("name", [
    "../outside.whl",
    "../../outside.whl",
    "..",
    "sub/nested.whl",
    "sub\\nested.whl",
])
def test_an_entry_outside_the_release_directory_is_refused(dist: Path, name: str):
    sums = dist / ra.CHECKSUM_FILENAME
    sums.write_text(f"{'b' * 64}  {name}\n", encoding="utf-8")
    problems = ra.verify_checksums(dist)
    assert len(problems) == 1, problems
    assert any(marker in problems[0] for marker in
               ("traverses upward", "path separator")), problems


def test_a_real_file_outside_the_directory_is_not_reachable(dist: Path, tmp_path: Path):
    """The whole point: a traversal that WOULD resolve must still be refused."""
    outside = tmp_path.parent / "outside-payload.whl"
    outside.write_bytes(b"outside")
    relative = os.path.relpath(outside, dist).replace("\\", "/")
    digest = hashlib.sha256(b"outside").hexdigest()
    (dist / ra.CHECKSUM_FILENAME).write_text(
        f"{digest}  {relative}\n", encoding="utf-8")
    problems = ra.verify_checksums(dist)
    assert problems and all("sha256 mismatch" not in p for p in problems), problems


@pytest.mark.skipif(
    os.name == "nt", reason="creating a symlink needs elevation or dev mode on Windows")
def test_a_symlinked_artifact_is_refused(dist: Path, tmp_path: Path):
    real = tmp_path.parent / "real-payload.whl"
    real.write_bytes(b"payload")
    link = dist / "jarvis-69.61.0-py3-none-linux.whl"
    link.symlink_to(real)
    entries = ra.parse_checksums((dist / ra.CHECKSUM_FILENAME).read_text("utf-8"))
    entries[link.name] = hashlib.sha256(b"payload").hexdigest()
    (dist / ra.CHECKSUM_FILENAME).write_text(
        ra.render_checksums(entries), encoding="utf-8")
    assert any("symlink" in p for p in ra.verify_checksums(dist))


# ── weak hashes are refused BY NAME, not silently ──────────────────────────
@pytest.mark.parametrize("digest,expected", [
    ("0" * 32, "md5"),
    ("0" * 40, "sha1"),
])
def test_a_weak_digest_is_refused_and_named(dist: Path, digest: str, expected: str):
    (dist / ra.CHECKSUM_FILENAME).write_text(
        f"{digest}  {_WHEEL}\n", encoding="utf-8")
    problems = ra.verify_checksums(dist)
    assert len(problems) == 1 and expected in problems[0], problems


@pytest.mark.parametrize("digest", ["0" * 63, "0" * 65, "g" * 64, "A" * 63 + "!"])
def test_a_malformed_digest_is_refused(dist: Path, digest: str):
    (dist / ra.CHECKSUM_FILENAME).write_text(
        f"{digest}  {_WHEEL}\n", encoding="utf-8")
    assert ra.verify_checksums(dist) != []


def test_uppercase_digests_are_normalized_not_rejected(dist: Path):
    entries = ra.parse_checksums((dist / ra.CHECKSUM_FILENAME).read_text("utf-8"))
    upper = "\n".join(f"{d.upper()}  {n}" for n, d in sorted(entries.items())) + "\n"
    (dist / ra.CHECKSUM_FILENAME).write_text(upper, encoding="utf-8")
    assert ra.verify_checksums(dist) == []


def test_the_project_publishes_only_sha256():
    assert ra.CHECKSUM_ALGORITHM == "sha256"
    source = Path(ra.__file__).read_text(encoding="utf-8")
    for weak in ("hashlib.md5", "hashlib.sha1("):
        assert weak not in source, f"{weak} must not appear in the checksum module"


# ── malformed files, empty files, and vacuous passes ───────────────────────
@pytest.mark.parametrize("body", [
    "",                                     # empty
    "\n\n",                                 # blank lines only
    f"{'c' * 64} {_WHEEL}\n",               # one space, not two
    f"{'c' * 64}\t{_WHEEL}\n",              # tab
    f"{_WHEEL}  {'c' * 64}\n",              # reversed
    f"{'c' * 64}  \n",                      # no name
])
def test_a_malformed_or_empty_checksum_file_never_verifies(dist: Path, body: str):
    (dist / ra.CHECKSUM_FILENAME).write_text(body, encoding="utf-8")
    assert ra.verify_checksums(dist) != [], "an unusable checksum file reported PASS"


def test_a_missing_checksum_file_never_verifies(dist: Path):
    (dist / ra.CHECKSUM_FILENAME).unlink()
    assert ra.verify_checksums(dist) == [f"{ra.CHECKSUM_FILENAME} is missing"]


def test_building_over_an_empty_directory_refuses(tmp_path: Path):
    """A checksum file listing nothing would verify. That must be impossible."""
    with pytest.raises(ra.ChecksumError, match="no release artifact"):
        ra.write_checksums(tmp_path)


def test_non_artifact_files_are_not_checksummed(dist: Path):
    """Only distributions are release artifacts; the evidence JSON is not one."""
    (dist / "release-evidence.json").write_text("{}", encoding="utf-8")
    (dist / "notes.md").write_text("# notes", encoding="utf-8")
    assert ra.verify_checksums(dist) == []
    assert set(ra.discover_artifacts(dist)) == {_WHEEL, _SDIST}


def test_an_unverified_inventory_is_never_reported(dist: Path):
    """artifact_inventory() must refuse to quote digests it did not check."""
    payload = bytearray((dist / _WHEEL).read_bytes())
    payload[0] ^= 0xFF
    (dist / _WHEEL).write_bytes(bytes(payload))
    with pytest.raises(ra.ChecksumError, match="sha256 mismatch"):
        ra.artifact_inventory(dist)


# ── the producer cannot emit an unsafe file either ─────────────────────────
@pytest.mark.parametrize("name", [
    "../escape.whl", "sub/x.whl", "C:/x.whl", "~/x.whl", "..", ".", " x.whl", "x.whl ",
])
def test_render_refuses_to_produce_an_unsafe_entry(name: str):
    with pytest.raises(ra.ChecksumError):
        ra.render_checksums({name: "d" * 64})


def test_render_refuses_a_weak_digest():
    with pytest.raises(ra.ChecksumError, match="md5"):
        ra.render_checksums({_WHEEL: "0" * 32})


def test_render_is_stable_regardless_of_input_order():
    a = ra.render_checksums({_WHEEL: "1" * 64, _SDIST: "2" * 64})
    b = ra.render_checksums({_SDIST: "2" * 64, _WHEEL: "1" * 64})
    assert a == b


def test_write_checksums_removes_its_own_output_when_it_cannot_verify(
        dist: Path, monkeypatch):
    """A checksum file that failed its own verification must not be left behind."""
    monkeypatch.setattr(ra, "verify_checksums", lambda *a, **k: ["planted failure"])
    (dist / ra.CHECKSUM_FILENAME).unlink()
    with pytest.raises(ra.ChecksumError, match="did not verify"):
        ra.write_checksums(dist)
    assert not (dist / ra.CHECKSUM_FILENAME).exists()
