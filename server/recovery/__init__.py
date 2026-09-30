"""Pure recovery snapshot construction and model suggestion validation.

The parent loads authoritative records, builds a snapshot, asks the model for a
suggestion, then reloads records and calls validate_suggestion with a fresh
snapshot before any scheduling. A permitted action still requires the parent's
atomic Run/Task/Operation check and business adapter authorization at dispatch.
"""

from .validation import (
    OperationFact,
    PlannedStep,
    RecoverySnapshot,
    TaskFact,
    Validation,
    build_task_snapshot,
    validate_suggestion,
)

__all__ = [
    "OperationFact", "PlannedStep", "RecoverySnapshot", "TaskFact", "Validation",
    "build_task_snapshot", "validate_suggestion",
]
