from dataclasses import dataclass
from typing import Protocol

from .session import Session


class SessionError(Exception):
    """Session 领域错误基类。"""


class SessionNotFound(SessionError):
    def __init__(self, session_id: int):
        super().__init__(f"没有 {session_id} 号会话")
        self.session_id = session_id


class SessionRevisionConflict(SessionError):
    def __init__(self, session_id: int):
        super().__init__(f"{session_id} 号会话已被更新，请重新加载后重试")
        self.session_id = session_id


class SessionDataError(SessionError):
    """Session 内容无法安全写入存储。"""


class SessionStorageError(SessionError):
    """Session 存储暂时不可用。"""


class SessionOutcomeUncertain(SessionError):
    def __init__(self, session_id: int):
        super().__init__(
            f"Session {session_id} 的工具执行已经开始，但结果未能保存；"
            "请检查实际状态后再重试"
        )
        self.session_id = session_id


@dataclass(frozen=True)
class SessionSummary:
    id: int
    summary: str
    message_count: int
    revision: int


class SessionService(Protocol):
    """会话存取接口；每次读取返回独立的 Session 快照。"""

    def create(self, system_prompt: str) -> Session: ...

    def get(self, session_id: int) -> Session | None: ...

    def list_sessions(self) -> list[SessionSummary]: ...

    def save(self, session: Session) -> None: ...

    def delete(self, session_id: int, expected_revision: int) -> bool: ...

    def close(self) -> None: ...
