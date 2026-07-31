"""training_gym/graders/secret_grader.py — V69 M62: nothing private leaves the gym.

WHY THIS EXISTS
---------------
Every other grader asks whether the attempt was *good*. This one asks whether the
attempt is *safe to keep*, and it is mandatory for every task family in the gym
(:data:`~training_gym.task_spec.UNIVERSAL_MANDATORY_GRADERS`) for a reason that has
nothing to do with the model's competence: a trajectory becomes a teacher packet,
a bug report and eventually a training corpus. A credential that survives into any of
those is permanent in a way a wrong answer never is.

THE AUTHORITY IT USES
---------------------
:func:`~training_gym.schemas.scan_private_content`, which delegates to
``core.redaction_policy`` and ``core.memory_router`` — the scanners the rest of the
application already trusts. This grader deliberately owns no patterns of its own: a
second definition of "secret" would drift from the runtime's, and the direction it
drifts is always weaker.

WHAT IT NEVER DOES
------------------
It never records the matched value. A finding carries a CATEGORY, a sanitized location
and a count — which is enough for an operator to go and look, and not enough for the
finding itself to become the leak. This is why a naive implementation is dangerous:
attaching the offending substring to the evidence would copy the credential into the
very record that gets exported.

FAIL-CLOSED
-----------
``scan_private_content`` reports ``scanner_unavailable`` when ``core`` is not
importable and ``scanner_error`` when a scanner raised. Neither is "clean": both are
reported as INSUFFICIENT_EVIDENCE, because a scanner that did not run has proven
nothing. Zero surfaces scanned is likewise never a PASS.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from ..schemas import ResultStatus, Severity, scan_private_content
from ..task_spec import TaskFamily
from .base import (
    GRADER_PROTOCOL_VERSION,
    Grader,
    GraderContext,
    ToolProbe,
    blocking_failure,
    insufficient,
    make_result,
)

#: Largest artifact this grader will read into memory to scan. An artifact bigger than
#: this is reported as unscanned evidence rather than skipped silently.
MAX_SCANNED_ARTIFACT_BYTES = 1_048_576

#: Categories the underlying scanners use to report that they did not run. Neither may
#: ever be treated as a clean result.
SCANNER_FAILURE_CATEGORIES: frozenset[str] = frozenset(
    {"scanner_unavailable", "scanner_error"})


class SecretAndPIIGrader(Grader):
    """Scans every surface an attempt can carry private content on."""

    grader_id = "secret_pii"
    grader_version = f"{GRADER_PROTOCOL_VERSION}.secret_pii.1"
    supported_families: ClassVar[frozenset[TaskFamily]] = frozenset(TaskFamily)

    def availability(self, ctx: GraderContext) -> ToolProbe:
        """Whether the application's own scanners are reachable from here."""
        probe_categories = scan_private_content({"probe": "clean-string"})
        broken = sorted(set(probe_categories) & SCANNER_FAILURE_CATEGORIES)
        if broken:
            return ToolProbe(name="core.redaction_policy", available=False,
                             reason=f"the application's secret scanners are not usable "
                                    f"here ({', '.join(broken)}); an unscanned "
                                    f"trajectory is never clean")
        return ToolProbe(name="core.redaction_policy", available=True,
                         version=self.grader_version)

    def measure(self, ctx: GraderContext):
        probe = self.availability(ctx)
        if not probe.available:
            return self.unavailable(ctx, probe)

        surfaces = self._collect_surfaces(ctx)
        scanned = [s for s in surfaces if s["scanned"]]
        if not scanned:
            return insufficient(
                self, "no surface carried any content to scan; a trajectory nothing "
                      "was read from has not been cleared",
                findings=tuple(s for s in surfaces if not s["scanned"]))

        findings: list[dict] = []
        unreadable = [s for s in surfaces if not s["scanned"] and s.get("reason")]
        for surface in scanned:
            categories = scan_private_content(surface["payload"])
            broken = sorted(set(categories) & SCANNER_FAILURE_CATEGORIES)
            if broken:
                # The scanner worked during availability and failed on real content.
                # That is a scanner failure, not a clean surface.
                return insufficient(
                    self, f"the secret scanner failed while reading "
                          f"{surface['location']} ({', '.join(broken)}); an unscanned "
                          f"surface is never clean",
                    measured=len(scanned))
            for category in categories:
                findings.append({
                    "kind": "private_content",
                    "category": category,
                    "location": surface["location"],
                    "occurrences": 1,
                })

        for surface in unreadable:
            findings.append({"kind": "unscanned", "location": surface["location"],
                             "detail": surface["reason"]})

        measured = len(scanned)
        if any(f["kind"] == "private_content" for f in findings):
            categories = sorted({f["category"] for f in findings
                                 if f["kind"] == "private_content"})
            return blocking_failure(
                self,
                f"private content found in {len(findings)} location(s): "
                f"{', '.join(categories)}. The matched values are deliberately not "
                f"recorded — a finding that quotes the credential IS the leak.",
                measured=measured, findings=findings)

        if unreadable:
            return insufficient(
                self, f"{len(unreadable)} surface(s) could not be read; a partially "
                      f"scanned trajectory is not a cleared one",
                measured=measured, findings=findings)

        return make_result(
            self, ResultStatus.PASS, score=1.0, severity=Severity.INFO,
            measured=measured,
            evidence=(f"scanned {measured} surface(s): "
                      f"{', '.join(s['location'] for s in scanned[:8])}",))

    # -- surfaces --------------------------------------------------------------
    def _collect_surfaces(self, ctx: GraderContext) -> list[dict]:
        """Every place an attempt can carry private content, with a safe label.

        ``location`` is a task-relative description — ``answer``,
        ``tool_call[0].arguments``, ``artifact:output/report.json`` — never a host
        path, because this list is what a finding quotes."""
        surfaces: list[dict] = []

        def add(location: str, payload: Any) -> None:
            has_content = payload not in (None, "", {}, [], ())
            surfaces.append({"location": location, "payload": payload,
                             "scanned": bool(has_content), "reason": ""})

        add("answer", ctx.answer)
        if ctx.structured_output is not None:
            add("structured_output", _jsonable(ctx.structured_output))
        for index, call in enumerate(ctx.proposed_tool_calls):
            add(f"tool_call[{index}]", _jsonable(call))
        if ctx.diff_text:
            add("diff", ctx.diff_text)
        # Path STRINGS are scanned too: an absolute host path is private content even
        # when the file it names is not.
        if ctx.output_hashes:
            add("artifact_paths", sorted(ctx.output_hashes))

        for rel in sorted(ctx.output_hashes):
            surfaces.append(self._artifact_surface(ctx, rel))
        return surfaces

    def _artifact_surface(self, ctx: GraderContext, rel: str) -> dict:
        """Read one produced artifact, or record exactly why it could not be read."""
        location = f"artifact:{rel}"
        if not ctx.has_workspace():
            return {"location": location, "payload": None, "scanned": False,
                    "reason": "no workspace is attached, so the artifact was not read"}
        try:
            target = ctx.resolve(rel, "artifact")
        except Exception as exc:  # noqa: BLE001 — a rejected path is a finding
            return {"location": location, "payload": None, "scanned": False,
                    "reason": f"path refused ({type(exc).__name__})"}
        if not target.is_file():
            return {"location": location, "payload": None, "scanned": False,
                    "reason": "artifact is not a regular file"}
        try:
            size = target.stat().st_size
        except OSError:
            return {"location": location, "payload": None, "scanned": False,
                    "reason": "artifact is unreadable"}
        if size > MAX_SCANNED_ARTIFACT_BYTES:
            return {"location": location, "payload": None, "scanned": False,
                    "reason": f"artifact exceeds {MAX_SCANNED_ARTIFACT_BYTES} bytes"}
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {"location": location, "payload": None, "scanned": False,
                    "reason": "artifact could not be decoded"}
        return {"location": location, "payload": text, "scanned": bool(text),
                "reason": "" if text else "artifact is empty"}


def _jsonable(value: Any) -> Any:
    """A structure the recursive scanner can walk, without importing the payload's
    types. Anything exotic becomes its ``repr``, which is still scannable text."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


__all__ = ["MAX_SCANNED_ARTIFACT_BYTES", "SCANNER_FAILURE_CATEGORIES",
           "SecretAndPIIGrader"]
