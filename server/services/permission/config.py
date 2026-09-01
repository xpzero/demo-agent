import json
from pathlib import Path
from typing import cast

from .rules import PermissionRule
from .service import PermissionAction


def _validate_action(value: object, location: str) -> PermissionAction:
    if value not in {"allow", "ask", "deny"}:
        raise ValueError(
            f"权限配置 {location} 必须是 allow、ask 或 deny，实际为 {value!r}"
        )
    return cast(PermissionAction, value)


def _validate_pattern(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"权限配置 {location} 必须是非空字符串")
    return value


def load_permission_rules(path: Path) -> tuple[PermissionRule, ...]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"无法加载权限配置 {path}：{error}") from error

    if not isinstance(config, dict):
        raise ValueError("权限配置顶层必须是 JSON 对象")

    rules = []
    for raw_permission, setting in config.items():
        permission = _validate_pattern(raw_permission, "permission")
        if isinstance(setting, str):
            rules.append(
                PermissionRule(
                    permission,
                    "*",
                    _validate_action(setting, permission),
                    path.name,
                )
            )
            continue

        if not isinstance(setting, dict) or not setting:
            raise ValueError(
                f"权限配置 {permission} 必须是 action 或非空 target 对象"
            )
        for raw_target, raw_action in setting.items():
            target = _validate_pattern(raw_target, f"{permission}.target")
            rules.append(
                PermissionRule(
                    permission,
                    target,
                    _validate_action(raw_action, f"{permission}.{target}"),
                    path.name,
                )
            )

    return tuple(rules)
