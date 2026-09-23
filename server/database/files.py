"""文件域数据访问：上传记录与清理所需的查询。"""


class FileStoreMixin:
    """files / message_files 相关操作；连接与事务来自 ConnectionMixin。"""

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

    def list_session_files(self, session_id: str) -> list[dict]:
        """列出某会话关联的全部附件，按消息 id 倒序（最近的最靠前）。"""
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT files.id, files.filename, files.content_type,
                       files.size, files.storage_path, files.upload_status,
                       files.created_at
                FROM message_files
                JOIN files ON files.id = message_files.file_id
                JOIN chat_messages
                  ON chat_messages.id = message_files.message_id
                WHERE chat_messages.session_id = ?
                ORDER BY chat_messages.id DESC, message_files.position
                """,
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def all_file_ids(self) -> set[str]:
        with self.connection() as connection:
            rows = connection.execute("SELECT id FROM files").fetchall()
        return {row["id"] for row in rows}
