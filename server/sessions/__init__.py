"""多会话管理：Session 模型、持久化服务与斜杠命令。"""

from .commands import handle as handle_command
from .factory import create_default_session_service
from .postgres import PostgresSessionService
from .service import (
    SessionDataError,
    SessionError,
    SessionNotFound,
    SessionOutcomeUncertain,
    SessionRevisionConflict,
    SessionService,
    SessionStorageError,
    SessionSummary,
)
from .session import Session

__all__ = [
    "PostgresSessionService",
    "Session",
    "SessionDataError",
    "SessionError",
    "SessionNotFound",
    "SessionOutcomeUncertain",
    "SessionRevisionConflict",
    "SessionService",
    "SessionStorageError",
    "SessionSummary",
    "create_default_session_service",
    "handle_command",
]
