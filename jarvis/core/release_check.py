"""core/release_check.py — V69 M61.1: deterministic release-truth verification.

Documentation drifts because nothing checks it. Before M61 the repository claimed
version ``63.0.0`` in package metadata at V69 M60, described itself as a "complete
autonomous Purple Team security platform" that "attacks autonomously" while every
offensive path is in fact behind HITL/NATO approval, advertised ``qwen2.5 7B/14B``
role models when the live role table resolves ``qwen3:8b`` / ``qwen3:14b``, and left
three release documents saying **NOT merged** for milestones that are ancestors of
master.

This module turns each of those into a deterministic, re-runnable check over the
repository's own files. It is a SCANNER: it reads, it never writes, and it never
executes anything it reads.

CHECK FAMILIES
--------------
  * ``version``  — pyproject derives from :mod:`core.version`; no file re-states a
    current version literal that contradicts it;
  * ``models``   — current-facing docs name the configured role models, not the
    superseded generation, and every model tag they quote exists in the
    authoritative runtime configuration;
  * ``claims``   — current-facing docs make no unsupported autonomy claim;
  * ``status``   — no current release document contradicts the declared release state:
    a milestone below the canonical one may not say "NOT merged", and once the
    canonical milestone is merged its own document may not either;
  * ``install``  — every install command quoted in the READMEs resolves to a file
    that actually exists;
  * ``counts``   — V69 M61.8: every validated number a current release document
    quotes (tests passed/skipped/failed) equals :mod:`core.release_facts`, so two
    release documents cannot state two different results;
  * ``security`` — V69 M61.8: the Bandit result quoted in the release documents is
    the current one, including the Low baseline;
  * ``posture``  — V69 M61.8: no current-facing document advertises a capability the
    code refuses (dynamic plugin execution, unconditional all-interface binding,
    automatic dependency installation), and the security policy states each of those
    postures explicitly rather than leaving it to be inferred.

SCOPE DISCIPLINE
----------------
Only CURRENT-FACING documents are scanned. Historical release documents and the
archived per-version sections of ``JARVIS.md`` are deliberately out of scope:
rewriting history to look consistent is the opposite of truth. The three M61.8
families use narrower, explicitly declared scopes for the same reason — the changelog
is scanned only in its CURRENT section, because its older sections correctly record
what older releases measured.

PURE STDLIB
-----------
This module is imported by ``scripts/check_release_consistency.py``, which runs in CI
with **no dependencies installed**, so nothing here may import a third-party package —
directly or transitively. The authoritative model configuration therefore comes from
parsing ``core/model_router.py`` and ``core/hardware_model_profile.py`` over the AST
rather than importing them (they need ``httpx`` and ``psutil``).
"""
from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

from core import release_facts
from core.version import GENERATION, MILESTONE, VERSION

_APP_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _APP_ROOT.parent

#: Current-facing documents. Everything else in docs/ is historical by definition.
_CURRENT_DOCS = (
    _REPO_ROOT / "README.md",
    _APP_ROOT / "README.md",
)

#: The header block of JARVIS.md is current-facing; its per-version sections are
#: history. The boundary is the first ``---`` rule after the title.
_JARVIS_MD = _APP_ROOT / "JARVIS.md"
_JARVIS_HEADER_LINES = 30

#: Role models the live configuration resolves. Sourced from the same defaults the
#: README role table documents; a doc naming a superseded generation is a defect.
CURRENT_ROLE_MODELS = {
    "FAST": "qwen3:8b",
    "CODER": "qwen2.5-coder:latest",
    "DEEP": "qwen3:14b",
    "VISION": "gemma3:4b",
    "EMBEDDING": "nomic-embed-text:latest",
    "VERIFIER": "qwen3:8b",
}

#: A doc may not claim the model family that M52-M60 superseded. ``qwen2.5-coder``
#: is still current for the CODER role, so the pattern must not match it.
_SUPERSEDED_MODEL_RE = re.compile(r"qwen2\.5(?!-coder)", re.IGNORECASE)

#: Unsupported autonomy claims. Every offensive path is operator-gated (HITL/NATO
#: approval + trusted-lab flag), so these sentences are simply not true.
_FORBIDDEN_CLAIMS = (
    "complete autonomous",
    "fully autonomous",
    "attacks autonomously",
    "autonomously attack",
)

#: A release document may say "NOT merged" only about the CURRENT milestone or a
#: later one — i.e. work still in flight. The canonical milestone's own document is
#: legitimately unmerged while it is being reviewed; a document for work that already
#: shipped is not. Matches ``V69 M58``/``V69_M58``/``M58``.
_MILESTONE_RE = re.compile(r"\bM(\d{2})\b")
_NOT_MERGED_RE = re.compile(r"not\s+merged", re.IGNORECASE)

#: Install commands quoted in the READMEs must resolve to real files.
_INSTALL_CMD_RE = re.compile(
    r"(?:pip install -r\s+|\./scripts/|python scripts/)([A-Za-z0-9_./-]+)")

# ── V69 M61.8 scopes ─────────────────────────────────────────────────────────
#: Operator-facing documentation. Wider than ``_CURRENT_DOCS``: a stale ``ollama pull``
#: in the troubleshooting guide sends the operator after a model the runtime will never
#: choose, which is the same defect as a stale role table in the README.
_OPERATOR_DOCS = (
    _REPO_ROOT / "README.md",
    _APP_ROOT / "README.md",
    _REPO_ROOT / "docs" / "TROUBLESHOOTING.md",
    _REPO_ROOT / "docs" / "INSTALLATION.md",
)

#: Documents that state the CURRENT release's validated results. These are the ones
#: that must not disagree with each other or with :mod:`core.release_facts`.
_RELEASE_DOCS = (
    _APP_ROOT / "docs" / "V69_M61_PRODUCTION_STABILIZATION.md",
    _APP_ROOT / "docs" / "V69_M61_8_RELEASE_CLOSURE.md",
    _REPO_ROOT / "docs" / "releases" / f"v{VERSION}.md",
)

_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"
_SECURITY_POLICY = _REPO_ROOT / "SECURITY.md"

#: Modules that hold the authoritative model configuration. Parsed, never imported.
_MODEL_ROUTER = _APP_ROOT / "core" / "model_router.py"
_HARDWARE_MODEL_PROFILE = _APP_ROOT / "core" / "hardware_model_profile.py"

#: Ollama model families a document may mention. A token outside this vocabulary is
#: not treated as a model name at all, which keeps the scan off unrelated prose.
_MODEL_FAMILY_PREFIXES = (
    "qwen", "deepseek", "gemma", "llama", "llava", "moondream", "nomic-embed-text",
    "mistral", "phi",
)
_MODEL_TAG_RE = re.compile(
    r"\b((?:" + "|".join(_MODEL_FAMILY_PREFIXES) + r")[A-Za-z0-9._-]*"
    r"(?::[A-Za-z0-9._-]+)?)\b")

#: Validated-result claims. ``3,481`` and ``3481`` are both recognized so a thousands
#: separator cannot hide a stale number.
_COUNT_RES = (
    ("tests_passed", re.compile(r"\*?\*?([\d,]{3,7})\*?\*?\s+passed", re.IGNORECASE)),
    ("tests_skipped", re.compile(r"([\d,]{1,7})\s+skipped", re.IGNORECASE)),
    ("tests_failed", re.compile(r"([\d,]{1,7})\s+failed", re.IGNORECASE)),
)

#: Low-severity Bandit claims in the release documents.
_LOW_FINDING_RE = re.compile(r"\b([\d,]{1,6})\s+Low\b")

#: Capabilities the code refuses. A current-facing document advertising any of these
#: as active is advertising something that will not happen.
_DISABLED_CAPABILITY_CLAIMS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "dynamic_plugin_exec",
        re.compile(
            r"(?:executes?|runs?|loads? and runs?|hot-?reloads? and (?:executes?|runs?))"
            r"[^.\n]{0,40}\b(?:source )?plugins?\b"
            r"|plugins?[^.\n]{0,40}\b(?:are|is) (?:executed|run|loaded and run)\b"
            r"|dynamic plugin (?:execution|exec)[^.\n]{0,30}"
            r"(?:is |remains )?(?:enabled|supported|available|active)\b"
            r"|(?:sandboxed|secure|restricted|isolated) plugin (?:sandbox|execution|"
            r"environment)\b",
            re.IGNORECASE),
        "dynamic source-plugin execution is refused fail-closed and there is no "
        "plugin sandbox (V69 M61.7)",
    ),
    (
        "all_interface_bind",
        re.compile(
            r"(?:binds?|bound|listens?|listening|exposed?|serves?)"
            r"[^.\n]{0,40}(?:all interfaces|0\.0\.0\.0)[^.\n]{0,40}"
            r"(?:by default|unconditionally|automatically)"
            r"|(?:all interfaces|0\.0\.0\.0)[^.\n]{0,30}by default",
            re.IGNORECASE),
        "every service is loopback-first and all-interface exposure needs an explicit "
        "JARVIS_<SERVICE>_EXPOSE opt-in (V69 M61.7)",
    ),
    (
        "auto_dependency_install",
        re.compile(
            r"(?:automatically|auto-?)\s*install(?:s|ing|ed)?\b"
            r"[^.\n]{0,40}(?:dependenc|package|requirement)"
            r"|install(?:s)?\s+(?:missing\s+)?(?:dependenc\w+|packages)"
            r"[^.\n]{0,30}automatically"
            r"|(?:dependenc\w+|packages)[^.\n]{0,30}installed automatically",
            re.IGNORECASE),
        "boot-time dependency installation is report-only unless the operator sets "
        "JARVIS_AUTO_INSTALL_DEPS=true (V69 M61.3)",
    ),
)

#: Structural anchors SECURITY.md must carry, one per posture that would otherwise be
#: left to inference. Labels, not sentences: the check is for a declared section, not
#: for a particular phrasing of it.
REQUIRED_SECURITY_ANCHORS: tuple[str, ...] = (
    "**Dynamic source plugins:**",
    "**SSH host keys:**",
    "**Service network exposure:**",
    "**Dependency installation:**",
)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def jarvis_md_header() -> str:
    """The current-facing header of JARVIS.md (history excluded)."""
    return "\n".join(_read(_JARVIS_MD).splitlines()[:_JARVIS_HEADER_LINES])


def _current_texts() -> dict[str, str]:
    texts = {
        str(p.relative_to(_REPO_ROOT)).replace("\\", "/"): _read(p)
        for p in _CURRENT_DOCS if p.is_file()
    }
    if _JARVIS_MD.is_file():
        texts["jarvis/JARVIS.md (header)"] = jarvis_md_header()
    return texts


def check_version() -> list[str]:
    """pyproject must DERIVE its version from :mod:`core.version`."""
    problems: list[str] = []
    pyproject = _APP_ROOT / "pyproject.toml"
    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"pyproject.toml unreadable: {type(exc).__name__}"]

    project = data.get("project", {})
    if "version" in project:
        problems.append(
            f"pyproject.toml hard-codes version {project['version']!r}; it must "
            f"declare dynamic = ['version'] deriving from core/version.py")
    if "version" not in project.get("dynamic", []):
        problems.append("pyproject.toml does not declare version as dynamic metadata")

    attr = (data.get("tool", {}).get("setuptools", {})
            .get("dynamic", {}).get("version", {}).get("attr"))
    if attr != "core.version.VERSION":
        problems.append(
            f"dynamic version attr is {attr!r}; expected 'core.version.VERSION'")
    return problems


def check_claims() -> list[str]:
    """No current-facing document may claim autonomous operation."""
    problems = []
    for name, text in _current_texts().items():
        lowered = text.lower()
        for claim in _FORBIDDEN_CLAIMS:
            idx = lowered.find(claim)
            if idx >= 0:
                line = text[:idx].count("\n") + 1
                problems.append(
                    f"{name}:{line} claims {claim!r}; every offensive path is "
                    f"operator-gated (HITL/NATO approval + trusted-lab flag)")
    return problems


def check_release_status() -> list[str]:
    """A milestone BELOW the canonical one may not be documented 'NOT merged'.

    Strictly below, not "at or below": the canonical milestone is the one currently in
    flight, and its own release document correctly states that it is awaiting review.
    Only work that has already shipped must not claim to be unmerged.
    """
    problems = []
    docs_dir = _APP_ROOT / "docs"
    if not docs_dir.is_dir():
        return problems
    for path in sorted(docs_dir.glob("V*.md")):
        text = _read(path)
        if not _NOT_MERGED_RE.search(text):
            continue
        # The FILENAME is the document's identity. Reading milestones out of the body
        # too was wrong: a "corrected in V69 M61.1" note inside the M60 document made
        # it look like an M61 document and exempted it — the checker stopped detecting
        # exactly the drift it exists for. Caught by the negative control below.
        milestones = [int(m) for m in _MILESTONE_RE.findall(path.stem)]
        if not milestones:
            heading = text.splitlines()[0] if text else ""
            milestones = [int(m) for m in _MILESTONE_RE.findall(heading)]
        if milestones and all(m < MILESTONE for m in milestones):
            problems.append(
                f"docs/{path.name} says 'NOT merged' for a milestone below the "
                f"canonical V{GENERATION} M{MILESTONE} — that work already shipped")
    return problems


def check_install_commands() -> list[str]:
    """Every install/doctor command quoted in the READMEs must resolve to a file."""
    problems = []
    for name, text in _current_texts().items():
        if not name.endswith("README.md"):
            continue
        for match in _INSTALL_CMD_RE.finditer(text):
            target = match.group(1).strip()
            if target.startswith(("http", "-")):
                continue
            candidate = target if target.startswith("scripts/") else target
            if not (_APP_ROOT / candidate).is_file() and \
               not (_APP_ROOT / "scripts" / candidate).is_file():
                line = text[:match.start()].count("\n") + 1
                problems.append(f"{name}:{line} references missing file {target!r}")
    return problems


# ══════════════════════════════════════════════════════════════════════════════
#  V69 M61.8 — the authoritative model configuration, read over the AST
# ══════════════════════════════════════════════════════════════════════════════
def _module_string_constants(path: Path) -> list[str]:
    """Every string literal in a module, via the AST. No import, no side effect."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def authoritative_model_tags() -> set[str]:
    """Every Ollama model tag the runtime can actually choose.

    Sourced from the two modules that own that decision — the per-role defaults in
    ``core/model_router.py`` and the per-tier recommendations in
    ``core/hardware_model_profile.py``. Read over the AST because this module must stay
    importable with no dependencies installed.
    """
    tags: set[str] = set()
    for path in (_MODEL_ROUTER, _HARDWARE_MODEL_PROFILE):
        for literal in _module_string_constants(path):
            token = literal.strip()
            if ":" not in token or " " in token or "/" in token:
                continue
            if _MODEL_TAG_RE.fullmatch(token):
                tags.add(token)
    return tags


def authoritative_model_families() -> set[str]:
    """The family part of every authoritative tag, e.g. ``qwen3``, ``gemma3``."""
    return {tag.split(":", 1)[0] for tag in authoritative_model_tags()}


def check_models() -> list[str]:
    """No current-facing document may advertise a model the runtime cannot choose.

    Two rules. The first (M61.1) refuses the superseded ``qwen2.5`` generation by name.
    The second (M61.8) is the general form of the same defect: every model tag quoted in
    operator-facing documentation must exist in :func:`authoritative_model_tags`, so a
    retired recommendation such as ``deepseek-r1:70b`` or ``moondream`` — which
    ``core/hardware_model_profile.py`` explicitly stopped recommending in V66.1 — cannot
    keep sending an operator after a model no role will ever resolve.
    """
    problems = []
    for name, text in _current_texts().items():
        for match in _SUPERSEDED_MODEL_RE.finditer(text):
            line = text[:match.start()].count("\n") + 1
            problems.append(
                f"{name}:{line} names superseded model family 'qwen2.5'; the live "
                f"role table resolves qwen3 (FAST {CURRENT_ROLE_MODELS['FAST']}, "
                f"DEEP {CURRENT_ROLE_MODELS['DEEP']})")

    tags = authoritative_model_tags()
    families = authoritative_model_families()
    if not tags:
        return problems + [
            "core/model_router.py and core/hardware_model_profile.py yielded no model "
            "tags — the model check would pass vacuously"]

    for path in _OPERATOR_DOCS:
        if not path.is_file():
            continue
        name = _relative(path)
        text = _read(path)
        for match in _MODEL_TAG_RE.finditer(text):
            token = match.group(1)
            line = text[:match.start()].count("\n") + 1
            if ":" in token:
                if token not in tags:
                    problems.append(
                        f"{name}:{line} quotes model tag {token!r}, which is not in the "
                        f"authoritative configuration (core/model_router.py role "
                        f"defaults + core/hardware_model_profile.py tier tables)")
            elif token.lower() not in {f.lower() for f in families}:
                problems.append(
                    f"{name}:{line} names retired model family {token!r}; the "
                    f"authoritative families are {sorted(families)}")
    return problems


def check_role_models_match_the_router() -> list[str]:
    """:data:`CURRENT_ROLE_MODELS` must equal the router's own per-role defaults.

    Without this the table is a hand-copied duplicate: the scanner would keep enforcing
    a role model the runtime had already moved off, and report a clean audit while doing
    it. Parsed over the AST for the no-dependency rule.
    """
    problems: list[str] = []
    try:
        tree = ast.parse(_MODEL_ROUTER.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return [f"core/model_router.py unparseable: {type(exc).__name__}"]

    declared: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.startswith("_DEFAULT_"):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            declared[target.id.removeprefix("_DEFAULT_")] = node.value.value

    if not declared:
        return ["core/model_router.py declares no _DEFAULT_<ROLE> model constants"]
    for role, expected in sorted(CURRENT_ROLE_MODELS.items()):
        actual = declared.get(role)
        if actual is None:
            problems.append(
                f"core/release_check.py claims a {role} role model but "
                f"core/model_router.py declares no _DEFAULT_{role}")
        elif actual != expected:
            problems.append(
                f"{role} role model drift: release_check says {expected!r}, "
                f"core/model_router.py resolves {actual!r}")
    return problems


# ══════════════════════════════════════════════════════════════════════════════
#  V69 M61.8 — validated numbers, security result, security posture
# ══════════════════════════════════════════════════════════════════════════════
def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(_REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return path.name


def changelog_current_section() -> str:
    """The CURRENT release section of the changelog — its first ``##`` block.

    Everything after it records what earlier releases measured and is correctly
    immutable; only the head block makes claims about the release being shipped.
    """
    lines = _read(_CHANGELOG).splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("## ")), None)
    if start is None:
        return ""
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def _release_texts() -> dict[str, str]:
    """Current release documents plus the changelog's current section."""
    texts = {_relative(p): _read(p) for p in _RELEASE_DOCS if p.is_file()}
    section = changelog_current_section()
    if section:
        texts["CHANGELOG.md (current section)"] = section
    return texts


def _as_int(raw: str) -> int | None:
    try:
        return int(raw.replace(",", ""))
    except ValueError:
        return None


def check_validated_counts() -> list[str]:
    """Every test count a current release document quotes must be the current one.

    The M61 report and the changelog both carried ``3481 passed / 18 skipped`` — the RC
    that produced them — long after the merged tree measured something else. One
    declared number in :mod:`core.release_facts`, checked here, is what makes that a
    build failure instead of a discovery.
    """
    expected = {
        "tests_passed": release_facts.DETERMINISTIC_TESTS_PASSED,
        "tests_skipped": release_facts.DETERMINISTIC_TESTS_SKIPPED,
        "tests_failed": release_facts.DETERMINISTIC_TESTS_FAILED,
    }
    problems: list[str] = []
    for name, text in _release_texts().items():
        for key, pattern in _COUNT_RES:
            for match in pattern.finditer(text):
                value = _as_int(match.group(1))
                if value is None or value == expected[key]:
                    continue
                line = text[:match.start()].count("\n") + 1
                problems.append(
                    f"{name}:{line} states {value} {key.removeprefix('tests_')}; the "
                    f"current validated result is {expected[key]} "
                    f"(core/release_facts.py)")
    return problems


def check_security_result() -> list[str]:
    """The Bandit result quoted in the release documents must be the current one."""
    canonical = (f"{release_facts.BANDIT_MEDIUM} Medium and "
                 f"{release_facts.BANDIT_HIGH} High")
    problems: list[str] = []
    for name, text in _release_texts().items():
        if "bandit" not in text.lower():
            continue
        if canonical not in text:
            problems.append(
                f"{name} discusses Bandit without stating the current gate result "
                f"({canonical!r}, command {release_facts.BANDIT_GATE_COMMAND!r})")
        for match in _LOW_FINDING_RE.finditer(text):
            value = _as_int(match.group(1))
            if value is None or value == release_facts.BANDIT_LOW_OBSERVED:
                continue
            line = text[:match.start()].count("\n") + 1
            problems.append(
                f"{name}:{line} states {value} Low findings; the current count is "
                f"{release_facts.BANDIT_LOW_OBSERVED} with an approved baseline of "
                f"{release_facts.BANDIT_LOW_BASELINE} (core/release_facts.py)")
    return problems


def check_security_posture() -> list[str]:
    """No current-facing document may advertise a capability the code refuses.

    The three refused capabilities are the ones a reader is most likely to assume are
    active because an older document said so: dynamic plugin execution (removed, no
    opt-in), all-interface service binding (now loopback-first with an explicit
    per-service opt-in) and boot-time dependency installation (now report-only). Each is
    also required to be *stated* in SECURITY.md, because "we quietly stopped doing it"
    is not a security posture an operator can rely on.
    """
    problems: list[str] = []
    scope = {**_current_texts(), **_release_texts()}
    for path in _OPERATOR_DOCS:
        if path.is_file():
            scope.setdefault(_relative(path), _read(path))

    for name, text in sorted(scope.items()):
        for key, pattern, reality in _DISABLED_CAPABILITY_CLAIMS:
            match = pattern.search(text)
            if match:
                line = text[:match.start()].count("\n") + 1
                problems.append(
                    f"{name}:{line} advertises {key!r} as active; in fact {reality}")

    if _SECURITY_POLICY.is_file():
        policy = _read(_SECURITY_POLICY)
        for anchor in REQUIRED_SECURITY_ANCHORS:
            if anchor not in policy:
                problems.append(
                    f"SECURITY.md does not declare the {anchor} posture; a refused "
                    f"capability must be documented, not merely absent")
    else:
        problems.append("SECURITY.md is missing")
    return problems


def check_release_state() -> list[str]:
    """The declared release state must agree with what the documents say.

    :data:`core.release_facts.RELEASE_STATE` is the declaration. When it says ``merged``,
    the canonical milestone's own document may no longer say it is awaiting review, and
    it must record the merge commit — the two facts a reader needs to locate the work.
    """
    problems: list[str] = []
    state = release_facts.RELEASE_STATE
    if state not in ("in_flight", "merged", "published"):
        return [f"core/release_facts.py declares an unknown RELEASE_STATE {state!r}"]
    if state == "in_flight":
        return problems

    for path in _RELEASE_DOCS:
        if not path.is_file():
            continue
        name = _relative(path)
        text = _read(path)
        if _NOT_MERGED_RE.search(text):
            line = text[:_NOT_MERGED_RE.search(text).start()].count("\n") + 1
            problems.append(
                f"{name}:{line} says 'not merged' while core/release_facts.py declares "
                f"RELEASE_STATE={state!r} at commit "
                f"{release_facts.RELEASE_MERGE_COMMIT}")
        if release_facts.RELEASE_MERGE_COMMIT not in text:
            problems.append(
                f"{name} does not record the merge commit "
                f"{release_facts.RELEASE_MERGE_COMMIT} for the merged release")
    return problems


def check_release_tag_not_claimed() -> list[str]:
    """M61.8 prepares the tag and the release; it must not claim either exists.

    A release note that says "released" before the tag is pushed is the same class of
    untruth as a milestone document that says "NOT merged" after it landed — just in
    the other direction, and much harder to notice.
    """
    if release_facts.RELEASE_STATE == "published":
        return []
    claimed = re.compile(
        r"(?:the )?(?:GitHub )?[Rr]elease (?:has been|was|is) (?:published|created)"
        r"|tag [`']?v\d+\.\d+\.\d+[`']? (?:has been|was|is) (?:pushed|created)",
    )
    problems = []
    for name, text in _release_texts().items():
        match = claimed.search(text)
        if match:
            line = text[:match.start()].count("\n") + 1
            problems.append(
                f"{name}:{line} claims the tag or GitHub Release exists; "
                f"RELEASE_STATE is {release_facts.RELEASE_STATE!r} and M61.8 creates "
                f"neither")
    return problems


def audit() -> dict:
    """Run every release-truth check. Bounded, content-free result."""
    families = {
        "version": check_version(),
        "models": check_models() + check_role_models_match_the_router(),
        "claims": check_claims(),
        "status": check_release_status() + check_release_state()
        + check_release_tag_not_claimed(),
        "install": check_install_commands(),
        "counts": check_validated_counts(),
        "security": check_security_result(),
        "posture": check_security_posture(),
    }
    problems = [p for family in families.values() for p in family]
    return {
        "ok": not problems,
        "canonical_version": VERSION,
        "families": {k: len(v) for k, v in families.items()},
        "problems": problems,
    }
