"""core/release_evidence.py — V69 M61.8: sanitized, verifiable release evidence.

WHAT THIS IS FOR
----------------
Every release claim M61 makes is checkable in principle — a command exists that
reproduces it. What did not exist was a single machine-readable record saying *which
commands were run, in which environment, and what they returned*, in a form safe to
attach to a public GitHub Release. Prose in a milestone document is not that: it cannot
be diffed, cannot be validated, and drifts the moment the tree moves (M61's own report
still quotes the RC1 test counts).

This module is the evidence format and its rules. It does not run anything — the
generator (``scripts/build_release_evidence_m61.py``) collects results and hands them
here to be assembled, sanitized, validated and written.

THREE RULES, EACH ENFORCED IN CODE
----------------------------------
1. **A missing result can never become PASS.** :class:`Status` has a distinct
   ``INSUFFICIENT_EVIDENCE`` state, :func:`aggregate_status` refuses to promote it, and
   :func:`evidence_completeness` reports ``INCOMPLETE`` when any mandatory section is
   absent or unmeasured. The generator exits non-zero on an incomplete bundle.

2. **Nothing private leaves the machine.** :func:`scan_for_private_content` is a
   deny-scanner over the assembled document: absolute paths, home directories,
   usernames, the machine hostname, sensitive environment *values*, credential-shaped
   tokens and key material are all refused. Evidence that fails the scan is not
   written, not partially written and not returned.

3. **The write is atomic.** :func:`write_evidence` builds into a temporary directory,
   validates there, and only then moves the file into place. A failure leaves no
   partial artifact behind.

CONTENT DISCIPLINE
------------------
The document carries counts, enum states, digests, filenames (basenames only),
repository-relative command strings and version numbers. It carries no prompt, no
response, no conversation, no tool argument, no log, no environment variable value, no
runtime database, no model content and no path that identifies the operator.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import tempfile
from enum import Enum
from pathlib import Path

from core import release_facts
from core.version import VERSION

EVIDENCE_SCHEMA_VERSION = "m61.8.1"

#: Written next to the evidence so a consumer can validate it without this repository.
EVIDENCE_SCHEMA_FILENAME = "release-evidence.schema.json"

PRODUCT = "jarvis"


#: The status vocabulary, declared ONCE as data. Every name here already exists in
#: :class:`core.qualification.ReleaseVerdict` or :class:`core.qualification.CaseVerdict`
#: — release evidence deliberately introduces no third, subtly different vocabulary, and
#: ``tests/test_release_evidence_v69_m618.py`` proves the union relationship rather than
#: trusting this comment.
STATUSES: tuple[str, ...] = (
    "PASS", "PASS_WITH_WARNINGS", "FAIL", "SKIPPED", "INSUFFICIENT_EVIDENCE",
)

#: Built with the functional Enum API so the names are *derived* from
#: :data:`STATUSES` instead of restated beside it — a class body would declare each
#: name twice and let the two lists drift, which is the defect this whole module exists
#: to prevent.
Status = Enum("Status", {name: name for name in STATUSES}, type=str)
Status.__doc__ = "The only statuses evidence may carry. There is no implicit success."

#: Statuses that do NOT establish a positive result. A bundle whose mandatory sections
#: contain any of these cannot be reported as complete evidence.
NON_AFFIRMATIVE = frozenset({
    Status.FAIL.value, Status.SKIPPED.value, Status.INSUFFICIENT_EVIDENCE.value,
})

#: Sections that must carry an affirmative status for the bundle to be COMPLETE.
MANDATORY_SECTIONS: tuple[str, ...] = (
    "pytest_root_layout", "pytest_app_layout", "ruff", "compileall", "bandit",
    "packaging", "package_scan", "soak", "qualification",
)

#: Sections that are recorded but whose absence is an honest gap, not a failure.
OPTIONAL_SECTIONS: tuple[str, ...] = ("live_validation",)


# ══════════════════════════════════════════════════════════════════════════════
#  Platform identity — category only, never the machine
# ══════════════════════════════════════════════════════════════════════════════
def platform_category() -> str:
    """A coarse platform label: ``windows-amd64``, ``linux-x86_64``, ``darwin-arm64``.

    Deliberately NOT ``platform.node()``, ``platform.uname()`` or
    ``platform.platform()``: those carry the machine name and a build string that can
    identify a host. The category is what a reader needs to know a wheel was built on
    a 64-bit Windows box; the hostname is not.
    """
    system = (platform.system() or "unknown").strip().lower()
    machine = (platform.machine() or "unknown").strip().lower()
    return f"{system}-{machine}" if machine else system


# ══════════════════════════════════════════════════════════════════════════════
#  Sanitization — a deny-scanner over the assembled document
# ══════════════════════════════════════════════════════════════════════════════
#: Structural leaks: anything that reveals where on a filesystem this ran.
_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("absolute_windows_path", re.compile(r"[A-Za-z]:[\\/]")),
    ("unc_path", re.compile(r"\\\\[A-Za-z0-9_.-]+\\")),
    ("absolute_posix_system_path", re.compile(
        r"(?<![\w./-])/(?:home|users|root|var|tmp|etc|opt|private|mnt|media)(?:/|\b)",
        re.IGNORECASE)),
    ("home_directory_marker", re.compile(
        r"%USERPROFILE%|%HOMEPATH%|%APPDATA%|%LOCALAPPDATA%|\$HOME\b|~[/\\]")),
    ("user_directory_segment", re.compile(
        r"\b(?:Users|home)[\\/][A-Za-z0-9._-]+", re.IGNORECASE)),
)

#: Credential-shaped material. Prefix-anchored so ordinary hex digests do not match.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("openssh_public_key", re.compile(r"\bssh-(?:rsa|ed25519|dss)\s+AAAA")),
    ("known_hosts_entry", re.compile(r"\|1\|[A-Za-z0-9+/=]{20,}\|")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("anthropic_style_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("bearer_header", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}")),
    ("inline_credential_assignment", re.compile(
        r"\b(?:api[_-]?key|secret|password|passwd|token|credential)\s*[=:]\s*\S{6,}",
        re.IGNORECASE)),
    ("url_with_credentials", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
)

#: Environment variables whose VALUE must never appear. Name fragments, not exact
#: names, so ``JARVIS_TELEGRAM_TOKEN`` and ``OPENAI_API_KEY`` are both covered.
_SENSITIVE_ENV_FRAGMENTS: tuple[str, ...] = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH",
    "SESSION", "COOKIE", "LICENSE",
)

#: Environment variables that identify the host or the operator, value-scanned too.
_IDENTITY_ENV_VARS: tuple[str, ...] = (
    "USERNAME", "USER", "LOGNAME", "USERPROFILE", "HOME", "HOMEPATH", "HOMEDRIVE",
    "COMPUTERNAME", "HOSTNAME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR",
    "USERDOMAIN", "VIRTUAL_ENV", "CONDA_PREFIX",
)

#: Values shorter than this are ignored when scanning environment/identity values: a
#: two-character username would match half the document and prove nothing.
_MIN_IDENTITY_VALUE_LEN = 4


def _identity_values() -> list[tuple[str, str]]:
    """``(label, value)`` pairs whose literal presence in evidence is a leak.

    Reads the environment to know what to REFUSE. No value is ever returned to a
    caller that serializes it, and nothing here is written into the document.
    """
    found: list[tuple[str, str]] = []

    def _add(label: str, value: object) -> None:
        text = str(value or "").strip()
        if len(text) >= _MIN_IDENTITY_VALUE_LEN:
            found.append((label, text))

    _add("machine_hostname", platform.node())
    try:
        _add("os_username", getpass.getuser())
    except Exception:  # noqa: BLE001 — no user database on this host
        # Fall back to the environment rather than giving up on the rule: a host
        # without a password database still has an operator name worth refusing.
        _add("os_username", os.environ.get("USERNAME") or os.environ.get("USER") or "")

    for name in _IDENTITY_ENV_VARS:
        _add(f"env[{name}]", os.environ.get(name, ""))

    for name, value in os.environ.items():
        upper = name.upper()
        if any(fragment in upper for fragment in _SENSITIVE_ENV_FRAGMENTS):
            _add(f"env[{name}]", value)
    return found


def _walk_strings(node: object, path: str = "$"):
    """Yield ``(json_path, text)`` for every string in a JSON-shaped document."""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield f"{path}.{key}", str(key)
            yield from _walk_strings(value, f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _walk_strings(value, f"{path}[{index}]")


def scan_for_private_content(document: object) -> list[str]:
    """Every private-content violation in ``document``. Empty list means clean.

    The problem strings name the JSON location and the RULE that fired — never the
    offending value, because a sanitization report that quotes the secret it found is
    itself a leak.
    """
    problems: list[str] = []
    identity = _identity_values()

    for location, text in _walk_strings(document):
        for label, pattern in _PATH_PATTERNS:
            if pattern.search(text):
                problems.append(f"{location}: {label}")
        for label, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                problems.append(f"{location}: {label}")
        lowered = text.lower()
        for label, value in identity:
            if value.lower() in lowered:
                problems.append(f"{location}: leaks {label}")
    return problems


# ══════════════════════════════════════════════════════════════════════════════
#  Schema
# ══════════════════════════════════════════════════════════════════════════════
_STATUS_SCHEMA = {"type": "string", "enum": list(STATUSES)}
_DIGEST_SCHEMA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}

_ARTIFACT_ENTRY_SCHEMA = {
    "type": "object",
    "required": ["sha256", "size_bytes"],
    "properties": {
        "sha256": _DIGEST_SCHEMA,
        "size_bytes": {"type": "integer", "minimum": 1},
        "kind": {"type": "string"},
    },
    "additionalProperties": True,
}

#: A real JSON Schema (draft 2020-12). Written beside the evidence so a consumer can
#: validate the bundle without this repository. :func:`validate_evidence` implements
#: the same rules deterministically, in stdlib, so the release gate needs no extra
#: dependency to check its own output.
EVIDENCE_JSON_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": f"https://example.invalid/jarvis/release-evidence/{EVIDENCE_SCHEMA_VERSION}",
    "title": "JARVIS release evidence",
    "type": "object",
    "additionalProperties": True,
    "required": [
        "schema_version", "product", "canonical_version", "release_stage",
        "git_commit", "git_branch", "git_clean", "generated_at_utc",
        "python_version", "platform_category",
        "qualification", "pytest_root_layout", "pytest_app_layout", "ruff",
        "compileall", "bandit", "packaging", "package_scan", "soak",
        "live_validation", "dependency_profile_hashes", "artifact_checksums",
        "sbom", "known_limitations", "evidence_completeness", "overall_status",
    ],
    "properties": {
        "schema_version": {"const": EVIDENCE_SCHEMA_VERSION},
        "product": {"const": PRODUCT},
        "canonical_version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
        "release_stage": {"type": "string", "minLength": 2},
        "release_tag": {"type": "string", "pattern": r"^v\d+\.\d+\.\d+$"},
        "git_commit": {"type": "string", "pattern": "^[0-9a-f]{7,40}$"},
        "git_branch": {"type": "string", "minLength": 1},
        "git_clean": {"type": "boolean"},
        "generated_at_utc": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
        },
        "python_version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+"},
        "platform_category": {"type": "string", "pattern": "^[a-z0-9_]+-[a-z0-9_]+$"},
        "qualification": {
            "type": "object",
            "required": ["status", "verdict", "warnings", "gates", "command"],
            "properties": {
                "status": _STATUS_SCHEMA,
                "verdict": {"type": "string"},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "gates": {
                    "type": "object",
                    "additionalProperties": {"type": "string", "enum": list(STATUSES)},
                },
                "command": {"type": "string"},
            },
        },
        "pytest_root_layout": {"$ref": "#/$defs/pytestResult"},
        "pytest_app_layout": {"$ref": "#/$defs/pytestResult"},
        "ruff": {"$ref": "#/$defs/toolResult"},
        "compileall": {"$ref": "#/$defs/toolResult"},
        "bandit": {
            "type": "object",
            "required": ["status", "threshold", "medium", "high", "low",
                         "low_baseline", "suppressions", "command"],
            "properties": {
                "status": _STATUS_SCHEMA,
                "threshold": {"type": "string"},
                "medium": {"type": "integer", "minimum": 0},
                "high": {"type": "integer", "minimum": 0},
                "low": {"type": "integer", "minimum": 0},
                "low_baseline": {"type": "integer", "minimum": 0},
                "suppressions": {"type": "integer", "minimum": 0},
                "command": {"type": "string"},
            },
        },
        "packaging": {
            "type": "object",
            "required": ["status"],
            "properties": {
                "status": _STATUS_SCHEMA,
                "wheel": {"type": ["string", "null"]},
                "sdist": {"type": ["string", "null"]},
            },
        },
        "package_scan": {"$ref": "#/$defs/toolResult"},
        "soak": {"$ref": "#/$defs/toolResult"},
        "live_validation": {
            "type": "object",
            "required": ["status"],
            "properties": {
                "status": {"type": "string",
                           "enum": [*STATUSES, "NOT_REQUESTED"]},
                "detail": {"type": "string"},
            },
        },
        "dependency_profile_hashes": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": _DIGEST_SCHEMA,
        },
        "artifact_checksums": {
            "type": "object",
            "additionalProperties": _ARTIFACT_ENTRY_SCHEMA,
        },
        "sbom": {
            "type": "object",
            "additionalProperties": _ARTIFACT_ENTRY_SCHEMA,
        },
        "known_limitations": {"type": "array", "items": {"type": "string"}},
        "evidence_completeness": {
            "type": "string", "enum": ["COMPLETE", "INCOMPLETE"],
        },
        "overall_status": _STATUS_SCHEMA,
    },
    "$defs": {
        "pytestResult": {
            "type": "object",
            "required": ["status", "command"],
            "properties": {
                "status": _STATUS_SCHEMA,
                "command": {"type": "string"},
                "passed": {"type": ["integer", "null"], "minimum": 0},
                "skipped": {"type": ["integer", "null"], "minimum": 0},
                "failed": {"type": ["integer", "null"], "minimum": 0},
            },
        },
        "toolResult": {
            "type": "object",
            "required": ["status"],
            "properties": {
                "status": _STATUS_SCHEMA,
                "command": {"type": "string"},
                "detail": {"type": "string"},
            },
        },
    },
}


def _type_ok(value: object, expected: object) -> bool:
    kinds = expected if isinstance(expected, list) else [expected]
    for kind in kinds:
        if kind == "object" and isinstance(value, dict):
            return True
        if kind == "array" and isinstance(value, (list, tuple)):
            return True
        if kind == "string" and isinstance(value, str):
            return True
        # bool before integer: in Python `True` is an int, in JSON Schema it is not.
        if kind == "boolean" and isinstance(value, bool):
            return True
        if kind == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if kind == "number" and isinstance(value, (int, float)) \
                and not isinstance(value, bool):
            return True
        if kind == "null" and value is None:
            return True
    return False


def _validate_node(value: object, schema: dict, location: str,
                   root: dict) -> list[str]:
    """Deterministic validation of the schema subset this format uses."""
    problems: list[str] = []

    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        target = root.get("$defs", {}).get(ref.split("/")[-1])
        if isinstance(target, dict):
            return _validate_node(value, target, location, root)
        return [f"{location}: unresolvable $ref {ref!r}"]

    if "const" in schema and value != schema["const"]:
        problems.append(f"{location}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{location}: {value!r} not one of {schema['enum']}")

    if "type" in schema and not _type_ok(value, schema["type"]):
        problems.append(f"{location}: expected type {schema['type']!r}")
        return problems

    if isinstance(value, str):
        pattern = schema.get("pattern")
        if pattern and not re.search(pattern, value):
            problems.append(f"{location}: does not match {pattern!r}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{location}: shorter than {schema['minLength']}")
    elif isinstance(value, bool):
        pass
    elif isinstance(value, int):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{location}: below minimum {schema['minimum']}")

    if isinstance(value, dict):
        for name in schema.get("required", ()):
            if name not in value:
                problems.append(f"{location}: missing required field {name!r}")
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            problems.append(
                f"{location}: needs at least {schema['minProperties']} entries")
        declared = schema.get("properties", {})
        for name, child in value.items():
            if name in declared:
                problems += _validate_node(child, declared[name],
                                           f"{location}.{name}", root)
            else:
                extra = schema.get("additionalProperties")
                if extra is False:
                    problems.append(f"{location}.{name}: unexpected field")
                elif isinstance(extra, dict):
                    problems += _validate_node(child, extra,
                                               f"{location}.{name}", root)
    elif isinstance(value, (list, tuple)):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                problems += _validate_node(item, item_schema,
                                           f"{location}[{index}]", root)
    return problems


def validate_evidence(document: object) -> list[str]:
    """Schema problems in ``document``. Empty list means it conforms."""
    if not isinstance(document, dict):
        return ["$: evidence must be a JSON object"]
    return _validate_node(document, EVIDENCE_JSON_SCHEMA, "$", EVIDENCE_JSON_SCHEMA)


# ══════════════════════════════════════════════════════════════════════════════
#  Section helpers & aggregation
# ══════════════════════════════════════════════════════════════════════════════
def tool_result(status: Status | str, *, command: str = "", detail: str = "") -> dict:
    """One ``{status, command, detail}`` section. ``status`` is validated here."""
    value = status.value if isinstance(status, Status) else str(status)
    if value not in STATUSES:
        raise ValueError(f"not a release-evidence status: {value!r}")
    section = {"status": value}
    if command:
        section["command"] = command
    if detail:
        section["detail"] = detail
    return section


def pytest_result(status: Status | str, *, command: str,
                  passed: int | None = None, skipped: int | None = None,
                  failed: int | None = None) -> dict:
    """One pytest-layout section. A layout that did not run reports its real state."""
    section = tool_result(status, command=command)
    section["passed"] = passed
    section["skipped"] = skipped
    section["failed"] = failed
    return section


def section_status(section: object) -> str:
    """The status of a section, or ``INSUFFICIENT_EVIDENCE`` when there is none."""
    if isinstance(section, dict):
        value = section.get("status")
        if isinstance(value, str) and value in STATUSES:
            return value
        if value == "NOT_REQUESTED":
            return Status.SKIPPED.value
    return Status.INSUFFICIENT_EVIDENCE.value


def evidence_completeness(document: dict) -> tuple[str, list[str]]:
    """``(COMPLETE|INCOMPLETE, gaps)`` over :data:`MANDATORY_SECTIONS`.

    A section that is absent, unmeasured, skipped or failing is a GAP. This is the
    rule that stops "we did not run it" from reading as "it passed".
    """
    gaps: list[str] = []
    for name in MANDATORY_SECTIONS:
        status = section_status(document.get(name))
        if status in NON_AFFIRMATIVE:
            gaps.append(f"{name}={status}")
    return ("INCOMPLETE" if gaps else "COMPLETE"), gaps


def aggregate_status(document: dict) -> str:
    """The bundle's overall status. Never promotes a missing or failing result.

    * any mandatory section FAIL            -> ``FAIL``
    * any mandatory section missing/skipped -> ``INSUFFICIENT_EVIDENCE``
    * everything affirmative, warnings present (including an unexecuted optional live
      validation) -> ``PASS_WITH_WARNINGS``
    * everything affirmative and no warning -> ``PASS``
    """
    statuses = [section_status(document.get(name)) for name in MANDATORY_SECTIONS]
    if Status.FAIL.value in statuses:
        return Status.FAIL.value
    if any(s in NON_AFFIRMATIVE for s in statuses):
        return Status.INSUFFICIENT_EVIDENCE.value

    warnings = list(document.get("qualification", {}).get("warnings") or ())
    live = section_status(document.get("live_validation"))
    if live != Status.PASS.value:
        warnings.append("live_validation_not_established")
    if Status.PASS_WITH_WARNINGS.value in statuses:
        warnings.append("gate_reported_warnings")
    return (Status.PASS_WITH_WARNINGS.value if warnings else Status.PASS.value)


# ══════════════════════════════════════════════════════════════════════════════
#  Dependency profile hashes
# ══════════════════════════════════════════════════════════════════════════════
def profile_hash(text: str) -> str:
    """SHA-256 of a dependency profile's NORMALIZED content.

    Comments, blank lines, ordering and case are removed first, so a comment edit does
    not invalidate the hash while an added, removed or re-specified package always
    does. That makes the digest a statement about the dependency set, which is what a
    release consumer needs, rather than about the file's byte layout.
    """
    from core.dependency_authority import parse_requirement

    entries = []
    for line in text.splitlines():
        parsed = parse_requirement(line)
        if parsed is not None:
            entries.append(f"{parsed[0]}=={parsed[1]}".lower())
    digest = hashlib.sha256()
    for entry in sorted(entries):
        digest.update(entry.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def dependency_profile_hashes(requirements_dir: Path) -> dict[str, str]:
    """``{profile: sha256}`` over every declared profile. Deterministic and sorted."""
    from core.dependency_authority import PROFILES

    out: dict[str, str] = {}
    for name in sorted(PROFILES):
        path = requirements_dir / f"{name}.txt"
        if path.is_file():
            out[name] = profile_hash(path.read_text(encoding="utf-8"))
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  Assembly & atomic write
# ══════════════════════════════════════════════════════════════════════════════
def build_evidence(
    *,
    generated_at_utc: str,
    git_commit: str,
    git_branch: str,
    git_clean: bool,
    python_version: str,
    sections: dict,
    dependency_hashes: dict[str, str],
    artifact_checksums: dict[str, dict],
    sbom: dict[str, dict],
) -> dict:
    """Assemble the evidence document. Pure: no I/O, no clock, no environment read.

    ``generated_at_utc`` is injected rather than read from the clock so the assembly is
    testable and so a caller cannot accidentally embed a local timezone.
    """
    document: dict = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "product": PRODUCT,
        "canonical_version": VERSION,
        "release_stage": release_facts.RELEASE_STAGE,
        "release_stage_name": release_facts.RELEASE_STAGE_NAME,
        "release_tag": release_facts.RELEASE_TAG,
        "release_state": release_facts.RELEASE_STATE,
        "git_commit": git_commit,
        "git_branch": git_branch,
        "git_clean": bool(git_clean),
        "generated_at_utc": generated_at_utc,
        "python_version": python_version,
        "platform_category": platform_category(),
        "dependency_profile_hashes": dict(sorted(dependency_hashes.items())),
        "artifact_checksums": dict(sorted(artifact_checksums.items())),
        "sbom": dict(sorted(sbom.items())),
        "known_limitations": list(release_facts.KNOWN_LIMITATIONS),
        "declared_facts": release_facts.release_facts(),
    }
    for name in (*MANDATORY_SECTIONS, *OPTIONAL_SECTIONS):
        document[name] = sections.get(name) or {
            "status": Status.INSUFFICIENT_EVIDENCE.value,
            "detail": "not measured by this evidence run",
        }
    completeness, gaps = evidence_completeness(document)
    document["evidence_completeness"] = completeness
    document["evidence_gaps"] = gaps
    document["overall_status"] = aggregate_status(document)
    return document


def serialize(document: dict) -> str:
    """Canonical JSON: sorted keys, two-space indent, trailing newline.

    Deterministic so two evidence bundles from the same inputs are byte-identical.
    """
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


class EvidenceRejected(RuntimeError):
    """Evidence failed validation or sanitization. Nothing was written."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__(f"{len(self.problems)} problem(s)")


def write_evidence(document: dict, output_dir: Path, *,
                   filename: str = release_facts.EVIDENCE_FILENAME) -> Path:
    """Validate, sanitize and ATOMICALLY write ``document`` into ``output_dir``.

    Order matters: schema, then sanitization, then a build in a temporary directory,
    then the move. A rejection raises :class:`EvidenceRejected` and leaves
    ``output_dir`` byte-for-byte unchanged — no partial file, no half-written JSON, no
    ``.tmp`` residue for a later run to mistake for evidence.
    """
    problems = validate_evidence(document)
    problems += [f"private content: {p}" for p in scan_for_private_content(document)]
    if problems:
        raise EvidenceRejected(problems)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="m61_evidence_"))
    try:
        staged = staging / filename
        staged.write_text(serialize(document), encoding="utf-8")
        # Re-read and re-validate what is actually on disk: a serializer defect must
        # not be able to ship a document that only validated in memory.
        reloaded = json.loads(staged.read_text(encoding="utf-8"))
        recheck = validate_evidence(reloaded)
        recheck += [f"private content: {p}"
                    for p in scan_for_private_content(reloaded)]
        if recheck:
            raise EvidenceRejected(recheck)

        schema_staged = staging / EVIDENCE_SCHEMA_FILENAME
        schema_staged.write_text(
            json.dumps(EVIDENCE_JSON_SCHEMA, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")

        final = output_dir / filename
        shutil.move(str(staged), str(final))
        shutil.move(str(schema_staged), str(output_dir / EVIDENCE_SCHEMA_FILENAME))
        return final
    finally:
        shutil.rmtree(staging, ignore_errors=True)
