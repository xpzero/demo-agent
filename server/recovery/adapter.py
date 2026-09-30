"""Business evidence contract. Transport completion is not business completion.

Adapters implement query/cancel against the original stable operation identifier.
A final observation is authoritative only if the adapter can guarantee the stated
business outcome; cancellation must guarantee the action cannot later take effect.
"""
from dataclasses import dataclass
from typing import Protocol

FINAL = frozenset({"succeeded", "failed", "cancelled"})
NONFINAL = frozenset({"processing", "unconfirmed"})


@dataclass(frozen=True)
class Capabilities:
    queryable: bool
    cancellable: bool
    idempotent_submission: bool
    # The adapter, rather than the model, approves independent steps and release conditions.


@dataclass(frozen=True)
class Observation:
    status: str
    result: str | None = None
    detail: str | None = None

    def __post_init__(self):
        if self.status not in FINAL | NONFINAL:
            raise ValueError("invalid business observation")
        if (self.status in FINAL) != (self.result is not None):
            raise ValueError("final observations require a result; nonfinal observations cannot have one")
        if self.result is not None and not isinstance(self.result, str):
            raise ValueError("result must be text")


class BusinessAdapter(Protocol):
    capabilities: Capabilities

    def approve(self, task_id: int, goal_version: int, step_id: str, tool: str,
                args: dict, dependencies: tuple[int, ...]) -> bool:
        """Authorize exact arguments and dependency release, including independence."""
        ...

    def submit(self, operation_id: int, tool: str, args: dict) -> tuple[str | None, Observation]:
        """Submit once after durable begin; return optional business ID and evidence."""
        ...

    def query(self, operation_id: int, business_operation_id: str | None) -> Observation:
        """Query original action without resubmission."""
        ...

    def cancel(self, operation_id: int, business_operation_id: str | None) -> Observation:
        """A cancellation request being accepted is only 'processing'."""
        ...
