"""training_gym.datasets — V69 M62 S2d: where an attempt becomes training data.

This is the package the whole milestone has been building towards, and it is the one
with the least room for a convenience shortcut. Everything before it produced a *verdict
about an attempt*; everything after it consumes a *corpus*. The conversion happens here,
once, explicitly, and it is the last point at which a human is in the loop.

THE FLOW, AND WHY EVERY ARROW IS A GATE
---------------------------------------
::

    Episode
      -> deterministic AggregationReport      (recomputed, never read back)
      -> teacher ConsensusReport              (eligibility only; teachers cannot approve)
      -> hash-bound DatasetHumanReview        (four bindings + the target's own digest)
      -> DatasetCandidate                     (six digests, one of them its own)
      -> privacy / provenance gates           (scanned again at the store boundary)
      -> duplicate and leakage analysis       (exact, normalized, lineage, n-gram)
      -> explicit promotion decision          (dry run, plan digest, bound confirmation)
      -> immutable versioned dataset          (append-only; revoked, never rewritten)
      -> deterministic split manifests        (sticky assignment; a seed is mandatory)
      -> optional SFT or preference export    (TRAIN only, PROMOTED only)

No transition happens implicitly. No conversation becomes training data. No teacher
output becomes training data. No ``corrected_answer`` becomes the ideal target. No
passing grader approves anything. No candidate moves between splits once assigned.

WHAT THIS PACKAGE DOES NOT DO
-----------------------------
It does not train, tokenize, download a tokenizer or a model, call a cloud API, or reach
the network. It does not promote a model, build an adapter, or touch the Model Registry.
It writes JSON and JSONL under a gitignored root and nothing else.

Importing this package performs no I/O and contacts no network.
"""
from __future__ import annotations

from .candidate import (
    ALLOWED_CANDIDATE_TRANSITIONS,
    CANDIDATE_VERSION,
    MAX_CANDIDATE_HASHES,
    MAX_CANDIDATE_TEXT_CHARS,
    MAX_CANDIDATE_TOOL_CALLS,
    NON_EXPORTABLE_SPLITS,
    CandidateError,
    CandidateState,
    CandidateTransitionError,
    DatasetCandidate,
    DatasetSplit,
    SourceType,
    TargetSource,
    check_candidate_transition,
    require_digest,
)
from .candidate_store import (
    CANDIDATE_DIR,
    MAX_CANDIDATE_BYTES,
    MAX_CANDIDATES,
    MAX_STORE_BYTES,
    CandidateStore,
    CandidateStoreError,
)
from .human_review import (
    REVIEW_PROTOCOL_VERSION,
    DatasetHumanReview,
    DatasetReviewDecision,
    DatasetReviewError,
    HumanReviewLedger,
    InMemoryHumanReviewLedger,
    ReviewReplayRejected,
)
from .similarity import (
    BLOCK_THRESHOLD,
    DEFAULT_MAX_COMPARISONS,
    SIMILARITY_VERSION,
    WARN_THRESHOLD,
    SimilarityError,
    SimilarityResult,
    compare_groups,
    normalize_for_similarity,
    normalized_key,
)
from .split import (
    DEFAULT_RATIOS,
    SPLIT_ALGORITHM_VERSION,
    InMemorySplitAssignmentLedger,
    SplitAssignmentLedger,
    SplitError,
    SplitPlan,
    SplitPolicy,
    StickyConflict,
    commit_plan,
    group_candidates,
    leakage_group_key,
    plan_splits,
)
from .leakage import (
    LEAKAGE_VERSION,
    MANDATORY_CHECKS,
    LeakageAnalyzer,
    LeakageError,
    LeakageFinding,
    LeakagePolicy,
    LeakageReport,
    LeakageVerdict,
)
from .promotion import (
    PROMOTION_POLICY_VERSION,
    build_candidate,
    candidate_blockers,
    prepare_target_text,
    refuse_hidden_field_names,
    schema_answer_exposure,
)
from .manifests import (
    DATASET_MANIFEST_VERSION,
    DATASET_ROOT_DIR,
    GENESIS_PARENT,
    MANIFEST_FILENAME,
    SHARD_SPLITS,
    DatasetVersionManifest,
    ManifestCandidateRow,
    ManifestError,
    RevocationSnapshot,
    ShardManifest,
    VersionVerification,
    WrittenVersion,
    encode_shard,
    latest_manifest_hash,
    load_manifest,
    read_shard,
    reviewer_safe_id,
    shard_filename,
    verify_parent,
    verify_version,
    version_dir,
    write_dataset_version,
)
from .promotion_plan import (
    CONFIRMATION_PREFIX,
    PLAN_SCHEMA_VERSION,
    PROMOTION_LEDGER_FILE,
    SPLIT_LEDGER_FILE,
    ConfirmationRejected,
    PlanAlreadyConsumed,
    PromotionError,
    PromotionPlan,
    PromotionRequest,
    PromotionResult,
    check_confirmation,
    describe_plan,
    is_plan_consumed,
    plan_promotion,
    promote,
    promotion_entries,
    split_ledger_path,
)
from .export import (
    EXPORT_DIR,
    EXPORT_VERSION,
    EXPORTABLE_SPLITS,
    SFT_FILENAME,
    SFT_MANIFEST_FILENAME,
    SFT_SOURCE_SPLIT,
    SFT_VALIDATION_FILENAME,
    SFT_VALIDATION_MANIFEST_FILENAME,
    SFT_VALIDATION_SOURCE_SPLIT,
    ExportError,
    ExportVerification,
    SFTExport,
    SFTExportManifest,
    export_dir,
    export_sft,
    export_sft_validation,
    row_hash,
    sft_row,
    verify_sft_export,
    verify_sft_validation_export,
)
from .preference import (
    PAIR_SIMILARITY_CEILING,
    PREFERENCE_FILENAME,
    PREFERENCE_MANIFEST_FILENAME,
    PREFERENCE_REVIEW_VERSION,
    PREFERENCE_VERSION,
    PreferenceDecision,
    PreferenceError,
    PreferenceExport,
    PreferenceExportManifest,
    PreferenceHumanReview,
    PreferencePair,
    RejectedResponse,
    RejectionReason,
    build_preference_pair,
    export_preference,
    pair_blockers,
    pair_similarity,
    verify_preference_export,
)
from .bridge import (
    BRIDGE_SOURCE_SPLIT,
    BRIDGE_VERSION,
    BridgeError,
    BridgeReport,
    bridge_blockers,
    bridge_dataset_version,
    to_training_example,
    verify_bridged,
)

__all__ = [
    "BRIDGE_SOURCE_SPLIT", "BRIDGE_VERSION", "BridgeError", "BridgeReport",
    "bridge_blockers", "bridge_dataset_version", "to_training_example",
    "verify_bridged",
    "EXPORTABLE_SPLITS", "EXPORT_DIR", "EXPORT_VERSION", "SFT_FILENAME",
    "SFT_MANIFEST_FILENAME", "SFT_SOURCE_SPLIT", "SFT_VALIDATION_FILENAME",
    "SFT_VALIDATION_MANIFEST_FILENAME", "SFT_VALIDATION_SOURCE_SPLIT", "ExportError",
    "ExportVerification", "SFTExport", "SFTExportManifest", "export_dir", "export_sft",
    "export_sft_validation", "row_hash", "sft_row", "verify_sft_export",
    "verify_sft_validation_export",
    "PAIR_SIMILARITY_CEILING", "PREFERENCE_FILENAME", "PREFERENCE_MANIFEST_FILENAME",
    "PREFERENCE_REVIEW_VERSION", "PREFERENCE_VERSION", "PreferenceDecision",
    "PreferenceError", "PreferenceExport", "PreferenceExportManifest",
    "PreferenceHumanReview", "PreferencePair", "RejectedResponse", "RejectionReason",
    "build_preference_pair", "export_preference", "pair_blockers", "pair_similarity",
    "verify_preference_export",
    "CONFIRMATION_PREFIX", "PLAN_SCHEMA_VERSION", "PROMOTION_LEDGER_FILE",
    "SPLIT_LEDGER_FILE", "ConfirmationRejected", "PlanAlreadyConsumed",
    "PromotionError", "PromotionPlan", "PromotionRequest", "PromotionResult",
    "check_confirmation", "describe_plan", "is_plan_consumed", "plan_promotion",
    "promote", "promotion_entries", "split_ledger_path",
    "DATASET_MANIFEST_VERSION", "DATASET_ROOT_DIR", "GENESIS_PARENT",
    "MANIFEST_FILENAME", "SHARD_SPLITS", "DatasetVersionManifest",
    "ManifestCandidateRow", "ManifestError", "RevocationSnapshot", "ShardManifest",
    "VersionVerification", "WrittenVersion", "encode_shard", "latest_manifest_hash",
    "load_manifest", "read_shard", "reviewer_safe_id", "shard_filename",
    "verify_parent", "verify_version", "version_dir", "write_dataset_version",
    "ALLOWED_CANDIDATE_TRANSITIONS", "BLOCK_THRESHOLD", "CANDIDATE_DIR",
    "CANDIDATE_VERSION", "DEFAULT_MAX_COMPARISONS", "DEFAULT_RATIOS",
    "LEAKAGE_VERSION", "MANDATORY_CHECKS", "MAX_CANDIDATES",
    "MAX_CANDIDATE_BYTES", "MAX_CANDIDATE_HASHES", "MAX_CANDIDATE_TEXT_CHARS",
    "MAX_CANDIDATE_TOOL_CALLS", "MAX_STORE_BYTES", "NON_EXPORTABLE_SPLITS",
    "PROMOTION_POLICY_VERSION", "REVIEW_PROTOCOL_VERSION",
    "SIMILARITY_VERSION", "SPLIT_ALGORITHM_VERSION", "WARN_THRESHOLD",
    "CandidateError", "CandidateState", "CandidateStore",
    "CandidateStoreError", "CandidateTransitionError", "DatasetCandidate",
    "DatasetHumanReview", "DatasetReviewDecision", "DatasetReviewError",
    "DatasetSplit", "HumanReviewLedger", "InMemoryHumanReviewLedger",
    "InMemorySplitAssignmentLedger", "LeakageAnalyzer", "LeakageError",
    "LeakageFinding", "LeakagePolicy", "LeakageReport", "LeakageVerdict",
    "ReviewReplayRejected", "SimilarityError", "SimilarityResult",
    "SourceType", "SplitAssignmentLedger", "SplitError", "SplitPlan",
    "SplitPolicy", "StickyConflict", "TargetSource", "build_candidate",
    "candidate_blockers", "check_candidate_transition", "commit_plan",
    "compare_groups", "group_candidates", "leakage_group_key",
    "normalize_for_similarity", "normalized_key", "plan_splits",
    "prepare_target_text", "refuse_hidden_field_names", "require_digest",
    "schema_answer_exposure",
]
