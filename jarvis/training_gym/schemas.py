"""training_gym/schemas.py — V69 M62: the frozen vocabulary of the training gym.

WHY THIS EXISTS
---------------
Everything downstream of a practice episode — graders, teachers, dataset promotion,
the LoRA launcher, the adapter evaluator, the Model Registry bridge — has to agree on
what a *result* is, what *counts as evidence*, and what a *hash* is computed over. If
each layer invents its own encoding, two things happen that this milestone exists to
prevent: a teacher review can be replayed onto a different attempt because nothing
binds it, and a "PASS" can mean "the grader ran and passed" in one module and "the
grader did not run, so nothing objected" in another.

So this module owns, and is the only owner of:

  * the schema version stamped into every persisted record;
  * :class:`ResultStatus` — including the two statuses that exist precisely because
    "did not measure" must never be spellable as "passed";
  * :func:`canonical_json` / :func:`sha256_text` / :func:`sha256_obj` — the ONE
    deterministic encoding every hash in the gym is computed over;
  * :func:`sha256_file` / :func:`sha256_tree` — artifact integrity;
  * :class:`SchemaError` and the fail-closed field validators every record uses.

DISCIPLINE
----------
  * pure stdlib; no import from the rest of ``core`` at module scope, so the gym's
    vocabulary is importable in a base (text-mode) install and inside the launcher's
    dedicated training virtualenv, neither of which has the full app installed;
  * no clock, no environment read, no I/O except the explicit file-hashing helpers —
    a record hashed on one host hashes identically on another;
  * fail closed: an unknown field, an unknown enum value or a missing required field
    raises :class:`SchemaError` instead of being coerced to a default.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from enum import Enum
from pathlib import Path, PurePath
from typing import Any

# ── identity ──────────────────────────────────────────────────────────────────
#: Stamped into every persisted gym record. Bump when a record's SHAPE changes in a
#: way a reader cannot tolerate; readers refuse an unknown MAJOR.
SCHEMA_VERSION = "m62.1"

#: The gym implementation's own version, recorded alongside results so a grader's
#: verdict can be attributed to the code that produced it.
GYM_VERSION = "v69.m62"

#: Every persisted record carries this key.
SCHEMA_KEY = "schema_version"


class SchemaError(ValueError):
    """A record failed validation. Fail-closed: never a warning, never a coercion."""


# ── result status ─────────────────────────────────────────────────────────────
class ResultStatus(str, Enum):
    """The outcome vocabulary shared by graders, teachers and evaluation.

    The distinction that matters: ``SKIPPED``, ``ERROR`` and ``INSUFFICIENT_EVIDENCE``
    are all *non-affirmative*. None of them may ever be read as "nothing objected, so
    this is fine" — a mandatory grader that could not run BLOCKS approval exactly as
    hard as one that failed. ``NOT_APPLICABLE`` is the one honest non-measurement:
    the check does not apply to this task family at all, so it neither blocks nor
    contributes evidence.
    """

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIPPED = "skipped"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NOT_APPLICABLE = "not_applicable"

    @property
    def is_affirmative(self) -> bool:
        """True only for a measurement that actually happened and succeeded."""
        return self is ResultStatus.PASS

    @property
    def is_blocking_when_mandatory(self) -> bool:
        """A mandatory check in this state must prevent approval.

        ``NOT_APPLICABLE`` is deliberately excluded — a rule that does not apply is
        not missing evidence. Everything else that is not ``PASS`` is."""
        return self not in (ResultStatus.PASS, ResultStatus.NOT_APPLICABLE)

    @property
    def measured(self) -> bool:
        """True when the check executed and produced a verdict about the subject."""
        return self in (ResultStatus.PASS, ResultStatus.FAIL)


#: The statuses a *mandatory* check may hold and still allow approval.
APPROVABLE_STATUSES = frozenset({ResultStatus.PASS, ResultStatus.NOT_APPLICABLE})


class Severity(str, Enum):
    """How much a finding is allowed to matter. ``BLOCKING`` dominates every score."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKING = "blocking"

    @property
    def blocks(self) -> bool:
        return self in (Severity.HIGH, Severity.BLOCKING)


class SensitivityClass(str, Enum):
    """What a task's material is, for export and teacher-packet decisions."""

    SYNTHETIC = "synthetic"          # generated fixtures; freely exportable
    LAB_FIXTURE = "lab_fixture"      # authorized homelab artifacts, sanitized
    INTERNAL = "internal"            # never leaves the host, never sent to a teacher
    RESTRICTED = "restricted"        # never exported, never trained on

    @property
    def exportable_to_teacher(self) -> bool:
        return self in (SensitivityClass.SYNTHETIC, SensitivityClass.LAB_FIXTURE)

    @property
    def dataset_eligible(self) -> bool:
        return self is not SensitivityClass.RESTRICTED


# ── deterministic encoding (the ONE hash input) ───────────────────────────────
def canonical_json(obj: Any) -> str:
    """The single canonical JSON encoding used for every hash in the gym.

    ``sort_keys`` makes dict ordering irrelevant, the separators remove insignificant
    whitespace, and ``ensure_ascii=False`` keeps non-ASCII content byte-stable rather
    than escaping it differently across Python versions. Two structurally equal
    records therefore always produce the same bytes, on any host.

    ``allow_nan=False`` because ``NaN`` and ``Infinity`` are not JSON and a strict
    reader cannot parse them back; and non-string mapping keys are refused because
    ``json`` would coerce them, making ``{1: "a"}`` and ``{"1": "a"}`` — two distinct
    records — share one digest.
    """
    _assert_hashable_keys(obj, "record")
    try:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":"), default=_json_default)
    except (TypeError, ValueError) as exc:
        # ``allow_nan=False`` reports NaN/Infinity as ValueError, and sort_keys over
        # heterogeneous keys reports TypeError. Both are schema failures; the gym
        # raises exactly one error type so a caller cannot catch only some of them.
        if isinstance(exc, SchemaError):
            raise
        raise SchemaError(f"record is not canonically encodable: {exc}") from None


def _assert_hashable_keys(obj: Any, path: str) -> None:
    """Refuse any mapping key that ``json`` would silently coerce to a string."""
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise SchemaError(
                    f"{path}: non-string mapping key {key!r} ({type(key).__name__}); "
                    f"two distinct records would share one digest")
            _assert_hashable_keys(value, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            _assert_hashable_keys(value, f"{path}[{i}]")


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value)
    if isinstance(value, PurePath):
        # A relative path is data; an ABSOLUTE path is the operator's filesystem
        # layout, which would put a username into a record and make the record's
        # hash differ between two hosts holding identical content.
        if value.is_absolute() or value.drive or value.as_posix().startswith("~"):
            raise SchemaError(
                f"absolute path {value.name!r} may not enter a gym record; store a "
                f"workspace-relative path or a hash instead")
        return value.as_posix()
    raise SchemaError(f"not JSON-encodable in a gym record: {type(value).__name__}")


def sha256_text(text: str) -> str:
    """SHA-256 of *text* as UTF-8.

    Requires a real ``str``: ``None`` and ``""`` must not share a digest, because
    "absent" and "empty" are different facts about a record. Lone surrogates are
    preserved rather than dropped, so two texts that differ only in an unpaired
    surrogate hash differently.
    """
    if not isinstance(text, str):
        raise SchemaError(f"sha256_text: expected str, got {type(text).__name__}")
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def sha256_obj(obj: Any) -> str:
    """SHA-256 over :func:`canonical_json` of *obj*."""
    return sha256_text(canonical_json(obj))


def sha256_file(path: str | Path) -> str:
    """SHA-256 of a file's bytes.

    Raises when the file is absent. An earlier design returned ``""``, which made two
    MISSING files compare equal — a hash check that silently means "neither exists"
    is worse than no hash check at all."""
    p = Path(path)
    if not p.is_file():
        raise SchemaError(f"sha256_file: not a regular file ({p.name!r})")
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


#: Largest single file :func:`sha256_tree` will read into a tree digest.
MAX_HASHED_FILE_BYTES = 512 * 1024 * 1024


def sha256_tree(root: str | Path) -> str:
    """SHA-256 over a directory's sorted ``(relative posix path, bytes)`` pairs.

    Path-sensitive on purpose: renaming a file inside an adapter directory changes
    the hash, because for an adapter the layout *is* part of the artifact.

    Each entry is LENGTH-PREFIXED rather than separated by a byte marker. With
    separators, a file whose content happened to contain the marker could be split
    into two apparent entries, so one file could hash identically to two — exactly
    the ambiguity an integrity hash must not have. Symlinks are skipped rather than
    followed: a link planted inside an adapter directory must not pull the
    operator's private key material into the digest.
    """
    p = Path(root)
    if not p.exists():
        raise SchemaError(f"sha256_tree: path does not exist ({p.name!r})")
    if p.is_file():
        return sha256_file(p)
    h = hashlib.sha256()
    for f in sorted(p.rglob("*"), key=lambda x: x.relative_to(p).as_posix()):
        if f.is_symlink() or not f.is_file():
            continue
        rel = f.relative_to(p).as_posix().encode("utf-8", "surrogatepass")
        size = f.stat().st_size
        if size > MAX_HASHED_FILE_BYTES:
            raise SchemaError(f"sha256_tree: {f.name!r} exceeds "
                              f"{MAX_HASHED_FILE_BYTES} bytes")
        h.update(len(rel).to_bytes(8, "big"))
        h.update(rel)
        h.update(size.to_bytes(8, "big"))
        with f.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    return h.hexdigest()


def short(digest: str, n: int = 12) -> str:
    """A display prefix of a hash. Never used for comparison."""
    return (digest or "")[:n]


# ── field validation (fail-closed) ────────────────────────────────────────────
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: Windows reserved device names. ``CON`` opens the console from ANY directory, and
#: ``CON.json`` is the same device — the extension does not protect it. Enforced on
#: every platform so a task authored on Linux cannot become dangerous on the
#: operator's Windows host.
RESERVED_DEVICE_NAMES: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def require_id(value: Any, field: str) -> str:
    """An identifier safe to use as a filename component.

    Rejects path separators, spaces, leading dots and anything that could turn an id
    into a traversal or a device name when it later becomes a directory. A non-string
    is refused outright rather than stringified: JSON ``true`` must not become the
    identifier ``"True"``.
    """
    if not isinstance(value, str):
        raise SchemaError(f"{field}: expected a string identifier, got "
                          f"{type(value).__name__}")
    text = value.strip()
    if not _ID_RE.match(text):
        raise SchemaError(f"{field}: invalid identifier {text!r} "
                          f"(expected {_ID_RE.pattern})")
    if text.endswith("."):
        raise SchemaError(f"{field}: identifier {text!r} ends with a dot (Windows "
                          f"strips it, aliasing a different name)")
    if text.split(".", 1)[0].lower() in RESERVED_DEVICE_NAMES:
        raise SchemaError(f"{field}: identifier {text!r} is a Windows reserved "
                          f"device name")
    return text


def require_text(value: Any, field: str, *, min_len: int = 1, max_len: int = 200_000) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{field}: expected a string, got {type(value).__name__}")
    if len(value.strip()) < min_len:
        raise SchemaError(f"{field}: required (min {min_len} chars)")
    if len(value) > max_len:
        raise SchemaError(f"{field}: too long ({len(value)} > {max_len})")
    return value


def require_bool(value: Any, field: str) -> bool:
    """A boolean that refuses to guess.

    ``bool("false")`` is ``True`` and ``bool(0)`` is ``False``; a record that writes
    ``"no_new_privileges": "false"`` must not be read as the opposite of what it says
    — in EITHER direction. Only real booleans, and the two unambiguous JSON-ish
    spellings, are accepted.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise SchemaError(f"{field}: expected a boolean, got {value!r}")


def require_enum(value: Any, enum_cls: type[Enum], field: str) -> Any:
    """Coerce to *enum_cls* or raise. Never falls back to a default member — an
    unrecognised policy value must stop the record, not silently become the safest
    or the first one."""
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except (ValueError, KeyError):
        allowed = sorted(m.value for m in enum_cls)  # type: ignore[attr-defined]
        raise SchemaError(f"{field}: unknown {enum_cls.__name__} {value!r}; "
                          f"allowed {allowed}") from None


def require_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    """An integer that refuses to truncate.

    ``int(900.9)`` is ``900``; a budget written as ``900.9`` must be reported as
    malformed rather than silently becoming a different number the author never
    approved. ``bool`` is excluded because ``isinstance(True, int)`` is ``True``.
    """
    if isinstance(value, bool):
        raise SchemaError(f"{field}: expected an integer, got a boolean ({value!r})")
    if isinstance(value, float) and not value.is_integer():
        raise SchemaError(f"{field}: expected an integer, got the fraction {value!r}")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise SchemaError(f"{field}: not an integer ({value!r})") from None
    if not (minimum <= n <= maximum):
        raise SchemaError(f"{field}: {n} outside [{minimum}, {maximum}]")
    return n


def require_float(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise SchemaError(f"{field}: not a number ({value!r})") from None
    if x != x or x in (float("inf"), float("-inf")):
        raise SchemaError(f"{field}: not finite ({value!r})")
    if not (minimum <= x <= maximum):
        raise SchemaError(f"{field}: {x} outside [{minimum}, {maximum}]")
    return x


def require_mapping(value: Any, field: str) -> dict:
    if not isinstance(value, Mapping):
        raise SchemaError(f"{field}: expected an object, got {type(value).__name__}")
    return dict(value)


def require_str_tuple(value: Any, field: str, *, max_items: int = 512) -> tuple[str, ...]:
    """A list of real strings. A mapping is refused even though it is ``Iterable``:
    iterating one yields its KEYS, so ``{"policy": "x"}`` would silently become
    ``("policy",)`` — a policy value quietly replaced by a policy name."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise SchemaError(f"{field}: expected a list of strings, got "
                          f"{type(value).__name__}")
    items = list(value)
    if len(items) > max_items:
        raise SchemaError(f"{field}: too many entries ({len(items)} > {max_items})")
    for item in items:
        if not isinstance(item, str):
            raise SchemaError(f"{field}: expected strings, found "
                              f"{type(item).__name__} ({item!r})")
    return tuple(items)


def reject_unknown_fields(payload: Mapping, allowed: Iterable[str], *, label: str) -> None:
    """Fail closed on unknown keys.

    A task spec carrying ``"network": "any"`` under a misspelt key must not load with
    the safe default silently applied — the operator would believe a policy is in
    force that the file never expressed."""
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise SchemaError(f"{label}: unknown field(s) {unknown}")


def check_schema_version(payload: Mapping, *, label: str) -> str:
    """Refuse a record whose schema MAJOR this build does not understand."""
    declared = str(payload.get(SCHEMA_KEY, "")).strip()
    if not declared:
        raise SchemaError(f"{label}: missing {SCHEMA_KEY}")
    if declared.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        raise SchemaError(f"{label}: incompatible {SCHEMA_KEY}={declared!r} "
                          f"(this build speaks {SCHEMA_VERSION!r})")
    return declared


def require_binding(payload: Mapping, *, field: str, expected: str, label: str) -> str:
    """Refuse a child record that was not produced for THIS parent.

    A teacher review, a grader result and a training example each name the hash of the
    attempt they describe. Without this check a favourable review could be lifted off
    one attempt and re-filed against a different, failing one — the record would still
    validate, because every field in it is well-formed. Binding is what makes replay
    detectable rather than merely discouraged.
    """
    declared = str(payload.get(field, "")).strip().lower()
    if not declared:
        raise SchemaError(f"{label}: missing {field} (a record that names no parent "
                          f"cannot be verified against one)")
    if declared != str(expected).strip().lower():
        raise SchemaError(f"{label}: {field}={short(declared)!r} does not match the "
                          f"expected parent {short(expected)!r}; refusing a record "
                          f"bound to a different subject")
    return declared


# ── secret / private-content screening (delegates to the app's own scanners) ───
def scan_private_content(payload: Any) -> tuple[str, ...]:
    """Category names of private content found in *payload*. Empty tuple = clean.

    Delegates to :mod:`core.redaction_policy` and :mod:`core.memory_router`, the
    scanners the rest of the application already trusts, so the gym cannot drift into
    a weaker definition of "secret" than the runtime uses. Imported lazily: the gym's
    schema layer must stay importable where ``core`` is not on the path (notably the
    training virtualenv), and an absent scanner is reported as an explicit
    ``scanner_unavailable`` category rather than as "clean".
    """
    try:
        from core.redaction_policy import scan_structure
        from core.memory_router import contains_secret
    except Exception:  # noqa: BLE001 — absence must be visible, not silently clean
        return ("scanner_unavailable",)

    try:
        found = list(scan_structure(payload))
        blob = payload if isinstance(payload, str) else canonical_json(payload)
        if contains_secret(blob) and "secret" not in found:
            found.append("secret")
    except Exception:  # noqa: BLE001 — a scanner that crashed proved nothing
        return ("scanner_error",)
    return tuple(found)


def assert_no_private_content(payload: Any, *, label: str) -> None:
    """Raise unless *payload* is free of secrets, home paths and hidden reasoning."""
    found = scan_private_content(payload)
    if found:
        raise SchemaError(f"{label}: refusing to emit — private content present "
                          f"({', '.join(found)})")


__all__ = [
    "APPROVABLE_STATUSES", "GYM_VERSION", "MAX_HASHED_FILE_BYTES",
    "RESERVED_DEVICE_NAMES", "SCHEMA_KEY", "SCHEMA_VERSION",
    "ResultStatus", "SchemaError", "SensitivityClass", "Severity",
    "assert_no_private_content", "canonical_json", "check_schema_version",
    "reject_unknown_fields", "require_binding", "require_bool", "require_enum",
    "require_float", "require_id", "require_int", "require_mapping",
    "require_str_tuple", "require_text", "scan_private_content", "sha256_file",
    "sha256_obj", "sha256_text", "sha256_tree", "short",
]
