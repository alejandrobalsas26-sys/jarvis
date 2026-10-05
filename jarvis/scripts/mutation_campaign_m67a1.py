"""scripts/mutation_campaign_m67a1.py — V69 M67A.1 (§32): the falsification campaign.

For each mutation:
  0. (once, up front) run every MAPPED test UNMUTATED and require it to PASS,
  1. assert the anchor occurs EXACTLY ONCE in its file,
  2. apply it, run the mapped focused test in a FRESH subprocess,
  3. require the test to FAIL (the mutation must be DETECTED),
  4. restore the file (always).

Step 0 is not ceremony. A mapped test that is RED — or merely SKIPPED — before any mutation
reports DETECTED for every mutation and turns the whole campaign into a green rubber stamp.
Exit 2 means "could not be evaluated".

A mutation whose mapped test stays GREEN is a LOAD-BEARING SURVIVOR: a property of the
corpus-qualification path with no test behind it. Requirement: 0 survivors, 0 anchor
errors, 0 vacuous mappings. There is no percentage target.

SCOPE. Targets are files M67A.1 wrote or changed, plus two M67A files whose behaviour
M67A.1 added NEW assertions about (`splitting.py`'s empty-corpus refusal and `config.py`'s
holdout floor). Mutating those is in scope precisely because the assertions being falsified
are new: M67A shipped no mutation campaign, so the empty-corpus refusal — the single most
important behaviour this milestone measured — had never been falsified before. Nothing here
touches `training_gym/graders/` or `training_gym/training/`, which are byte-frozen.

Run from `jarvis/`.
"""
from __future__ import annotations

import os
import subprocess  # nosec B404 - this IS a test runner; every argv is a fixed list
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── files under mutation ─────────────────────────────────────────────────────
DX = "reasoning_distillation/adapters/docx_export.py"
TR = "reasoning_distillation/adapters/transcript.py"
RG = "reasoning_distillation/adapters/__init__.py"
BA = "reasoning_distillation/adapters/base.py"
PL = "reasoning_distillation/pipeline.py"
SP = "reasoning_distillation/splitting.py"
CF = "reasoning_distillation/config.py"

# ── mapped tests ─────────────────────────────────────────────────────────────
D = "tests/test_reasoning_docx_adapter_m67a1.py"
H = "tests/test_reasoning_hidden_reasoning_prohibition_m67a1.py"
R = "tests/test_reasoning_real_corpus_invariants_m67a1.py"
M = "tests/test_reasoning_distillation_m67a.py"

T_ROLELESS = f"{D}::test_a_role_less_document_is_refused_with_structural_counts"
T_ONESIDED = f"{D}::test_one_role_alone_is_not_a_conversation"
T_NODOC = f"{D}::test_sniff_refuses_a_zip_without_a_word_document_member"
T_DATE = f"{D}::test_source_model_and_date_stay_unknown_even_though_docprops_carries_dates"
T_DEL = f"{D}::test_deleted_text_is_not_resurrected"
T_BOUND = f"{D}::test_a_declared_oversize_document_is_refused_before_decompression"
T_FAILCLOSED = f"{D}::test_defusedxml_absence_is_fail_closed"
T_ORDER = f"{D}::test_the_registry_selects_docx_before_the_heuristic_transcript_adapter"
T_EMPTYDOC = f"{D}::test_an_empty_document_is_refused_rather_than_imported_as_zero_turns"
T_TABLE = f"{D}::test_the_closed_table_is_shared_with_the_transcript_adapter"
T_DRAW = f"{D}::test_an_embedded_drawing_is_reported_as_not_ingested"
T_STYLEVAC = f"{D}::test_styles_present_means_the_style_reason_is_absent"
T_REVIEW = f"{D}::test_absent_style_markup_is_reported_rather_than_assumed_harmless"
T_VERSION = f"{D}::test_the_supported_report_publishes_the_docx_adapter_version"
T_ORDERDOC = f"{D}::test_a_multi_turn_conversation_keeps_document_order"

T_HIDSRC = f"{H}::test_no_package_source_declares_a_hidden_reasoning_authority_name"
T_HIDCONTRACT = f"{H}::test_the_published_input_contract_offers_no_hidden_reasoning_field"

T_RCI_ROLELESS = f"{R}::test_a_role_less_docx_corpus_ingests_nothing_and_says_why"
T_HOLDOUT = f"{R}::test_an_empty_corpus_produces_no_frozen_holdout"
T_FLOOR = f"{R}::test_a_corpus_below_the_family_floor_refuses_a_holdout_rather_than_carving_one"
T_IDEMP = f"{R}::test_the_build_is_idempotent_over_unchanged_input"
T_PLACE = f"{R}::test_placement_score_is_stable_across_calls_and_the_seed_participates"

T_UNRECOGNISED = f"{M}::test_no_adapter_claims_an_unrecognised_format_rather_than_best_effort_parsing"
T_ABSENTLIST = f"{M}::test_docx_is_no_longer_deliberately_absent_now_that_a_sample_exists"


def _mut(mid, cat, file, find, replace, test):
    return {"id": mid, "cat": cat, "file": file, "find": find,
            "replace": replace, "test": test}


MUTATIONS: list[dict] = []

# ── FORMAT · an unknown shape must not become a known one ────────────────────
MUTATIONS += [
    # "claim any ZIP": sniff stops requiring the WordprocessingML body member.
    _mut("F_sniff_claims_any_zip", "F_FORMAT", DX,
         "                return _DOCUMENT_MEMBER in archive.namelist()",
         "                return True", T_NODOC),
    # "one role is enough": a one-sided document becomes a conversation.
    _mut("F_min_roles_one", "F_FORMAT", DX,
         "MIN_DISTINCT_ROLES = 2",
         "MIN_DISTINCT_ROLES = 1", T_ONESIDED),
    # "import it anyway": the role-less refusal is removed entirely.
    _mut("F_roleless_accepted", "F_FORMAT", DX,
         "        if len(distinct_roles) < MIN_DISTINCT_ROLES:",
         "        if False:", T_ROLELESS),
    # "guess the role": an unrecognised paragraph silently defaults to a user turn.
    _mut("F_role_defaults_to_user", "F_FORMAT", DX,
         "    return (None, \"\", \"\")\n\n\nclass DocxExportAdapter:",
         "    return (TurnRole.USER_REQUEST, \"label:defaulted\", stripped)"
         "\n\n\nclass DocxExportAdapter:", T_ONESIDED),
    # "an empty document is a conversation with zero turns".
    _mut("F_empty_document_accepted", "F_FORMAT", DX,
         "        if not non_empty:",
         "        if False:", T_EMPTYDOC),
    # "best-effort the unknown": an unrecognised extension gets the absent-format
    # message, which claims a sample would fix a format nobody has decided.
    _mut("F_unknown_treated_as_absent", "F_FORMAT", BA,
         "    reason = DELIBERATELY_ABSENT.get(ext)",
         "    reason = DELIBERATELY_ABSENT.get(ext, \"unknown\")", T_UNRECOGNISED),
    # "the format is still absent": the registry loses the adapter it gained.
    _mut("F_docx_still_absent", "F_FORMAT", BA,
         "    \"html\": \"needs sample .html exports: the per-provider DOM differs and a generic \"",
         "    \"docx\": \"re-added\",\n"
         "    \"html\": \"needs sample .html exports: the per-provider DOM differs and a generic \"",
         T_ABSENTLIST),
    # "let the heuristic parser see a binary ZIP first".
    _mut("F_registry_order_inverted", "F_FORMAT", RG,
         "    return (JsonExportAdapter(), DocxExportAdapter(), TranscriptAdapter())",
         "    return (JsonExportAdapter(), TranscriptAdapter(), DocxExportAdapter())",
         T_ORDER),
    # "the table drifts": the shared closed table stops answering.
    _mut("F_closed_table_returns_nothing", "F_FORMAT", TR,
         "    return _LABELS.get(_normalize_label(raw))",
         "    return None", T_TABLE),
    # "the suffix is not ingestible": the real corpus stops reaching its adapter, so the
    # refusal reason becomes a generic suffix skip instead of a structural verdict.
    _mut("F_docx_not_ingestible", "F_FORMAT", PL,
         "    \".docx\",\n",
         "", T_RCI_ROLELESS),
]

# ── PROVENANCE · missing must stay missing ───────────────────────────────────
MUTATIONS += [
    # "the Word save date is the conversation date".
    _mut("P_source_date_fabricated", "P_PROVENANCE", DX,
         "            source_date=\"\",",
         "            source_date=\"2026-09-25\",", T_DATE),
    # "name a model nobody recorded".
    _mut("P_source_model_fabricated", "P_PROVENANCE", DX,
         "            source_model=\"\",",
         "            source_model=\"claude-3\",", T_DATE),
    # "stop publishing the stage version".
    _mut("P_version_unpublished", "P_PROVENANCE", RG,
         "        \"docx_adapter_version\": DOCX_ADAPTER_VERSION,\n",
         "", T_VERSION),
    # "review status is always clean": what could not be established stops being said.
    _mut("P_review_always_ok", "P_PROVENANCE", DX,
         "            review_status=(ReviewStatus.NEEDS_HUMAN_REVIEW if reasons"
         " else ReviewStatus.OK),",
         "            review_status=ReviewStatus.OK,", T_REVIEW),
    # "absent style markup is harmless": the caveat stops being reported.
    _mut("P_style_absence_unreported", "P_PROVENANCE", DX,
         "        if style_count == 0:",
         "        if False:", T_REVIEW),
    # NON-VACUITY mutation: the style caveat fires unconditionally, so a document WITH
    # styles also carries it. A reason that is always present carries no information.
    _mut("P_style_reason_always_fires", "P_PROVENANCE", DX,
         "        if style_count == 0:\n            reasons.append(\n"
         "                \"the document carries no paragraph-style markup",
         "        if True:\n            reasons.append(\n"
         "                \"the document carries no paragraph-style markup", T_STYLEVAC),
    # "the image was not there": an un-ingested attachment stops being declared.
    _mut("P_drawing_unreported", "P_PROVENANCE", DX,
         "        if drawing_count:",
         "        if False:", T_DRAW),
    # "document order is a detail": turns are emitted in label order, not source order.
    _mut("P_order_reversed", "P_PROVENANCE", DX,
         "        for index, text in enumerate(paragraphs):\n"
         "            if index in label_at:",
         "        for index, text in reversed(list(enumerate(paragraphs))):\n"
         "            if index in label_at:", T_ORDERDOC),
]

# ── PRIVACY · deleted and hidden content must stay out ───────────────────────
MUTATIONS += [
    # "resurrect what a human deleted": tracked-change deletions re-enter the corpus.
    _mut("V_deltext_included", "V_PRIVACY", DX,
         "        if tag == f\"{_W}t\":",
         "        if tag in (f\"{_W}t\", f\"{_W}delText\"):", T_DEL),
]

# ── HARDENING · the guards must be load-bearing ──────────────────────────────
MUTATIONS += [
    # "decompress first, ask later": the zip-bomb bound stops being checked.
    _mut("X_size_bound_removed", "X_HARDENING", DX,
         "                if info.file_size > MAX_DOCUMENT_XML_BYTES:",
         "                if False:", T_BOUND),
    # "fall back to the stdlib parser": the fail-closed XML guard is removed.
    _mut("X_defusedxml_not_fail_closed", "X_HARDENING", DX,
         "        if _xml_fromstring is None:",
         "        if False:", T_FAILCLOSED),
]

# ── HIDDEN REASONING · §7's prohibition must be enforced, not merely true ────
MUTATIONS += [
    # A module-level annotated field asserting hidden-reasoning authority.
    _mut("Z_hidden_field_declared", "Z_HIDDEN", DX,
         "DOCX_ADAPTER_VERSION = \"m67a1.docx.1\"",
         "DOCX_ADAPTER_VERSION = \"m67a1.docx.1\"\nchain_of_thought: str = \"\"",
         T_HIDSRC),
    # The same claim smuggled in as a mapping key rather than a declared field.
    _mut("Z_hidden_dict_key", "Z_HIDDEN", DX,
         "            \"paragraphs_total\": len(paragraphs),",
         "            \"chain_of_thought\": \"\",\n"
         "            \"paragraphs_total\": len(paragraphs),", T_HIDSRC),
    # The operator is INVITED to supply one, through the published input contract.
    _mut("Z_hidden_offered_in_contract", "Z_HIDDEN", DX,
         "            \"labelled_paragraphs\": len(labelled),",
         "            \"hidden_reasoning\": \"\",\n"
         "            \"labelled_paragraphs\": len(labelled),", T_HIDSRC),
]

# ── HOLDOUT · an empty corpus must not acquire a frozen holdout ──────────────
MUTATIONS += [
    # THE mutation this milestone exists to survive: the empty corpus is declared
    # splittable, so a holdout gets carved out of nothing.
    _mut("O_empty_corpus_is_defensible", "O_HOLDOUT", SP,
         "                         defensible=False, holdout_available=False)",
         "                         defensible=True, holdout_available=True)", T_HOLDOUT),
    # "the floor is advisory": one family becomes enough for a frozen holdout.
    _mut("O_family_floor_removed", "O_HOLDOUT", CF,
         "    min_families_for_holdout: int = 24",
         "    min_families_for_holdout: int = 0", T_FLOOR),
    # "the holdout is possible whatever the family count".
    #
    # EXPLAINED MASKED SURVIVOR - deliberately retained, and NOT counted as detected. It is
    # listed in EXPLAINED_SURVIVORS below with the measurement that explains it:
    # `holdout_available` ANDs this floor with `min_families_per_partition`, and at a 0.15
    # ratio the per-partition floor needs ~54 families while this one needs 24. So this
    # floor is DOMINATED and no input exists for which mutating it alone changes an
    # outcome. Keeping it documents a real config finding instead of hiding a zero.
    _mut("O_holdout_always_possible", "O_HOLDOUT", SP,
         "    holdout_possible = total_families >= cfg.min_families_for_holdout",
         "    holdout_possible = True", T_FLOOR),
    # "a rerun may repartition": placement stops being a function of its inputs.
    #
    # REPLACES a first-draft mutation that set the `SplitConfig.seed` dataclass DEFAULT to
    # `random.randint(...)`. That mutation SURVIVED, and the reason is worth keeping: a
    # dataclass field default is evaluated ONCE, when the class is created, so every build
    # inside one process shared the same random seed and the digest never moved. The
    # mutation was semantically inert rather than undetected. Placement is mutated here
    # instead, because `placement_score` is called per family per build.
    _mut("O_placement_score_random", "O_HOLDOUT", SP,
         "    return int(digest[:13], 16) / float(1 << 52)",
         "    return __import__(\"random\").random()", T_PLACE),
    # "the seed records nothing": it stops participating in placement, so a frozen holdout
    # built under one seed is reproducible under any other.
    _mut("O_placement_ignores_seed", "O_HOLDOUT", SP,
         "    digest = sha256_text(f\"{SPLIT_ALGORITHM_VERSION}\\x1f{seed}\\x1f{family_id}\")",
         "    digest = sha256_text(f\"{SPLIT_ALGORITHM_VERSION}\\x1f{family_id}\")", T_PLACE),
]


#: Survivors with a MEASURED explanation. A survivor may only appear here with the
#: measurement that shows no input could distinguish mutated from unmutated behaviour.
#: "I could not find a test for it" is not an explanation and does not belong here.
EXPLAINED_SURVIVORS: dict[str, str] = {
    "O_holdout_always_possible":
        "masked by a stricter layer. `holdout_available` = (holdout_possible AND "
        "holdout_count >= min_families_per_partition). Measured: at frozen_holdout_ratio "
        "0.15 the per-partition floor of 8 requires ~54 families, while this floor "
        "requires 24, so for every family count the partition floor decides first. "
        "`min_families_for_holdout = 24` is therefore dead configuration and mutating it "
        "alone cannot change any outcome. Recorded as a residual limitation; the floor was "
        "NOT retuned here, because changing a threshold is a policy decision and this "
        "session has no corpus to justify one.",
}


def _pytest(target: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B603 - fixed argv, shell=False
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:randomly", target],
        cwd=_ROOT, capture_output=True, text=True, check=False)


def _preflight(targets: list[str]) -> list[str]:
    """Every mapped test must PASS, and collect at least one test, BEFORE any mutation."""
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
            print(f"  [ OK ] {target}")
    return bad


def _run() -> int:
    total = len(MUTATIONS)
    targets = sorted({m["test"] for m in MUTATIONS})
    print(f"M67A.1 FALSIFICATION CAMPAIGN — {total} mutations, "
          f"{len(targets)} mapped targets\n")

    vacuous = _preflight(targets)
    if vacuous:
        print("\nPREFLIGHT FAILED — the campaign would be vacuous:")
        for line in vacuous:
            print(f"  {line}")
        print("M67A1_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
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
            print(f"  [ANCHOR ERR] {m['id']:36s} {count}x")
            continue
        mutated = original.replace(m["find"], m["replace"], 1)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(mutated)
            proc = _pytest(m["test"])
            if proc.returncode != 0:
                detected += 1
                print(f"  [DETECTED] {m['id']:36s} ({m['cat']})")
            else:
                survivors.append(m["id"])
                print(f"  [SURVIVOR] {m['id']:36s} ({m['cat']}) — NO TEST FAILED")
        finally:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)

    print(f"\n{'=' * 72}")
    print(f"mutations:      {total}")
    print(f"detected:       {detected}")
    print(f"survivors:      {len(survivors)}  {survivors if survivors else ''}")
    print(f"anchor errors:  {len(anchor_errors)}  {anchor_errors if anchor_errors else ''}")
    unexplained = [s for s in survivors if s not in EXPLAINED_SURVIVORS]
    explained = [s for s in survivors if s in EXPLAINED_SURVIVORS]
    if explained:
        print("\nEXPLAINED survivors (measured as semantically unreachable):")
        for sid in explained:
            print(f"  {sid}: {EXPLAINED_SURVIVORS[sid]}")
    print(f"unexplained:    {len(unexplained)}  {unexplained if unexplained else ''}")
    ok = not unexplained and not anchor_errors
    print(f"M67A1_MUTATION_CAMPAIGN: {'PASS' if ok else 'FAIL'} "
          f"({detected}/{total} detected, {len(explained)} explained, "
          f"{len(unexplained)} unexplained)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_run())
