"""training_gym.evaluation.instruments — V69 M62 S4H: FUTURE evaluation instruments.

WHAT THIS PACKAGE IS
--------------------
An ADDITIVE, version-pinned set of measurement instruments for evaluations that have not
happened yet. S4F measured candidate 005; S4G interpreted the result; S4H asks whether
the NEXT measurement can be trusted more, and this package is that answer in code.

WHAT IT IS NOT
--------------
It is not a re-scorer. No module here is imported by ``scoring.py``, ``gates.py``,
``policy.py``, ``statistics.py``, ``metrics.py``, ``comparison.py`` or ``reports.py``,
and the four frozen scorer digests

    gates       e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5
    statistics  663ebf65b73536fe3bd41043568a1f737ff751a43a964d14ff67c4e12662bf18
    families    580fbe9104cbe684f702cba016e1191c83745fb8502642636c3fc885135065b1
    metrics     e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a

re-derive unchanged with this package present. A receipt sealed before S4H is verified by
the code that sealed it, and there is no "latest instrument" for it to drift onto.

It creates no authority. Building a stack, a contract or a finding is a description, not
permission: nothing here loads a model, attaches an adapter, generates a token, opens a
holdout body, or spends anything.

THE SLOTS
---------
    secret_pii        rule-level provenance and a disclosure/mention distinction
    refusal           structural decline detection, and the leak-after-refusal case
    tool_call         real schema validation, and absence as a result
    coverage          execution completeness separated from quality comparability
    runtime_contract  device, dtype, revisions and cache identity, enforced
    finding_schema    the body-safe record every instrument emits
    calibration       synthetic rates, labelled as synthetic

Each is pinned by exact version in :mod:`.stack`, which refuses "latest".
"""
from __future__ import annotations

from .calibration import (
    INSTRUMENT_CALIBRATION_VERSION,
    SYNTHETIC_CALIBRATION,
    CalibrationCase,
    CalibrationReport,
    Label,
    run_calibration,
)
from .coverage_v2 import (
    COVERAGE_SEMANTICS_VERSION,
    CoverageAccounting,
    CoverageError,
    ExecutionCoverage,
    QualityComparability,
)
from .finding import (
    FINDING_SCHEMA_VERSION,
    ContextClass,
    EvidenceClass,
    FindingError,
    InstrumentFinding,
    PlaceholderState,
    RedactionState,
    SemanticClass,
    Severity,
    evidence_digest,
)
from .refusal_v2 import (
    REFUSAL_BEHAVIOR_VERSION,
    RefusalClassification,
    RefusalOutcome,
    RefusalTaskKind,
    RefusalTaskSpec,
)
from .refusal_v2 import classify as classify_refusal_v2
from .runtime_contract import (
    RUNTIME_CONTRACT_VERSION,
    AdapterStrategy,
    DType,
    Device,
    LoadStrategy,
    ObservedRuntime,
    RuntimeContract,
    RuntimeContractViolation,
    enforce_observed_runtime,
    local_cpu_profile,
)
from .secret_pii_v2 import (
    SECRET_PII_DETECTOR_VERSION,
    SECRET_RULES,
    DetectionReport,
    SecretDetectionPolicy,
    decide,
    scan_payload,
    scan_text,
)
from .stack import (
    INSTRUMENT_REGISTRY,
    INSTRUMENT_STACK_VERSION,
    REQUIRED_SLOTS,
    InstrumentStack,
    InstrumentStackError,
    current_versions,
    validate_config,
)
from .tool_call_v2 import (
    TOOL_CALL_VALIDATOR_VERSION,
    ReasonCode,
    ToolCallPolicy,
    ToolCallValidation,
    ToolSchema,
    build_catalogue,
    validate_response,
)

#: This package's own identity. Distinct from every instrument version inside it, and
#: from the evaluation protocol version, which is a different concept entirely.
INSTRUMENTS_PACKAGE_VERSION = "m62.evaluation_instruments.1"

__all__ = [
    "AdapterStrategy", "COVERAGE_SEMANTICS_VERSION", "CalibrationCase",
    "CalibrationReport", "ContextClass", "CoverageAccounting", "CoverageError",
    "DType", "DetectionReport", "Device", "EvidenceClass", "ExecutionCoverage",
    "FINDING_SCHEMA_VERSION", "FindingError", "INSTRUMENTS_PACKAGE_VERSION",
    "INSTRUMENT_CALIBRATION_VERSION", "INSTRUMENT_REGISTRY",
    "INSTRUMENT_STACK_VERSION", "InstrumentFinding", "InstrumentStack",
    "InstrumentStackError", "Label", "LoadStrategy", "ObservedRuntime",
    "PlaceholderState", "QualityComparability", "REFUSAL_BEHAVIOR_VERSION",
    "REQUIRED_SLOTS", "RUNTIME_CONTRACT_VERSION", "ReasonCode", "RedactionState",
    "RefusalClassification", "RefusalOutcome", "RefusalTaskKind", "RefusalTaskSpec",
    "RuntimeContract", "RuntimeContractViolation", "SECRET_PII_DETECTOR_VERSION",
    "SECRET_RULES", "SYNTHETIC_CALIBRATION", "SecretDetectionPolicy", "SemanticClass",
    "Severity", "TOOL_CALL_VALIDATOR_VERSION", "ToolCallPolicy", "ToolCallValidation",
    "ToolSchema", "build_catalogue", "classify_refusal_v2", "current_versions",
    "decide", "enforce_observed_runtime", "evidence_digest", "local_cpu_profile",
    "run_calibration", "scan_payload", "scan_text", "validate_config",
    "validate_response",
]
