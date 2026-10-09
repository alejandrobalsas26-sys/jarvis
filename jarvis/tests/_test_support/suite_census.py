"""V69 M68D.2 — what a test suite ACTUALLY did, on the runner that ran it.

WHY THIS EXISTS
===============
M68D.1 guarded against skip-washing with an arithmetic ratio::

    assert passed >= 8 * skipped

On the real Windows runner that assertion could not be satisfied by ANY result.
The H02 matrix is collected DYNAMICALLY — its mechanism parametrisations come
from measured platform capabilities — so Windows collected **55** cases where
Linux collects 60, and 9 of those 55 were expected capability skips. The
assertion then demanded at least **72** passes out of 55 collected cases: an
impossible bar, and one that failed for a reason entirely unrelated to the
security property it was defending (M68D.2 §14).

THE REPLACEMENT
===============
A skip budget has to be stated against something that cannot be exceeded by
construction. :func:`skip_budget` derives it from the collection the runner
actually produced, so it is satisfiable at every collection size, and
:func:`ratio_is_satisfiable` exists so a test can PROVE the old form was not.

Counts come from pytest's own summary rather than from a hand-maintained table,
and skip REASONS are captured too: a skip that cannot be traced to a named,
measured primitive is skip-washing whatever the numbers say (§15).
"""
from __future__ import annotations

import math
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["SuiteCensus", "census", "collected_node_ids", "skip_budget",
           "ratio_is_satisfiable"]

_JARVIS_ROOT = Path(__file__).resolve().parent.parent.parent

#: Fraction of the ACTUAL collection that may be behind a capability gate.
#: One quarter: generous enough that a platform missing several mechanisms is
#: not a failure, tight enough that a suite cannot be hollowed out. Satisfiable
#: at every collection size, which is the property the ratio it replaced lacked.
DEFAULT_SKIP_FRACTION = 0.25


@dataclass(frozen=True)
class SuiteCensus:
    """One suite's measured outcome. Every field comes from pytest's report."""

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    xfailed: int = 0
    xpassed: int = 0
    #: The reason text pytest printed for each skip, with its count.
    skip_reasons: "dict[str, int]" = field(default_factory=dict)
    #: pytest's own last line, kept verbatim for failure messages.
    summary: str = ""

    @property
    def collected(self) -> int:
        """Every case pytest reported an outcome for.

        Derived from the outcomes rather than from a separate ``--collect-only``
        run, so it cannot disagree with the counts it is compared against.
        """
        return (self.passed + self.failed + self.skipped + self.errors
                + self.xfailed + self.xpassed)

    @property
    def ran(self) -> int:
        """Cases that actually executed, as opposed to being skipped."""
        return self.collected - self.skipped


_OUTCOME = re.compile(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed)")
#: pytest renders a skip as ``SKIPPED [n] <path>:<lineno>: <reason>``. The
#: line number sits between two colons, which is what an earlier form of this
#: pattern missed — it matched nothing at all and reported every suite as
#: having no skip reasons, which would have made the reason contract vacuous.
_SKIP_LINE = re.compile(r"^SKIPPED \[(\d+)\] (.+?):(\d+): (.*)$")


def census(target: str, *, root: "Path | None" = None) -> SuiteCensus:
    """Run *target* in a FRESH interpreter and report what happened.

    A fresh process because the capability probe runs at import time: the counts
    have to be the ones a real runner would see, not ones the calling session
    has already imported and cached.
    """
    done = subprocess.run(  # nosec B603 - fixed argv, shell=False
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-rs",
         "-p", "no:cacheprovider", target],
        cwd=str(root or _JARVIS_ROOT), capture_output=True, text=True,
        check=False)
    stdout = done.stdout or ""
    lines = stdout.strip().splitlines()
    summary = lines[-1] if lines else "<no output>"

    counts: "dict[str, int]" = {}
    for number, label in _OUTCOME.findall(summary):
        counts[label.rstrip("s") if label.startswith("error") else label] = int(number)

    reasons: "dict[str, int]" = {}
    for line in lines:
        match = _SKIP_LINE.match(line.strip())
        if match:
            reason = match.group(4).strip()
            reasons[reason] = reasons.get(reason, 0) + int(match.group(1))

    return SuiteCensus(
        passed=counts.get("passed", 0), failed=counts.get("failed", 0),
        skipped=counts.get("skipped", 0), errors=counts.get("error", 0),
        xfailed=counts.get("xfailed", 0), xpassed=counts.get("xpassed", 0),
        skip_reasons=reasons, summary=summary)


def skip_budget(collected: int,
                fraction: float = DEFAULT_SKIP_FRACTION) -> int:
    """The most cases that may be skipped out of *collected*.

    Always in ``range(0, collected + 1)``, so the bound is satisfiable at every
    collection size — including the degenerate ones. That is the whole point:
    the assertion it replaces could demand more passes than there were cases.
    """
    if collected <= 0:
        return 0
    return max(1, min(collected, math.floor(collected * fraction)))


def ratio_is_satisfiable(collected: int, multiple: int, skipped: int) -> bool:
    """Could ``passed >= multiple * skipped`` EVER hold at this collection?

    Evaluated at the most favourable possible result — zero failures, so every
    unskipped case passes. ``False`` means no outcome whatsoever satisfies the
    assertion, which is what M68D.1's ``passed >= 8 * skipped`` became once the
    Windows matrix collected 55 cases with 9 capability skips (§14).
    """
    return (collected - skipped) >= multiple * skipped


def collected_node_ids(target: str, *, root: "Path | None" = None) -> "list[str]":
    """Every node id pytest COLLECTS from *target*. Nothing is executed.

    Collection-only on purpose: a suite that needs to assert its own mandatory
    inventory cannot run itself in a subprocess, because the subprocess would
    reach the same assertion and recurse without bound. Collection answers the
    question that matters anyway — is this invariant present and ungated on
    this platform — without executing anything.
    """
    done = subprocess.run(  # nosec B603 - fixed argv, shell=False
        [sys.executable, "-m", "pytest", "-q", "--collect-only",
         "-p", "no:cacheprovider", target],
        cwd=str(root or _JARVIS_ROOT), capture_output=True, text=True,
        check=False)
    return [line.strip() for line in (done.stdout or "").splitlines()
            if "::" in line and not line.startswith(" ")]
