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
from .intervals import (
    AccountedInterval,
    AccountingSummary,
    IntervalBuildResult,
    build_intervals,
    construct_intervals,
    summarize_accounting,
)

__all__ = [
    "EVENT_TYPES",
    "PHASES",
    "ROLES",
    "AccountedInterval",
    "AccountingSummary",
    "CorruptionReport",
    "EventCorruptionError",
    "EventReadResult",
    "EventWriteError",
    "ExecutionEventStore",
    "ExecutionRecorder",
    "FailureLesson",
    "FailureMemory",
    "InterProcessFileLock",
    "IntervalBuildResult",
    "build_intervals",
    "construct_intervals",
    "environment_for_project",
    "lesson_fingerprint",
    "summarize_accounting",
]
