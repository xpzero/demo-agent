"""会话域数据访问：会话、消息链与父指针约束。"""

import time
from uuid import UUID

from .connection import StoreError


def normalize_session_id(session_id: str) -> str:
    try:
        return str(UUID(session_id))
    except (ValueError, AttributeError) as error:
        raise StoreError("invalid_session_id", "session_id 必须是 UUID") from error


class SessionStoreMixin:
    """chat_sessions / chat_messages 相关操作；连接与事务来自 ConnectionMixin。"""

    normalize_session_id = staticmethod(normalize_session_id)

    def create_user_turn(
        self,
        *,
        session_id: str,
        parent_message_id: int | None,
        content: str,
        file_ids: list[str],
    ) -> int:
        session_id = normalize_session_id(session_id)
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
                if row["role"] == "assistant" and row["parent_id"] is not None:
                    runs = connection.execute(
                        """SELECT tool_runs.id, tool_runs.name, tool_runs.args_excerpt,
                                  tool_runs.result_excerpt, tool_runs.duration_ms
                           FROM tool_runs JOIN agent_turns
                             ON agent_turns.id = tool_runs.agent_turn_id
                           WHERE agent_turns.message_id = ?
                           ORDER BY agent_turns.id, tool_runs.id""",
                        (row["parent_id"],),
                    ).fetchall()
                    message["tool_runs"] = [dict(run) for run in runs]
                message["files"] = [dict(file_row) for file_row in files]
                messages.append(message)
        return {"session": dict(session), "messages": messages}
