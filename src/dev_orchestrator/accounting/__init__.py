"""Execution accounting and failure-memory primitives.

The package is deliberately independent from the orchestration coordinators so
synthetic/offline reports use exactly the same contracts as live recording.
"""

from .events import (
    EVENT_TYPES,
    PHASES,
    ROLES,
    CorruptionReport,
    EventCorruptionError,
    EventReadResult,
    EventWriteError,
    ExecutionEventStore,
    ExecutionRecorder,
    InterProcessFileLock,
)
from .failure_memory import (
    FailureLesson,
    FailureMemory,
    environment_for_project,
    lesson_fingerprint,
)
from .evidence import (
    DimensionContinuity,
    FailoverEvidence,
    ProviderEvidenceSummary,
    RDCAnalysis,
    RDCFinding,
    RDCInvocationEvidence,
    RDCThresholds,
    classify_rdc_evidence,
    import_rdc_evidence,
    normalize_rdc_observation,
    plan_rdc_recovery,
    summarize_provider_evidence,
)
from .intervals import (
    AccountedInterval,
    AccountingSummary,
    IntervalBuildResult,
    build_intervals,
    construct_intervals,
    summarize_accounting,
)
from .reporting import (
    AcceptanceGate,
    AcceptanceThresholds,
    Bottleneck,
    P11Report,
    build_p11_report,
    reporting_event_store,
)

__all__ = [
    "EVENT_TYPES",
    "PHASES",
    "ROLES",
    "AccountedInterval",
    "AccountingSummary",
    "AcceptanceGate",
    "AcceptanceThresholds",
    "Bottleneck",
    "CorruptionReport",
    "DimensionContinuity",
    "EventCorruptionError",
    "EventReadResult",
    "EventWriteError",
    "ExecutionEventStore",
    "ExecutionRecorder",
    "FailureLesson",
    "FailureMemory",
    "FailoverEvidence",
    "InterProcessFileLock",
    "IntervalBuildResult",
    "ProviderEvidenceSummary",
    "P11Report",
    "RDCAnalysis",
    "RDCFinding",
    "RDCInvocationEvidence",
    "RDCThresholds",
    "build_intervals",
    "build_p11_report",
    "construct_intervals",
    "classify_rdc_evidence",
    "environment_for_project",
    "lesson_fingerprint",
    "import_rdc_evidence",
    "normalize_rdc_observation",
    "plan_rdc_recovery",
    "reporting_event_store",
    "summarize_provider_evidence",
    "summarize_accounting",
]
