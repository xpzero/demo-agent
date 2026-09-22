"""SQLite 数据访问层：持久化会话、消息、文件及消息附件关系。"""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

DATABASE_PATH = Path(__file__).resolve().parents[1] / ".data" / "demo-agent.sqlite3"


class StoreError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class Database:
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

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
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
            )

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

    def create_file(self, record: dict) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO files (
                    id, filename, content_type, size, storage_path,
                    upload_status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'uploaded', ?)
                """,
                (
                    record["file_id"],
                    record["filename"],
                    record["content_type"],
                    record["size"],
                    record["storage_path"],
                    record["created_at"],
                ),
            )

    def get_file(self, file_id: str) -> dict | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM files WHERE id = ?", (file_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete_file_if_unreferenced(self, file_id: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM files
                WHERE id = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM message_files WHERE file_id = files.id
                  )
                """,
                (file_id,),
            )
            return cursor.rowcount == 1

    def list_expired_unreferenced_files(self, before: float) -> list[dict]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT files.* FROM files
                WHERE files.created_at < ?
                  AND NOT EXISTS (
                    SELECT 1 FROM message_files
                    WHERE message_files.file_id = files.id
                  )
                ORDER BY files.created_at
                """,
                (before,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def normalize_session_id(session_id: str) -> str:
        try:
            return str(UUID(session_id))
        except (ValueError, AttributeError) as error:
            raise StoreError("invalid_session_id", "session_id 必须是 UUID") from error

    def create_user_turn(
        self,
        *,
        session_id: str,
        parent_message_id: int | None,
        content: str,
        file_ids: list[str],
    ) -> int:
        session_id = self.normalize_session_id(session_id)
        if len(file_ids) > 1:
            raise StoreError("too_many_files", "当前只支持单个附件")
        now = time.time()
        title = content.strip()[:30] or "新对话"

        with self.transaction() as connection:
            session = connection.execute(
                "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                if parent_message_id is not None:
                    raise StoreError(
                        "session_not_found",
                        "会话不存在，首条消息不能指定父消息",
                        404,
                    )
                connection.execute(
                    """
                    INSERT INTO chat_sessions (
                        id, title, current_message_id, created_at, updated_at
                    ) VALUES (?, ?, NULL, ?, ?)
                    """,
                    (session_id, title, now, now),
                )
            else:
                if parent_message_id is None:
                    raise StoreError(
                        "parent_message_required",
                        "已有会话必须指定 parent_message_id",
                        409,
                    )
                if session["current_message_id"] != parent_message_id:
                    raise StoreError(
                        "stale_parent_message",
                        "会话已更新，请刷新历史后重试",
                        409,
                    )
                parent = connection.execute(
                    """
                    SELECT id FROM chat_messages
                    WHERE id = ? AND session_id = ?
                    """,
                    (parent_message_id, session_id),
                ).fetchone()
                if parent is None:
                    raise StoreError(
                        "invalid_parent_message",
                        "父消息不属于当前会话",
                        409,
                    )

            for file_id in file_ids:
                file_row = connection.execute(
                    "SELECT id FROM files WHERE id = ?", (file_id,)
                ).fetchone()
                if file_row is None:
                    raise StoreError("file_not_found", "附件不存在", 404)
                foreign_session = connection.execute(
                    """
                    SELECT 1
                    FROM message_files
                    JOIN chat_messages
                      ON chat_messages.id = message_files.message_id
                    WHERE message_files.file_id = ?
                      AND chat_messages.session_id != ?
                    LIMIT 1
                    """,
                    (file_id, session_id),
                ).fetchone()
                if foreign_session is not None:
                    raise StoreError(
                        "file_belongs_to_other_session",
                        "附件已属于其他会话",
                        409,
                    )

            cursor = connection.execute(
                """
                INSERT INTO chat_messages (
                    session_id, parent_id, role, status, content, created_at
                ) VALUES (?, ?, 'user', 'finished', ?, ?)
                """,
                (session_id, parent_message_id, content, now),
            )
            message_id = int(cursor.lastrowid)
            for position, file_id in enumerate(file_ids):
                connection.execute(
                    """
                    INSERT INTO message_files (message_id, file_id, position)
                    VALUES (?, ?, ?)
                    """,
                    (message_id, file_id, position),
                )
            connection.execute(
                """
                UPDATE chat_sessions
                SET current_message_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (message_id, now, session_id),
            )
        return message_id

    def add_assistant_message(
        self, *, session_id: str, parent_message_id: int, content: str
    ) -> int:
        now = time.time()
        with self.transaction() as connection:
            session = connection.execute(
                "SELECT current_message_id FROM chat_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise StoreError("session_not_found", "会话不存在", 404)
            if session["current_message_id"] != parent_message_id:
                raise StoreError(
                    "stale_parent_message",
                    "会话已更新，不能覆盖较新的消息",
                    409,
                )
            parent = connection.execute(
                """
                SELECT id FROM chat_messages
                WHERE id = ? AND session_id = ?
                """,
                (parent_message_id, session_id),
            ).fetchone()
            if parent is None:
                raise StoreError("invalid_parent_message", "父消息不属于当前会话", 409)
            cursor = connection.execute(
                """
                INSERT INTO chat_messages (
                    session_id, parent_id, role, status, content, created_at
                ) VALUES (?, ?, 'assistant', 'finished', ?, ?)
                """,
                (session_id, parent_message_id, content, now),
            )
            message_id = int(cursor.lastrowid)
            connection.execute(
                """
                UPDATE chat_sessions
                SET current_message_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (message_id, now, session_id),
            )
        return message_id

    def get_message_chain(self, session_id: str, message_id: int) -> list[dict]:
        chain = []
        current_id: int | None = message_id
        with self.connection() as connection:
            while current_id is not None:
                row = connection.execute(
                    """
                    SELECT id, parent_id, role, content
                    FROM chat_messages
                    WHERE id = ? AND session_id = ?
                    """,
                    (current_id, session_id),
                ).fetchone()
                if row is None:
                    raise StoreError("message_not_found", "消息不存在", 404)
                chain.append(dict(row))
                current_id = row["parent_id"]
        chain.reverse()
        return chain

    def list_sessions(self) -> list[dict]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, title, current_message_id, created_at, updated_at
                FROM chat_sessions
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_session_history(self, session_id: str) -> dict | None:
        with self.connection() as connection:
            session = connection.execute(
                """
                SELECT id, title, current_message_id, created_at, updated_at
                FROM chat_sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if session is None:
                return None
            rows = connection.execute(
                """
                SELECT id, parent_id, role, status, content, created_at
                FROM chat_messages
                WHERE session_id = ?
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()
            messages = []
            for row in rows:
                files = connection.execute(
                    """
                    SELECT files.id, files.filename, files.content_type,
                           files.size, files.upload_status
                    FROM message_files
                    JOIN files ON files.id = message_files.file_id
                    WHERE message_files.message_id = ?
                    ORDER BY message_files.position
                    """,
                    (row["id"],),
                ).fetchall()
                message = dict(row)
                message["files"] = [dict(file_row) for file_row in files]
                messages.append(message)
        return {"session": dict(session), "messages": messages}

    def all_file_ids(self) -> set[str]:
        with self.connection() as connection:
            rows = connection.execute("SELECT id FROM files").fetchall()
        return {row["id"] for row in rows}
