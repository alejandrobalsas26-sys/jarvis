"""core/version.py — V69 M61.1: THE canonical version and release identity.

WHY THIS EXISTS
---------------
Before M61 the repository had no version source at all: ``pyproject.toml`` carried a
hand-written ``version = "63.0.0"`` while development had reached V69 M60, and nothing
in the runtime, the diagnostics or the release qualification could state which release
it was. A version that only one file knows about is a version nobody can verify.

This module is the single authority. Everything else DERIVES from it:

  * ``pyproject.toml``          — ``dynamic = ["version"]`` reading ``VERSION`` here;
  * the runtime / diagnostics   — :func:`release_identity`;
  * the release qualification   — :func:`release_identity` + :func:`milestone_tag`;
  * the documentation gate      — ``tests/test_version_v69_m611.py`` asserts the docs
    agree with these constants instead of with each other.

DISCIPLINE
----------
  * pure stdlib, zero imports from the rest of ``core`` — anything may import it,
    including ``setup``-time code running before dependencies exist;
  * no I/O, no clock, no environment read: the value is a build-time constant, so a
    diagnostics bundle produced on any host reports the same identity;
  * content-free: nothing here can carry a prompt, a secret or a private path.

VERSIONING SCHEME
-----------------
``MAJOR.MINOR.PATCH`` where MAJOR is the JARVIS generation (V69) and MINOR is the
milestone number within it (M61). PATCH is reserved for post-milestone fixes that do
not change the milestone's scope. This is the scheme the repository's own history
already used implicitly (V63 -> ``63.0.0``); M61 makes it explicit and checkable.
"""
from __future__ import annotations

# ── The canonical constants. Change these HERE and nowhere else. ─────────────
GENERATION = 69
MILESTONE = 61
PATCH = 0

VERSION = f"{GENERATION}.{MILESTONE}.{PATCH}"
"""PEP 440 version string. Consumed by pyproject.toml as dynamic metadata."""

__version__ = VERSION

MILESTONE_TAG = f"V{GENERATION} M{MILESTONE}"
"""Human-facing milestone identity, e.g. ``V69 M61``."""

RELEASE_NAME = "Production Stabilization, CI & Release Engineering"
"""What THIS milestone is. Stabilization release: no new capability subsystem."""

RELEASE_KIND = "stabilization"
"""``stabilization`` or ``capability``. M61 adds no operational capability."""

# One sentence, used verbatim by the READMEs and package metadata so the three
# cannot drift into three different claims about what JARVIS is.
PROJECT_SUMMARY = (
    "Local-first, operator-controlled AI workstation and authorized Purple Team/SOC "
    "homelab platform."
)


def milestone_tag() -> str:
    """The milestone identity string, e.g. ``V69 M61``."""
    return MILESTONE_TAG


def release_identity() -> dict:
    """Bounded, content-free release identity for diagnostics and qualification.

    Every value is a build-time constant, so this is safe to embed in a redacted
    diagnostics bundle or a CI artifact.
    """
    return {
        "version": VERSION,
        "generation": GENERATION,
        "milestone": MILESTONE,
        "patch": PATCH,
        "milestone_tag": MILESTONE_TAG,
        "release_name": RELEASE_NAME,
        "release_kind": RELEASE_KIND,
        "summary": PROJECT_SUMMARY,
    }


def version_tuple() -> tuple[int, int, int]:
    """``(GENERATION, MILESTONE, PATCH)`` for ordered comparisons."""
    return (GENERATION, MILESTONE, PATCH)
