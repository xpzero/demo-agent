from dataclasses import dataclass
from typing import Literal, Protocol, Sequence


PermissionAction = Literal["allow", "ask", "deny"]


@dataclass(frozen=True)
class PermissionRequest:
    permission: str
    target: str = "*"


@dataclass(frozen=True)
class PermissionCheckContext:
    call_id: str
    tool_name: str
    session_id: int | None = None


@dataclass(frozen=True)
class PermissionDecision:
    action: PermissionAction
    reason: str | None = None


class PermissionService(Protocol):
    def check(
        self,
        requests: Sequence[PermissionRequest],
        context: PermissionCheckContext,
    ) -> PermissionDecision: ...
