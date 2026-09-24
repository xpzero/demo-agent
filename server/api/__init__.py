"""API 包入口：组装 FastAPI 应用并对外保持 `api.app` 调用面不变。

`uvicorn api:app` 与测试里的 `import api` 都指向这里；
_running_sessions / get_database 等共享件从 deps 转发，
测试的 `api._running_sessions` 访问习惯不变。
"""

from .deps import _lock, _running_sessions, get_database  # noqa: F401
from .routes import app

__all__ = ["app"]
