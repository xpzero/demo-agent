"""组合入口：Database 以 mixin 汇聚连接、会话与文件域能力。"""

from .connection import ConnectionMixin
from .files import FileStoreMixin
from .metrics import MetricsStoreMixin
from .sessions import SessionStoreMixin
from .runs import RunStoreMixin
from .recovery import RecoveryStoreMixin

__all__ = ["Database"]


class Database(ConnectionMixin, SessionStoreMixin, FileStoreMixin, MetricsStoreMixin, RunStoreMixin, RecoveryStoreMixin):
    """SQLite 数据访问门面：对外保持 database.xxx() 调用面不变。"""

    def initialize(self) -> None:
        super().initialize()
        # Keep the legacy initializer callable when callers recreate an old table.
        self.initialize_session_summary()
        self.initialize_metrics()
