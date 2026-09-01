import re
from dataclasses import dataclass
from typing import Sequence

from .service import (
    PermissionAction,
    PermissionCheckContext,
    PermissionDecision,
    PermissionRequest,
)


@dataclass(frozen=True)
class PermissionRule:
    permission: str
    target: str
    action: PermissionAction
    source: str = "builtin"


def _matches(value: str, pattern: str) -> bool:
    value = value.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    expression = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.fullmatch(expression, value, flags=re.DOTALL) is not None


class RulePermissionService:
    def __init__(
        self,
        rules: Sequence[PermissionRule],
        default_action: PermissionAction = "ask",
    ):
        self._rules = tuple(rules)
        self._default_action = default_action

    def _evaluate(self, request: PermissionRequest) -> tuple[PermissionAction, str]:
        for rule in reversed(self._rules):
            if _matches(request.permission, rule.permission) and _matches(
                request.target, rule.target
            ):
                return (
                    rule.action,
                    f"匹配 {rule.source} 规则：{rule.permission} {rule.target}",
                )
        return self._default_action, "没有匹配规则，使用默认权限"

    def check(
        self,
        requests: Sequence[PermissionRequest],
        context: PermissionCheckContext,
    ) -> PermissionDecision:
        del context
        if not requests:
            return PermissionDecision("deny", "工具没有声明权限请求")

        evaluated = [self._evaluate(request) for request in requests]
        for action, reason in evaluated:
            if action == "deny":
                return PermissionDecision("deny", reason)
        for action, reason in evaluated:
            if action == "ask":
                return PermissionDecision("ask", reason)
        return PermissionDecision("allow", evaluated[-1][1])
