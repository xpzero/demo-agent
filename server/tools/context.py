"""工具执行上下文：把会话与存储位置显式递给需要它们的工具。"""

from pathlib import Path

from database import Database


class SessionContext:
    """一次 Chat 请求内共享的会话信息。

    由 API 层在每轮 Chat 开始时构造，只在该轮 SSE 生命周期内使用，
    不跨请求缓存。database 的每个操作都独立开短连接，
    因此本对象可以在流式生成器里安全地长期持有。
    """

    def __init__(self, *, session_id: str, database: Database, file_root: Path):
        self.session_id = session_id
        self.database = database
        self.file_root = file_root
