"""SQLite 连接、事务与建表：数据访问层的公共底座。"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATABASE_PATH = Path(__file__).resolve().parents[1] / ".data" / "demo-agent.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    current_message_id INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (current_message_id)
        REFERENCES chat_messages(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    parent_id INTEGER,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    status TEXT NOT NULL CHECK (status IN ('finished', 'failed')),
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (session_id)
        REFERENCES chat_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id)
        REFERENCES chat_messages(id)
);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    storage_path TEXT NOT NULL,
    upload_status TEXT NOT NULL CHECK (upload_status = 'uploaded'),
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS message_files (
    message_id INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, file_id),
    FOREIGN KEY (message_id)
        REFERENCES chat_messages(id) ON DELETE CASCADE,
    FOREIGN KEY (file_id)
        REFERENCES files(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON chat_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_parent
    ON chat_messages(parent_id);
CREATE INDEX IF NOT EXISTS idx_files_created
    ON files(created_at);
CREATE INDEX IF NOT EXISTS idx_message_files_file
    ON message_files(file_id);
"""


class StoreError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ConnectionMixin:
    """连接与事务管理：sqlite3 的 with 只管提交回滚，这里补上显式关闭。"""

    def __init__(self, path: Path = DATABASE_PATH):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def connection(self):
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
