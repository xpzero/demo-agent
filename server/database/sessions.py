"""会话域数据访问：会话、消息链与父指针约束。"""

import json
import time
from contextlib import nullcontext
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

    def initialize_session_summary(self) -> None:
        """迁移现有 SQLite 会话表，不改变已保存的原始消息。"""
        with self.transaction() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(chat_sessions)")}
            if "summary" not in columns:
                connection.execute("ALTER TABLE chat_sessions ADD COLUMN summary TEXT")
            if "summary_upto_message_id" not in columns:
                connection.execute("ALTER TABLE chat_sessions ADD COLUMN summary_upto_message_id INTEGER")

    def get_session_summary(self, session_id: str) -> tuple[str | None, int | None] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT summary, summary_upto_message_id FROM chat_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return (row["summary"], row["summary_upto_message_id"]) if row else None

    def update_session_summary(
        self, session_id: str, previous_cursor: int | None,
        summary: str, upto_message_id: int,
    ) -> bool:
        """用游标比较并覆盖一份摘要，避免并行摘要回写旧结果。"""
        with self.transaction() as connection:
            result = connection.execute(
                """UPDATE chat_sessions SET summary = ?, summary_upto_message_id = ?
                   WHERE id = ? AND summary_upto_message_id IS ?
                     AND (summary_upto_message_id IS NULL OR summary_upto_message_id < ?)""",
                (summary, upto_message_id, session_id, previous_cursor, upto_message_id),
            )
            return result.rowcount == 1

    def create_user_turn(
        self,
        *,
        session_id: str,
        parent_message_id: int | None,
        content: str,
        file_ids: list[str],
    ) -> int:
        with self.transaction() as connection:
            return self._create_user_turn(connection, session_id, parent_message_id, content, file_ids)

    def _create_user_turn(self, connection, session_id, parent_message_id, content, file_ids):
        session_id = normalize_session_id(session_id)
        if len(file_ids) > 1:
            raise StoreError("too_many_files", "当前只支持单个附件")
        now = time.time()
        title = content.strip()[:30] or "新对话"

        with nullcontext(connection):  # Reuse the caller's transaction.
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
                SELECT id, title, current_message_id, summary, summary_upto_message_id,
                       created_at, updated_at
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

    # 历史进度为只读快照：不改写任何持久化事实，Run 终态与结论
    # 保持当时落库的值，Task/Operation 呈现最新已知状态。
    _RUN_CONCLUSION_LABELS = {
        "completed": "本轮消息处理完毕（完成不代表各业务操作都成功）",
        "stopped": "应你的停止请求结束了本轮处理",
        "failed": "后端未能完成本轮必要判断或答复",
        "timed_out": "等待业务反馈达到总时限，本轮以超时结束",
        "limit_reached": "达到模型请求次数上限，本轮结束",
        "running": "正在处理本条消息",
        "finishing": "正在收尾：核实在途业务操作并保存事实",
    }

    _PROGRESS_TASK_STATUS_LABELS = {
        "pending": "已登记，尚未推进",
        "running": "正在推进",
        "active": "目标明确，可继续推进",
        "waiting_input": "等待你补充信息",
        "waiting_business": "已提交操作尚待业务方确认结束",
        "completed": "目标已完成且必要结果已确认",
    }

    _PROGRESS_OPERATION_STATUS_LABELS = {
        "pending": "not_started",
        "maybe_submitted": "maybe_submitted",
        "submitted": "maybe_submitted",
        "processing": "processing",
        "running": "processing",
        "unknown": "unconfirmed",
        "unconfirmed": "unconfirmed",
        "succeeded": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
        "completed": "unconfirmed",
    }

    def _session_run_conclusions(self, connection, session_id: str) -> list[dict]:
        rows = connection.execute(
            """
            SELECT id, user_message_id, status, reason, stop_requested,
                   created_at, finished_at
            FROM runs WHERE session_id = ? ORDER BY id
            """,
            (session_id,),
        ).fetchall()
        conclusions = []
        for row in rows:
            conclusion = dict(row)
            label = self._RUN_CONCLUSION_LABELS.get(row["status"])
            if label is None:
                # 未知状态只说明记录不可解读，不推测业务结论
                conclusion["conclusion"] = "运行记录状态未知"
            elif row["status"] == "timed_out" and row["reason"] == "user_stop":
                conclusion["conclusion"] = (
                    "停止后等待业务反馈达到总时限，本轮以超时结束；"
                    "未确认的操作保留待核实状态"
                )
            else:
                conclusion["conclusion"] = label
            conclusions.append(conclusion)
        return conclusions

    def _session_task_progress(self, connection, session_id: str) -> list[dict]:
        tasks = connection.execute(
            """
            SELECT id, status, name, goal_version, created_at, updated_at
            FROM tasks WHERE session_id = ? ORDER BY id
            """,
            (session_id,),
        ).fetchall()
        progress = []
        for task in tasks:
            entry = {
                "id": task["id"],
                "goal": task["name"],
                "version": task["goal_version"],
                "status": task["status"],
                "status_label": self._PROGRESS_TASK_STATUS_LABELS.get(
                    task["status"], "状态未知"
                ),
                "created_at": task["created_at"],
                "updated_at": task["updated_at"],
                "operations": [],
                "steps": [],
            }
            for op in connection.execute(
                """
                SELECT id, run_id, step_id, name, status, conflict,
                       confirmed_result, business_operation_id, updated_at
                FROM operations WHERE task_id = ? ORDER BY id
                """,
                (task["id"],),
            ).fetchall():
                entry["operations"].append({
                    "id": op["id"],
                    "run_id": op["run_id"],
                    "step_id": op["step_id"],
                    "tool": op["name"],
                    # 状态语义与恢复快照一致：可靠结果只来自已确认事实
                    "status": self._PROGRESS_OPERATION_STATUS_LABELS.get(
                        op["status"], "unconfirmed"
                    ),
                    "conflict": bool(op["conflict"]),
                    "confirmed_result": op["confirmed_result"],
                    "business_operation_id": op["business_operation_id"],
                    "updated_at": op["updated_at"],
                })
            for step in connection.execute(
                """
                SELECT step_id, tool, args_json, depends_on, approved
                FROM planned_steps WHERE task_id = ? ORDER BY created_at, step_id
                """,
                (task["id"],),
            ).fetchall():
                entry["steps"].append({
                    "step_id": step["step_id"],
                    "tool": step["tool"],
                    "args": json.loads(step["args_json"]) if step["args_json"] else {},
                    "depends_on": json.loads(step["depends_on"]) if step["depends_on"] else [],
                    "approved": bool(step["approved"]),
                })
            progress.append(entry)
        return progress

    def get_session_progress(self, session_id: str) -> dict | None:
        """只读快照：Run 结论 + Task/Operation 最新事实，供历史查询展示。"""
        session_id = normalize_session_id(session_id)
        with self.connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if exists is None:
                return None
            return {
                "runs": self._session_run_conclusions(connection, session_id),
                "tasks": self._session_task_progress(connection, session_id),
            }
