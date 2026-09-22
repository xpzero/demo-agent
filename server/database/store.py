"""组合入口：Database 以 mixin 汇聚连接、会话与文件域能力。"""

from .connection import ConnectionMixin
from .files import FileStoreMixin
from .sessions import SessionStoreMixin

__all__ = ["Database"]


class Database(ConnectionMixin, SessionStoreMixin, FileStoreMixin):
    """SQLite 数据访问门面：对外保持 database.xxx() 调用面不变。"""
