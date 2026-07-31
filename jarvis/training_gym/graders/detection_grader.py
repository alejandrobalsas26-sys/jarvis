"""training_gym/graders/detection_grader.py — V69 M62: does the rule actually parse.

WHY THIS EXISTS
---------------
A detection rule is the one artefact in this gym that a human cannot eyeball for
correctness. Sigma, YARA and Suricata all accept text that looks exactly like a working
rule and is not one: a condition referencing a string identifier that was never
defined, a logsource nobody ships, a ``sid`` that collides, a ``meta`` block with
``author: TODO``. A model that produces those is producing something worse than
nothing — a detection that silently never fires.

THE RULE THAT SHAPES THIS FILE
------------------------------
**A PASS requires the real validator.** Structural checks in this module can prove a
rule is BROKEN; they can never prove it is valid, because the only authority on that is
the engine that will run it. When the engine is absent the verdict is SKIPPED, or
INSUFFICIENT_EVIDENCE when the task made this grader mandatory — both of which block —
and never PASS. A syntactic screen that reported PASS in the engine's absence would be
teaching the corpus that "looks like Sigma" is the standard.

DEFENSIVE ONLY
--------------
Nothing here runs a rule against a live target, a live network or a production feed.
Fixture evaluation, where the engine supports it, runs against files the task staged
inside the disposable workspace and nothing else.
"""
from __future__ import annotations

import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
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
    probe_tool,
    run_bounded,
)

#: ``family -> (tool name, version argv, rule file suffix)``.
RULE_TOOLING: dict[TaskFamily, tuple[str, tuple[str, ...], str]] = {
    TaskFamily.SIGMA_RULE: ("sigma", ("--version",), ".yml"),
    TaskFamily.YARA_RULE: ("yara", ("--version",), ".yar"),
    TaskFamily.SURICATA_RULE: ("suricata", ("-V",), ".rules"),
}

#: Metadata a rule of each type cannot be complete without.
REQUIRED_FIELDS: dict[TaskFamily, tuple[str, ...]] = {
    TaskFamily.SIGMA_RULE: ("title", "id", "status", "description", "logsource",
                            "detection", "level"),
    TaskFamily.YARA_RULE: ("meta", "condition"),
    TaskFamily.SURICATA_RULE: ("msg", "sid", "rev", "classtype"),
}

#: Values that mean the author never filled the field in.
_PLACEHOLDER_RE = re.compile(
    r"(?i)\b(?:todo|fixme|changeme|placeholder|tbd|your[_ -]?(?:value|name|domain)|"
    r"xxxx+|lorem ipsum)\b|<[a-z_ ]{3,32}>")
#: A MITRE ATT&CK technique id, with or without a sub-technique.
_ATTACK_TAG_RE = re.compile(r"(?i)^attack\.t\d{4}(?:\.\d{3})?$")
_ATTACK_ANY_RE = re.compile(r"(?i)^attack\.")
_SURICATA_ACTION_RE = re.compile(r"^\s*(?:alert|drop|pass|reject|rejectsrc|rejectdst)\s")
_YARA_RULE_RE = re.compile(r"(?m)^\s*(?:private\s+|global\s+)*rule\s+[A-Za-z_]\w*")
_YARA_STRING_ID_RE = re.compile(r"^\s*(\$[\w]*)\s*=")

#: Sigma log sources this deployment knows about. An unlisted one is REPORTED, not
#: refused: the set of real products is open, and a grader that fails an unfamiliar
#: but legitimate logsource would get switched off.
KNOWN_SIGMA_LOGSOURCES: frozenset[str] = frozenset({
    "windows", "linux", "macos", "azure", "aws", "gcp", "m365", "okta",
    "kubernetes", "network", "dns", "firewall", "proxy", "webserver", "antivirus",
    "process_creation", "network_connection", "file_event", "registry_event",
    "registry_set", "registry_add", "image_load", "pipe_created", "dns_query",
    "ps_script", "ps_module", "security", "system", "application", "sysmon",
    "auditd", "zeek", "suricata", "sysmon_error",
})


def load_yaml():
    """The YAML parser, or ``None``. Module-level so its absence is testable."""
    try:
        import yaml
    except Exception:  # noqa: BLE001 — absence must be visible, not silently clean
        return None
    return yaml


class DetectionRuleGrader(Grader):
    """Structural screening always; a PASS only when the real engine accepted the rule."""

    grader_id = "detection_rule"
    grader_version = f"{GRADER_PROTOCOL_VERSION}.detection_rule.1"
    supported_families: ClassVar[frozenset[TaskFamily]] = frozenset(
        f for f in TaskFamily if f.is_detection_rule)

    def availability(self, ctx: GraderContext) -> ToolProbe:
        tooling = RULE_TOOLING.get(ctx.family)
        if tooling is None:  # pragma: no cover — guarded by supported_families
            return ToolProbe(name="", available=False,
                             reason=f"no validator is defined for {ctx.family.value}")
        name, version_argv, _suffix = tooling
        cwd = ctx.workspace_root if ctx.has_workspace() else None
        return probe_tool(name, version_argv=version_argv, cwd=cwd)

    def measure(self, ctx: GraderContext):
        rule_text = self._rule_text(ctx)
        if not rule_text.strip():
            return insufficient(
                self, "the attempt produced no rule text to validate")

        leaks = [c for c in scan_private_content(rule_text)
                 if c not in ("scanner_unavailable",)]
        if leaks:
            return blocking_failure(
                self, f"the rule carries private content ({', '.join(sorted(set(leaks)))}); "
                      f"the matched value is deliberately not recorded",
                measured=1,
                findings=[{"kind": "private_content_in_rule", "blocking": True,
                           "categories": sorted(set(leaks))}])

        findings, checks = self._structural(ctx, rule_text)
        if checks <= 0:
            return insufficient(
                self, "the rule could not be parsed far enough for any check to run")

        blocking = [f for f in findings if f.get("blocking")]
        if blocking:
            kinds = sorted({f["kind"] for f in blocking})
            return make_result(
                self, ResultStatus.FAIL, score=0.0, severity=Severity.MEDIUM,
                evidence=(f"{len(blocking)} structural defect(s) over {checks} check(s): "
                          f"{', '.join(kinds)}",),
                findings=findings, measured=checks)

        probe = self.availability(ctx)
        if not probe.available:
            # The structural screen passed. That is not the same as the engine
            # accepting the rule, and reporting it as one would be the exact lie this
            # grader exists to refuse.
            reason = (f"{probe.reason}; {checks} structural check(s) passed, but only "
                      f"the engine can decide whether this rule is valid")
            if ctx.is_mandatory(self.grader_id):
                return insufficient(self, reason, measured=checks, findings=findings)
            return make_result(self, ResultStatus.SKIPPED, evidence=(reason,),
                               findings=findings, measured=0)

        outcome_findings, validator_checks, failed = self._run_validator(
            ctx, probe, rule_text)
        findings.extend(outcome_findings)
        checks += validator_checks
        if failed:
            return make_result(
                self, ResultStatus.FAIL, score=0.0, severity=Severity.MEDIUM,
                evidence=(f"{probe.name} rejected the rule",),
                findings=findings, tool_version=probe.version, measured=checks)
        return make_result(
            self, ResultStatus.PASS, score=1.0, findings=findings,
            tool_version=probe.version, measured=checks,
            evidence=(f"{probe.name} accepted the rule; {checks} check(s) performed",))

    # -- inputs ----------------------------------------------------------------
    def _rule_text(self, ctx: GraderContext) -> str:
        if isinstance(ctx.structured_output, str):
            return ctx.structured_output
        if isinstance(ctx.structured_output, Mapping):
            candidate = ctx.structured_output.get("rule")
            if isinstance(candidate, str):
                return candidate
        return str(ctx.answer or "")

    # -- structural screening --------------------------------------------------
    def _structural(self, ctx: GraderContext,
                    rule_text: str) -> tuple[list[dict], int]:
        if ctx.family is TaskFamily.SIGMA_RULE:
            return self._sigma(ctx, rule_text)
        if ctx.family is TaskFamily.YARA_RULE:
            return self._yara(rule_text)
        return self._suricata(rule_text)

    def _sigma(self, ctx: GraderContext, rule_text: str) -> tuple[list[dict], int]:
        yaml = load_yaml()
        if yaml is None:
            return ([{"kind": "yaml_unavailable", "blocking": False,
                      "detail": "PyYAML is not importable, so the rule body was not "
                                "parsed"}], 0)
        try:
            document = yaml.safe_load(rule_text)
        except Exception as exc:  # noqa: BLE001 — a rule that will not parse is broken
            return ([{"kind": "unparseable_rule", "blocking": True,
                      "detail": f"YAML parse failure: {type(exc).__name__}"}], 1)
        if not isinstance(document, Mapping):
            return ([{"kind": "unparseable_rule", "blocking": True,
                      "detail": "a Sigma rule must be a YAML mapping"}], 1)

        findings: list[dict] = []
        checks = 0
        for field in REQUIRED_FIELDS[TaskFamily.SIGMA_RULE]:
            checks += 1
            if not document.get(field):
                findings.append({"kind": "missing_metadata", "field": field,
                                 "blocking": True,
                                 "detail": f"a Sigma rule requires {field!r}"})
        findings.extend(_placeholder_findings(document))
        checks += 1

        logsource = document.get("logsource")
        checks += 1
        if isinstance(logsource, Mapping):
            values = [str(v).strip().lower() for v in logsource.values() if v]
            if not values:
                findings.append({"kind": "empty_logsource", "blocking": True,
                                 "detail": "logsource names no product, service or "
                                           "category"})
            unknown = [v for v in values if v not in KNOWN_SIGMA_LOGSOURCES]
            if unknown:
                findings.append({"kind": "unknown_logsource", "blocking": False,
                                 "values": unknown,
                                 "detail": "this deployment does not recognise the "
                                           "log source; confirm it is real before "
                                           "shipping the rule"})

        detection = document.get("detection")
        checks += 1
        if isinstance(detection, Mapping):
            if "condition" not in detection:
                findings.append({"kind": "missing_condition", "blocking": True,
                                 "detail": "detection declares no condition"})
            elif len([k for k in detection if k != "condition"]) == 0:
                findings.append({"kind": "empty_detection", "blocking": True,
                                 "detail": "the condition references no selection"})

        for tag in document.get("tags") or ():
            text = str(tag).strip()
            if _ATTACK_ANY_RE.match(text) and text.lower().startswith("attack.t"):
                checks += 1
                if not _ATTACK_TAG_RE.match(text):
                    findings.append({"kind": "invalid_attack_id", "value": text[:40],
                                     "blocking": True,
                                     "detail": "not a well-formed ATT&CK technique id"})
        return findings, checks

    def _yara(self, rule_text: str) -> tuple[list[dict], int]:
        findings: list[dict] = []
        checks = 1
        if not _YARA_RULE_RE.search(rule_text):
            findings.append({"kind": "unparseable_rule", "blocking": True,
                             "detail": "no 'rule <name>' declaration was found"})
            return findings, checks
        for field in REQUIRED_FIELDS[TaskFamily.YARA_RULE]:
            checks += 1
            if f"{field}:" not in rule_text:
                findings.append({"kind": "missing_metadata", "field": field,
                                 "blocking": True,
                                 "detail": f"a YARA rule requires a {field!r} section"})
        checks += 1
        if "author" not in rule_text or "description" not in rule_text:
            findings.append({"kind": "missing_metadata", "field": "meta.author",
                             "blocking": True,
                             "detail": "meta must name an author and a description"})
        declared = {m.group(1) for m in
                    (_YARA_STRING_ID_RE.match(line) for line in rule_text.splitlines())
                    if m}
        condition = rule_text.split("condition:", 1)[-1]
        checks += 1
        used = set(re.findall(r"\$\w*", condition))
        undefined = sorted(u for u in used
                           if u not in declared and not u.endswith("*") and u != "$")
        if undefined and declared:
            findings.append({"kind": "undefined_string_identifier",
                             "values": undefined[:8], "blocking": True,
                             "detail": "the condition references a string that the "
                                       "rule never defines"})
        findings.extend(_placeholder_findings(rule_text))
        checks += 1
        return findings, checks

    def _suricata(self, rule_text: str) -> tuple[list[dict], int]:
        findings: list[dict] = []
        lines = [ln for ln in rule_text.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        checks = 1
        if not lines:
            findings.append({"kind": "unparseable_rule", "blocking": True,
                             "detail": "the rule body is empty"})
            return findings, checks
        for index, line in enumerate(lines):
            checks += 1
            if not _SURICATA_ACTION_RE.match(line):
                findings.append({"kind": "unparseable_rule", "line": index + 1,
                                 "blocking": True,
                                 "detail": "a Suricata rule must begin with an action"})
                continue
            for field in REQUIRED_FIELDS[TaskFamily.SURICATA_RULE]:
                checks += 1
                if f"{field}:" not in line:
                    findings.append({"kind": "missing_metadata", "field": field,
                                     "line": index + 1, "blocking": True,
                                     "detail": f"a Suricata rule requires {field!r}"})
        findings.extend(_placeholder_findings(rule_text))
        checks += 1
        return findings, checks

    # -- the engine ------------------------------------------------------------
    def _run_validator(self, ctx: GraderContext, probe: ToolProbe,
                       rule_text: str) -> tuple[list[dict], int, bool]:
        """``(findings, checks, failed)``. The rule is written OUTSIDE the workspace.

        A grader that dropped the candidate rule into the graded tree would change the
        tree the diff describes, and the file-boundary grader would be right to
        complain about it."""
        _name, _version_argv, suffix = RULE_TOOLING[ctx.family]
        findings: list[dict] = []
        with tempfile.TemporaryDirectory(prefix="gym-rule-") as staging:
            rule_path = Path(staging) / f"candidate{suffix}"
            rule_path.write_text(rule_text, encoding="utf-8")
            argv = self._validator_argv(ctx, probe, rule_path)
            outcome = run_bounded(argv, cwd=staging, timeout_s=ctx.timeout_s)
            if outcome.timed_out:
                return ([{"kind": "validator_timeout", "blocking": True,
                          "detail": f"{probe.name} exceeded {ctx.timeout_s}s"}], 1, True)
            if not outcome.started:
                return ([{"kind": "validator_unusable", "blocking": True,
                          "detail": f"{probe.name} could not be started"}], 1, True)
            failed = outcome.exit_code != 0
            if failed:
                findings.append({
                    "kind": "validator_rejected", "blocking": True,
                    "detail": ctx.sanitize(outcome.stderr or outcome.stdout,
                                           limit=400)})
            fixture_findings, fixture_checks = self._fixtures(ctx, probe, rule_path)
            findings.extend(fixture_findings)
            failed = failed or any(f.get("blocking") for f in fixture_findings)
        return findings, 1 + fixture_checks, failed

    def _validator_argv(self, ctx: GraderContext, probe: ToolProbe,
                        rule_path: Path) -> list[str]:
        if ctx.family is TaskFamily.YARA_RULE:
            # Compile only. No target is scanned at this stage.
            return [probe.executable, "--fail-on-warnings", str(rule_path),
                    str(rule_path)]
        if ctx.family is TaskFamily.SURICATA_RULE:
            return [probe.executable, "-T", "-S", str(rule_path), "-l", str(
                rule_path.parent)]
        return [probe.executable, "check", str(rule_path)]

    def _fixtures(self, ctx: GraderContext, probe: ToolProbe,
                  rule_path: Path) -> tuple[list[dict], int]:
        """Positive, negative and false-positive fixtures, where the engine can run them.

        Only YARA can be pointed at a file and asked "does this match" without standing
        up a pipeline, so it is the only engine this layer evaluates fixtures for. For
        the others the absence of fixture evaluation is RECORDED rather than implied.
        """
        expectations = _fixture_expectations(ctx)
        if not expectations:
            return [], 0
        if ctx.family is not TaskFamily.YARA_RULE:
            return ([{"kind": "fixtures_not_evaluated", "blocking": False,
                      "detail": f"{probe.name} cannot evaluate a fixture without a "
                                f"pipeline; the rule's syntax was validated and its "
                                f"fixtures were not"}], 0)
        findings: list[dict] = []
        checks = 0
        for rel, should_match in expectations:
            try:
                target = ctx.resolve(rel, "fixture")
            except Exception as exc:  # noqa: BLE001 — a refused path is a finding
                findings.append({"kind": "fixture_unreadable", "path": rel,
                                 "blocking": False,
                                 "detail": f"path refused ({type(exc).__name__})"})
                continue
            if not target.is_file():
                findings.append({"kind": "fixture_unreadable", "path": rel,
                                 "blocking": False,
                                 "detail": "fixture is not a regular file"})
                continue
            checks += 1
            outcome = run_bounded([probe.executable, str(rule_path), str(target)],
                                  cwd=str(rule_path.parent), timeout_s=ctx.timeout_s)
            matched = outcome.started and outcome.exit_code == 0 \
                and bool(outcome.stdout.strip())
            if matched is not should_match:
                findings.append({
                    "kind": "fixture_expectation_failed", "path": rel,
                    "expected_match": should_match, "matched": matched,
                    "blocking": True,
                    "detail": "the rule did not behave as the fixture requires"})
        return findings, checks


def _fixture_expectations(ctx: GraderContext) -> tuple[tuple[str, bool], ...]:
    """``(relative path, should_match)`` pairs declared by the task's baseline."""
    baseline = ctx.baseline if isinstance(ctx.baseline, Mapping) else {}
    pairs: list[tuple[str, bool]] = []
    for key, expected in (("positive_fixtures", True), ("negative_fixtures", False),
                          ("false_positive_fixtures", False)):
        for rel in baseline.get(key) or ():
            pairs.append((str(rel), expected))
    return tuple(pairs)


def _placeholder_findings(payload: Any) -> list[dict]:
    """Fields the author never filled in. A rule shipping ``author: TODO`` is a draft."""
    text = payload if isinstance(payload, str) else _flatten(payload)
    hits = sorted({m.group(0)[:40] for m in _PLACEHOLDER_RE.finditer(text)})
    if not hits:
        return []
    return [{"kind": "placeholder_value", "values": hits[:8], "blocking": True,
             "detail": "the rule carries unfilled placeholder values"}]


def _flatten(value: Any, *, _depth: int = 0) -> str:
    if _depth > 12 or value is None:
        return ""
    if isinstance(value, Mapping):
        return " ".join(f"{k} {_flatten(v, _depth=_depth + 1)}" for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return " ".join(_flatten(v, _depth=_depth + 1) for v in value)
    return str(value)


__all__ = ["KNOWN_SIGMA_LOGSOURCES", "REQUIRED_FIELDS", "RULE_TOOLING",
           "DetectionRuleGrader", "load_yaml"]
