from dataclasses import dataclass
from pathlib import Path

from .permission import PermissionService, RulePermissionService, load_permission_rules


DEFAULT_PERMISSION_PATH = Path(__file__).resolve().parents[1] / "permission.json"


@dataclass(frozen=True)
class ServiceContainer:
    permission: PermissionService


def create_default_services(
    permission_path: Path = DEFAULT_PERMISSION_PATH,
) -> ServiceContainer:
    rules = load_permission_rules(permission_path)
    return ServiceContainer(permission=RulePermissionService(rules))
