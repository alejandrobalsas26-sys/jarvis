"""scripts/mutation_campaign_m68d.py — V69 M68D (§45): the falsification campaign.

For each mutation:
  0. (once, up front) run every MAPPED test UNMUTATED and require it to PASS,
  1. assert the anchor occurs EXACTLY ONCE in its file,
  2. apply it, run the mapped focused test in a FRESH subprocess,
  3. require the test to FAIL (the mutation must be DETECTED),
  4. restore the file (always).

Step 0 is not ceremony. A mapped test that is RED — or merely SKIPPED — before
any mutation reports DETECTED for every mutation and turns the whole campaign
into a green rubber stamp. Exit 2 means "could not be evaluated".

A mutation whose mapped test stays GREEN is a LOAD-BEARING SURVIVOR: a property
of the trust-boundary code with no test behind it. Requirement: 0 UNEXPLAINED
survivors, 0 anchor errors, 0 vacuous mappings. A survivor may be registered in
:data:`EXPLAINED_SURVIVORS` only with a MEASURED reason — never to make the
number look better, and never by deleting the mutation.

Targets are files M68D wrote or changed, plus `.gitattributes` and the CI
workflow, which are controls in exactly the same sense: a byte seal nothing pins
and a portability gate no runner executes are both decorative. Nothing here
touches `training_gym/`, `graders/` or sealed historical state.

Run from `jarvis/`.
"""
from __future__ import annotations

import os
import subprocess  # nosec B404 - this IS a test runner; every argv is a fixed list
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── files under mutation ─────────────────────────────────────────────────────
EX = "tools/executor.py"
SI = "core/source_integrity.py"
SO = "core/safe_observability.py"
GOV = "core/governance.py"
LLM = "core/llm.py"
NQ = "core/network_quarantine.py"
CPV = "scripts/verify_m62_control_plane.py"
M68C_TESTS = "tests/test_source_integrity_m68c.py"
GITATTRIBUTES = "../.gitattributes"
WORKFLOW = "../.github/workflows/ci.yml"

# ── mapped tests ─────────────────────────────────────────────────────────────
T1 = "tests/test_trust_boundary_m68d_h01_egress.py"
T2 = "tests/test_trust_boundary_m68d_h02_source_identity.py"
T3 = "tests/test_trust_boundary_m68d_h03_safe_observability.py"
T4 = "tests/test_trust_boundary_m68d_h04_quarantine_truth.py"
T5 = "tests/test_trust_boundary_m68d_h05_portability.py"

H1_RESOLVE = f"{T1}::TestGovernedResolution"
H1_PIN = f"{T1}::TestPinningAtTheSocket"
H1_PROXY = f"{T1}::TestProxySemantics"
H1_REDIR = f"{T1}::TestRedirectsReturnToPolicy"
H1_TLS = f"{T1}::TestTlsIsNotWeakened"
H1_ABSENT = f"{T1}::TestAbsentControls"
H1_FAIL = f"{T1}::TestResolverFailureInjection"

H2_SNAP = f"{T2}::TestSnapshotCoherence"
H2_RACE = f"{T2}::TestSnapshotRaces"
H2_READ = f"{T2}::TestReadFileIdentity"
H2_DERIVED = f"{T2}::TestDerivedFormats"
H2_ABSENT = f"{T2}::TestAbsentControls"

H3_SAN = f"{T3}::TestTheGovernedSanitizer"
H3_FAIL = f"{T3}::TestFailClosed"
H3_ORDER = f"{T3}::TestOrdering"
H3_SINKS = f"{T3}::TestSinksReceiveNoSecret"
H3_HIST = f"{T3}::TestLlmHistorySink"
H3_LOG = f"{T3}::TestLoguruSink"
H3_ABSENT = f"{T3}::TestAbsentControls"

H4_QUAR = f"{T4}::TestQuarantineEffects"
H4_IDEM = f"{T4}::TestQuarantineIdempotence"
H4_REL = f"{T4}::TestReleaseEffects"
H4_DERIVE = f"{T4}::TestEffectDerivation"
H4_ABSENT = f"{T4}::TestAbsentControls"

H5_PATH = f"{T5}::TestRepositoryPathRendering"
H5_MODE = f"{T5}::TestGitModeIsTheAuthority"
H5_NEWLINE = f"{T5}::TestNewlinePinning"
H5_CAP = f"{T5}::TestPlatformCapabilities"
H5_INTEG = f"{T5}::TestIntegrityIsNotWeakened"
H5_CI = f"{T5}::TestWindowsCiJob"

#: Survivors with a MEASURED explanation. Empty is the expected state; an entry
#: here is a claim that has to be defended in the milestone document.
EXPLAINED_SURVIVORS: dict[str, str] = {}


def _mut(mid, cat, file, find, replace, test):
    return {"id": mid, "cat": cat, "file": file, "find": find,
            "replace": replace, "test": test}


MUTATIONS: list[dict] = []

# ── H01 · SSRF / DESTINATION IDENTITY ────────────────────────────────────────
MUTATIONS += [
    # The decision stops carrying an address, so nothing can be pinned to it.
    _mut("H01_pin_nothing", "H01_PIN", EX,
         "    return _decide(candidates, candidates[0], None)",
         "    return _decide(candidates, None, None)", H1_PIN),
    # The address chosen is not one the policy validated.
    _mut("H01_choose_outside_validated_set", "H01_PIN", EX,
         "    return _decide(candidates, candidates[0], None)\n",
         '    return _decide(candidates, "127.0.0.1", None)\n', H1_PIN),
    # The transport resolves the hostname again, which is the original defect.
    _mut("H01_transport_resolves_the_hostname_again", "H01_PIN", EX,
         "    if dest.pinned is not None:\n"
         "        adapter = _PinnedDestinationAdapter(dest.pinned)\n"
         "        session.mount(\"http://\", adapter)\n"
         "        session.mount(\"https://\", adapter)",
         "    if False:\n"
         "        adapter = _PinnedDestinationAdapter(dest.pinned)\n"
         "        session.mount(\"http://\", adapter)\n"
         "        session.mount(\"https://\", adapter)", H1_PIN),
    # Only the first DNS answer is validated; a mixed answer slips through.
    _mut("H01_validate_only_the_first_candidate", "H01_POLICY", EX,
         "        for raw_ip in candidates:\n"
         "            reason = _internal_address_reason(host, raw_ip)",
         "        for raw_ip in candidates[:1]:\n"
         "            reason = _internal_address_reason(host, raw_ip)", H1_RESOLVE),
    # Trusted-lab mode stops being a mode and weakens normal mode.
    _mut("H01_trusted_lab_weakens_normal_mode", "H01_POLICY", EX,
         "    if not trusted:\n"
         "        for raw_ip in candidates:",
         "    if False:\n"
         "        for raw_ip in candidates:", H1_RESOLVE),
    # A redirect hop is not re-resolved: every hop reuses the first decision.
    _mut("H01_redirect_without_revalidation", "H01_REDIRECT", EX,
         "        dest = govern_destination(current_url)",
         "        dest = govern_destination(url)", H1_REDIR),
    # The environment proxy comes back.
    _mut("H01_env_proxy_inherited", "H01_PROXY", EX,
         "    session.trust_env = False",
         "    session.trust_env = True", H1_PROXY),
    _mut("H01_per_request_proxies_not_cleared", "H01_PROXY", EX,
         "    session.proxies = {}\n",
         "    session.proxies = dict(os.environ.get(\"HTTP_PROXY\") and\n"
         "                           {\"http\": os.environ[\"HTTP_PROXY\"]} or {})\n",
         H1_PROXY),
    # Host/SNI preservation broken in each of the two ways that matter.
    _mut("H01_host_header_becomes_the_ip", "H01_HOST", EX,
         '    if dest.pinned is not None and dest.authority:\n'
         '        send_headers["Host"] = dest.authority',
         '    if False:\n'
         '        send_headers["Host"] = dest.authority', H1_PIN),
    _mut("H01_sni_uses_the_pinned_ip", "H01_TLS", EX,
         '        pool_kwargs["server_hostname"] = host_params["host"]',
         '        pool_kwargs["server_hostname"] = self._pinned_ip', H1_TLS),
    # Redirect following is delegated back to the transport, so hops are unchecked.
    _mut("H01_transport_follows_redirects", "H01_REDIRECT", EX,
         "            timeout=timeout, allow_redirects=False, proxies={},",
         "            timeout=timeout, allow_redirects=True, proxies={},", H1_ABSENT),
    # An unpinned decision is allowed through in normal mode.
    _mut("H01_unpinned_decision_allowed", "H01_PIN", EX,
         "        if dest.pinned is None and not dest.trusted_lab:",
         "        if False:", H1_ABSENT),
    # A resolver failure becomes an allow instead of a block.
    _mut("H01_resolver_failure_allows", "H01_FAILCLOSED", EX,
         "            return _decide((), None, f\"No se pudo resolver el host '{host}'.\")",
         "            return _decide((), None, None)", H1_FAIL),
]

# ── H02 · SOURCE READ IDENTITY ───────────────────────────────────────────────
MUTATIONS += [
    # The digest comes from a LATER reopening of the mutable path.
    _mut("H02_digest_from_a_second_opening", "H02_COHERENCE", SI,
         "            payload = b\"\".join(chunks)\n"
         "            digest.update(payload)",
         "            payload = b\"\".join(chunks)\n"
         "            with open(str(resolved), \"rb\") as _re:\n"
         "                digest.update(_re.read())", H2_RACE),
    # Source mutation during the observation is accepted as complete.
    _mut("H02_mutation_during_read_is_stable", "H02_STABILITY", SI,
         "        stable = (\n"
         "            before.st_ino == after.st_ino",
         "        stable = True or (\n"
         "            before.st_ino == after.st_ino", H2_RACE),
    # One fstat cannot detect a change during the observation.
    _mut("H02_single_fstat", "H02_STABILITY", SI,
         "        after = os.fstat(fd)\n"
         "        stable = (",
         "        after = before\n"
         "        stable = (", H2_RACE),
    # An unstable snapshot still states a precondition.
    _mut("H02_unstable_still_has_a_digest", "H02_STABILITY", SI,
         "    sha = snapshot.sha256 if snapshot.stable else None",
         "    sha = snapshot.sha256", H2_RACE),
    # The stream reopens the mutable path instead of duplicating the descriptor.
    _mut("H02_stream_reopens_the_path", "H02_COHERENCE", SI,
         "        dup = os.dup(self._fd)\n"
         "        handle = os.fdopen(dup, \"rb\")",
         "        handle = open(self.path, \"rb\")  # noqa: SIM115\n"
         "        dup = -1", H2_DERIVED),
    # The read renders from one opening and identifies another.
    _mut("H02_render_from_a_fresh_read", "H02_COHERENCE", EX,
         '                    content = snapshot.text(encoding="utf-8", errors="ignore")',
         '                    content = p.read_text(encoding="utf-8", errors="ignore")',
         H2_READ),
    # The unstable short-circuit is removed, so a torn read is returned.
    _mut("H02_unstable_read_returns_content", "H02_STABILITY", EX,
         "                if not snapshot.stable:",
         "                if False:", H2_READ),
    # The derived path stops re-checking after the parse.
    _mut("H02_derived_recheck_ignored", "H02_STABILITY", EX,
         "                    if not snapshot.recheck():",
         "                    if False:", H2_DERIVED),
    # The reported size comes from a third, independent stat of the path.
    _mut("H02_size_from_a_fresh_stat", "H02_COHERENCE", EX,
         '                    "size_kb": round(snapshot.size_bytes / 1024, 2),',
         '                    "size_kb": round(p.stat().st_size / 1024, 2),',
         H2_ABSENT),
    # A derived extension falls through to OCR instead of failing closed.
    _mut("H02_derived_dispatch_falls_through", "H02_DISPATCH", EX,
         "        reader = self._DERIVED_READERS.get(ext)\n"
         "        if reader is None:",
         "        reader = self._DERIVED_READERS.get(ext, \"_read_image_ocr\")\n"
         "        if reader is None:", H2_DERIVED),
    # An empty capture reports a digest over nothing as complete.
    _mut("H02_size_ignores_the_captured_length", "H02_STABILITY", SI,
         "            and before.st_size == after.st_size == total",
         "            and before.st_size == after.st_size", H2_RACE),
]

# ── H03 · SECRET-SAFE OBSERVABILITY ──────────────────────────────────────────
#
# The two ORDERING mutations below are mapped to the ABSENT-CONTROL class, not to
# the sink tests, and that is a measured decision rather than a convenience.
# H03's fix has two enforcement points: the call site sanitizes BEFORE building a
# summary, and `TacticAuditLogger.log_action` / `_aura_broadcast` sanitize again
# at the sink. Re-introducing the raw dump at the call site therefore leaks
# NOTHING — the sink catches it — so the canary tests stay green and the layer
# that answered is the lower one. The ordering §18 requires is real and is
# enforced structurally; mapping these to a canary test would have recorded a
# detection the canary did not make.
MUTATIONS += [
    # The summary is built from the RAW result again (the original defect).
    _mut("H03_summary_before_redaction", "H03_ORDER", EX,
         "        self._audit.log_action(\n"
         "            tool_name, reasoning, \"sync\", status,\n"
         "            safe_observability.safe_summary(result),\n"
         "        )",
         "        self._audit.log_action(\n"
         "            tool_name, reasoning, \"sync\", status,\n"
         "            json.dumps(result, ensure_ascii=False, default=str)[:200],\n"
         "        )", H3_ABSENT),
    _mut("H03_async_summary_before_redaction", "H03_ORDER", EX,
         "            output_summary = safe_observability.safe_summary(result)\n"
         "            result = self._check_pii_output(result)",
         "            output_summary = json.dumps(result, ensure_ascii=False,\n"
         "                                        default=str)[:200]\n"
         "            result = self._check_pii_output(result)", H3_ABSENT),
    # The persistent audit sink gets the raw strings.
    _mut("H03_jsonl_gets_the_raw_result", "H03_SINK", GOV,
         '            "result": safe_observability.safe_reasoning(result) if result else "",',
         '            "result": result[:200] if result else "",', H3_SINKS),
    _mut("H03_jsonl_gets_the_raw_reasoning", "H03_SINK", GOV,
         '            "thinking": safe_observability.safe_reasoning(reasoning),',
         '            "thinking": reasoning,', H3_SINKS),
    _mut("H03_jsonl_gets_the_raw_command", "H03_SINK", GOV,
         '            "command": safe_observability.sanitize_text(str(command or "")),',
         '            "command": command,', H3_SINKS),
    # The AURA sink broadcasts the unsanitized event.
    _mut("H03_aura_gets_the_raw_event", "H03_SINK", EX,
         "        await broadcast(safe_event)",
         "        await broadcast(event)", H3_SINKS),
    # Sanitizer failure falls back to raw.
    _mut("H03_refusal_falls_back_to_raw", "H03_FAILCLOSED", SO,
         '    except _SanitizeRefused as exc:\n'
         '        return {"_redaction": REDACTION_FAILED, "_reason": str(exc)[:120],\n'
         '                "_type": type(value).__name__}',
         "    except _SanitizeRefused:\n"
         "        return value", H3_FAIL),
    _mut("H03_exception_falls_back_to_raw", "H03_FAILCLOSED", SO,
         '    except Exception as exc:                                   # noqa: BLE001\n'
         '        return {"_redaction": REDACTION_FAILED,\n'
         '                "_reason": f"{type(exc).__name__}", "_type": type(value).__name__}',
         "    except Exception:                                          # noqa: BLE001\n"
         "        return value", H3_FAIL),
    _mut("H03_governed_pipeline_failure_falls_back_to_raw", "H03_FAILCLOSED", SO,
         "    except Exception:                                          # noqa: BLE001\n"
         "        return REDACTION_FAILED",
         "    except Exception:                                          # noqa: BLE001\n"
         "        return out", H3_FAIL),
    # A nested value is not walked, so a nested secret survives.
    _mut("H03_nested_value_not_walked", "H03_STRUCTURE", SO,
         "                out[safe_key] = _walk(item, depth + 1, seen)",
         "                out[safe_key] = item", H3_SAN),
    # The field-name rule is removed, so an opaque credential survives.
    _mut("H03_field_name_rule_removed", "H03_STRUCTURE", SO,
         "            if is_secret_field(key):",
         "            if False:", H3_SAN),
    # The PEM pre-pass moves after the governed pipeline and loses its anchor.
    _mut("H03_pem_prepass_removed", "H03_ORDER", SO,
         "    for pattern in _PRE_SPANS:\n"
         "        out = pattern.sub(REDACTED, out)",
         "    for pattern in ():\n"
         "        out = pattern.sub(REDACTED, out)", H3_SAN),
    # The summary serialises the raw value and only then cuts.
    _mut("H03_summary_skips_the_sanitizer", "H03_ORDER", SO,
         "        safe = sanitize(value)",
         "        safe = value", H3_ORDER),
    # Reasoning bypasses the sanitizer at its own function.
    _mut("H03_reasoning_bypasses_the_sanitizer", "H03_SINK", SO,
         "        safe = sanitize_text(text)",
         "        safe = text", H3_SINKS),
    # A handler's exception message reaches the log file unsanitized.
    _mut("H03_loguru_gets_the_raw_exception", "H03_SINK", EX,
         "            logger.error(f\"Error en tool '{tool_name}': \"\n"
         "                         f\"{safe_observability.safe_reasoning(str(e))}\")\n"
         "            self._audit.log_action(tool_name, reasoning, \"sync\", \"error\", str(e)[:200])",
         "            logger.error(f\"Error en tool '{tool_name}': {e}\")\n"
         "            self._audit.log_action(tool_name, reasoning, \"sync\", \"error\", str(e)[:200])",
         H3_LOG),
    # Tool output re-enters the prompt history unsanitized.
    _mut("H03_llm_history_gets_raw_output", "H03_SINK", LLM,
         "                result_str = _safe_obs.sanitize_text(result_str)",
         "                result_str = result_str", H3_HIST),
]

# ── H04 · QUARANTINE EFFECT TRUTH ────────────────────────────────────────────
MUTATIONS += [
    # The deletion return codes are discarded again.
    _mut("H04_release_ignores_the_rc", "H04_EVIDENCE", NQ,
         '            cmd = ["netsh", "advfirewall", "firewall", "delete", "rule",\n'
         '                   f"name={name}"]\n'
         "            rc, out = await loop.run_in_executor(None, _run, cmd)",
         '            cmd = ["netsh", "advfirewall", "firewall", "delete", "rule",\n'
         '                   f"name={name}"]\n'
         "            await loop.run_in_executor(None, _run, cmd)\n"
         "            rc, out = 0, \"\"", H4_REL),
    # The receipt's per-rule rc stops matching what the command said. The
    # OUTCOME is decided by the query, which is why discarding the rc leaves
    # behaviour intact — so the evidence field is what has to be asserted.
    # Success is asserted rather than proven.
    _mut("H04_released_unconditionally", "H04_TRUTH", NQ,
         '        res["released"] = proven_absent',
         '        res["released"] = True', H4_REL),
    # Tracking is erased before the release is verified.
    _mut("H04_pop_before_verification", "H04_TRACKING", NQ,
         "        if proven_absent:\n"
         "            _active.pop(ip, None)",
         "        if True:\n"
         "            _active.pop(ip, None)", H4_REL),
    # The release stops querying and trusts the command again.
    _mut("H04_release_skips_the_query", "H04_EVIDENCE", NQ,
         "            present = await loop.run_in_executor(None, _rule_present, name)\n"
         "            # For a delete the TARGET state is absence, so `want_present=False`.",
         "            present = None\n"
         "            # For a delete the TARGET state is absence, so `want_present=False`.",
         H4_REL),
    # A partial add is dropped from local tracking (the measured failure).
    _mut("H04_partial_add_is_dropped", "H04_TRACKING", NQ,
         "        if effect is not ContainmentEffect.NO_EFFECT:\n"
         "            _active[ip] = res",
         "        if effect is ContainmentEffect.FULL_EFFECT:\n"
         "            _active[ip] = res", H4_QUAR),
    # A partial effect is reported as isolation.
    _mut("H04_partial_claims_isolation", "H04_TRUTH", NQ,
         '            "host_isolated": effect is ContainmentEffect.FULL_EFFECT,',
         '            "host_isolated": effect is not ContainmentEffect.NO_EFFECT,',
         H4_QUAR),
    # UNKNOWN is rounded to a certainty, in each direction.
    _mut("H04_unknown_becomes_committed", "H04_TRUTH", NQ,
         "    if rc == 0:\n"
         "        return _PROVEN_COMMITTED\n"
         "    return _UNKNOWN",
         "    if rc == 0:\n"
         "        return _PROVEN_COMMITTED\n"
         "    return _PROVEN_COMMITTED", H4_DERIVE),
    _mut("H04_unknown_becomes_no_effect", "H04_TRUTH", NQ,
         "    if unknown:\n"
         "        return ContainmentEffect.UNKNOWN_EFFECT",
         "    if unknown:\n"
         "        return ContainmentEffect.NO_EFFECT", H4_DERIVE),
    _mut("H04_mixed_unknown_collapses_to_partial", "H04_TRUTH", NQ,
         "    if unknown and committed:\n"
         "        return ContainmentEffect.RECONCILIATION_REQUIRED",
         "    if unknown and committed:\n"
         "        return ContainmentEffect.PARTIAL_EFFECT", H4_DERIVE),
    # The return code outranks the query.
    _mut("H04_rc_outranks_the_query", "H04_EVIDENCE", NQ,
         "    if present is not None:\n"
         "        if present is want_present:\n"
         "            return _PROVEN_COMMITTED\n"
         "        return _PROVEN_NOT_EXECUTED\n"
         "    if rc == 0:",
         "    if rc == 0:\n"
         "        return _PROVEN_COMMITTED\n"
         "    if present is not None:\n"
         "        if present is want_present:\n"
         "            return _PROVEN_COMMITTED\n"
         "        return _PROVEN_NOT_EXECUTED\n"
         "    if rc == 0:", H4_DERIVE),
    # An unanswerable query is guessed as absence.
    _mut("H04_opaque_query_guesses_absent", "H04_EVIDENCE", NQ,
         "    # An exit code we cannot interpret proves nothing either way.\n"
         "    return None",
         "    # An exit code we cannot interpret proves nothing either way.\n"
         "    return False", H4_DERIVE),
    # The release stops writing a receipt (it never wrote one before M68D).
    _mut("H04_release_writes_no_receipt", "H04_RECEIPT", NQ,
         '        res["still_tracked"] = ip in _active\n'
         "        _audit(res)",
         '        res["still_tracked"] = ip in _active',
         H4_REL),
    # A duplicate request stops being audited.
    _mut("H04_duplicate_not_audited", "H04_RECEIPT", NQ,
         "            # Was silently un-audited. An idempotent request is still a request.\n"
         "            _audit(res)",
         "            # Was silently un-audited. An idempotent request is still a request.\n"
         "            pass", H4_IDEM),
    # The release accepts a command receipt instead of proven absence.
    _mut("H04_release_accepts_rc_zero_as_absence", "H04_EVIDENCE", NQ,
         "        proven_absent = all(r.observed_present is False for r in rules)",
         "        proven_absent = all(r.command_rc == 0 for r in rules)", H4_REL),
]

# ── H05 · WINDOWS / REPOSITORY TRUTH ─────────────────────────────────────────
MUTATIONS += [
    # The native rendering comes back into the tracked-set comparison.
    _mut("H05_native_path_in_the_tracked_set", "H05_PATH", CPV,
         "                VERIFIER_PATH, repo_path(cp.snapshot_path)]",
         "                VERIFIER_PATH, str(cp.snapshot_path.relative_to(REPO_ROOT))]",
         H5_PATH),
    # The normaliser stops normalising.
    _mut("H05_repo_path_uses_str", "H05_PATH", CPV,
         "            return candidate.as_posix()\n"
         "    return candidate.as_posix()",
         "            return str(candidate)\n"
         "    return str(candidate)", H5_PATH),
    # The executable invariant goes back to the filesystem.
    _mut("H05_os_access_for_the_git_mode", "H05_MODE", CPV,
         "    modes = _git_index_modes(*mode_targets)",
         "    modes = {rel: (GIT_MODE_EXECUTABLE\n"
         "                   if os.access(REPO_ROOT / rel, os.X_OK) else \"100644\")\n"
         "             for rel in mode_targets}", H5_MODE),
    # A failed ls-files silently skips the invariant.
    _mut("H05_mode_check_fails_open", "H05_MODE", CPV,
         "    if not modes:\n"
         "        report.fail(\"PATH_INTEGRITY\",",
         "    if False:\n"
         "        report.fail(\"PATH_INTEGRITY\",", H5_MODE),
    # The record store stops checking modes at all.
    _mut("H05_record_store_skips_modes", "H05_MODE", CPV,
         "        record_modes = _git_index_modes(*rel_paths)",
         "        record_modes = {}", H5_MODE),
    # The newline policy is removed from the mandatory dispatch.
    _mut("H05_newline_policy_not_dispatched", "H05_NEWLINE", CPV,
         '    ("check_newline_policy", ("NEWLINE_POLICY",)),\n',
         "", H5_NEWLINE),
    # The newline policy passes vacuously on an empty artifact set.
    _mut("H05_newline_policy_passes_vacuously", "H05_NEWLINE", CPV,
         "    if not paths:\n"
         "        # NON-VACUITY.",
         "    if False:\n"
         "        # NON-VACUITY.", H5_NEWLINE),
    # The byte-sealed set shrinks to nothing.
    _mut("H05_byte_pinned_set_emptied", "H05_NEWLINE", CPV,
         'BYTE_PINNED_TREES = ("state/m62",)',
         "BYTE_PINNED_TREES = ()", H5_NEWLINE),
    # The pin becomes a conversion.
    _mut("H05_gitattributes_text_auto", "H05_NEWLINE", GITATTRIBUTES,
         "state/m62/**           -text",
         "state/m62/**           text=auto", H5_NEWLINE),
    _mut("H05_gitattributes_progress_unpinned", "H05_NEWLINE", GITATTRIBUTES,
         "PROGRESS.md            -text",
         "PROGRESS.md            text eol=lf", H5_NEWLINE),
    # The sealed digest starts normalising newlines before hashing.
    _mut("H05_digest_normalises_newlines", "H05_INTEGRITY", CPV,
         "    return sha256_bytes(path.read_bytes())",
         '    return sha256_bytes(path.read_bytes().replace(b"\\r\\n", b"\\n"))',
         H5_INTEG),
    # The Windows runner stops being Windows.
    _mut("H05_windows_job_runs_on_linux", "H05_CI", WORKFLOW,
         "  windows-portability:\n"
         "    name: Windows portability (3.11, blocking)\n"
         "    runs-on: windows-latest",
         "  windows-portability:\n"
         "    name: Windows portability (3.11, blocking)\n"
         "    runs-on: ubuntu-latest", H5_CI),
    # The Windows runner avoids the condition it exists to witness.
    _mut("H05_windows_job_disables_autocrlf", "H05_CI", WORKFLOW,
         "          git config --get core.autocrlf",
         "          git config core.autocrlf false", H5_CI),
    # The Windows runner stops running the verifier.
    _mut("H05_windows_job_skips_the_verifier", "H05_CI", WORKFLOW,
         "      - name: Control Plane verifier (must be PASS / 0 on Windows)\n"
         "        run: python jarvis/scripts/verify_m62_control_plane.py",
         "      - name: Control Plane verifier (must be PASS / 0 on Windows)\n"
         "        run: python -c \"print('skipped')\"", H5_CI),
    # The Windows runner becomes advisory.
    _mut("H05_windows_job_becomes_advisory", "H05_CI", WORKFLOW,
         "      - name: Control Plane verifier (must be PASS / 0 on Windows)\n"
         "        run:",
         "      - name: Control Plane verifier (must be PASS / 0 on Windows)\n"
         "        continue-on-error: true\n"
         "        run:", H5_CI),
    # POSIX assumptions come back into the source-integrity suite.
    _mut("H05_tests_assume_geteuid", "H05_CAPABILITY", M68C_TESTS,
         'POSIX_MODES_ENFORCED = (\n'
         '    os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0)',
         "POSIX_MODES_ENFORCED = os.geteuid() != 0", H5_CAP),
    _mut("H05_tests_assume_flock", "H05_CAPABILITY", M68C_TESTS,
         "    @pytest.mark.skipif(\n"
         "        not FLOCK_AVAILABLE,",
         "    @pytest.mark.skipif(\n"
         "        False,", H5_CAP),
    # The honest-degradation contract starts lying.
    _mut("H05_serialised_claims_true_without_fcntl", "H05_CAPABILITY", SI,
         "    except ImportError:  # pragma: no cover - Windows\n"
         "        yield False\n"
         "        return",
         "    except ImportError:  # pragma: no cover - Windows\n"
         "        yield True\n"
         "        return", H5_CAP),
]


def _pytest(target: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B603 - fixed argv, shell=False
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:randomly",
         target],
        cwd=_ROOT, capture_output=True, text=True, check=False)


def _preflight(targets: list[str]) -> list[str]:
    """Every mapped test must PASS, and collect at least one test, BEFORE any
    mutation. A red or skipped mapping makes every mutation look DETECTED."""
    bad: list[str] = []
    print(f"PREFLIGHT — {len(targets)} mapped test target(s)")
    for target in sorted(targets):
        proc = _pytest(target)
        tail = (proc.stdout or "").strip().splitlines()
        summary = tail[-1] if tail else "<no output>"
        if proc.returncode != 0:
            bad.append(f"{target}: RED before mutation ({summary})")
            print(f"  [RED ] {target}")
        elif " passed" not in summary or "no tests ran" in summary:
            bad.append(f"{target}: vacuous ({summary})")
            print(f"  [VOID] {target} — {summary}")
        else:
            print(f"  [ OK ] {target} — {summary}")
    return bad


def _run() -> int:
    total = len(MUTATIONS)
    targets = sorted({m["test"] for m in MUTATIONS})
    print(f"M68D FALSIFICATION CAMPAIGN — {total} mutations, "
          f"{len(targets)} mapped targets\n")

    duplicate_ids = [m["id"] for m in MUTATIONS
                     if [x["id"] for x in MUTATIONS].count(m["id"]) > 1]
    if duplicate_ids:
        print(f"DUPLICATE MUTATION IDS: {sorted(set(duplicate_ids))}")
        print("M68D_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
        return 2

    vacuous = _preflight(targets)
    if vacuous:
        print("\nPREFLIGHT FAILED — the campaign would be vacuous:")
        for line in vacuous:
            print(f"  {line}")
        print("M68D_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
        return 2

    survivors: list[str] = []
    anchor_errors: list[str] = []
    detected = 0
    print(f"\nMUTATING — {total} mutations\n")
    for m in MUTATIONS:
        path = os.path.join(_ROOT, m["file"])
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
        count = original.count(m["find"])
        if count != 1:
            anchor_errors.append(f"{m['id']}: anchor occurs {count}x in {m['file']}")
            print(f"  [ANCHOR ERR] {m['id']:44s} {count}x")
            continue
        mutated = original.replace(m["find"], m["replace"], 1)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(mutated)
            proc = _pytest(m["test"])
            if proc.returncode != 0:
                detected += 1
                print(f"  [DETECTED] {m['id']:44s} ({m['cat']})")
            else:
                survivors.append(m["id"])
                marker = ("EXPLAINED" if m["id"] in EXPLAINED_SURVIVORS
                          else "NO TEST FAILED")
                print(f"  [SURVIVOR] {m['id']:44s} ({m['cat']}) — {marker}")
        finally:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)

    unexplained = [s for s in survivors if s not in EXPLAINED_SURVIVORS]
    explained = [s for s in survivors if s in EXPLAINED_SURVIVORS]
    print(f"\n{'=' * 72}")
    print(f"mutations:              {total}")
    print(f"detected:               {detected}")
    print(f"explained survivors:    {len(explained)}  {explained if explained else ''}")
    print(f"unexplained survivors:  {len(unexplained)}  "
          f"{unexplained if unexplained else ''}")
    print(f"anchor errors:          {len(anchor_errors)}  "
          f"{anchor_errors if anchor_errors else ''}")
    for sid in explained:
        print(f"  EXPLAINED {sid}: {EXPLAINED_SURVIVORS[sid]}")
    ok = not unexplained and not anchor_errors
    print(f"M68D_MUTATION_CAMPAIGN: {'PASS' if ok else 'FAIL'} "
          f"({detected}/{total} detected, {len(unexplained)} unexplained survivors)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_run())
