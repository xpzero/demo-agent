"""可观测性域数据访问：agent_turns / tool_runs 账本与消息 meta 挂牌。"""

import json

from .connection import StoreError
from .sessions import normalize_session_id

METRICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    duration_ms REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    estimated_prompt_tokens INTEGER,
    estimated_completion_tokens INTEGER,
    created_at REAL NOT NULL,
    FOREIGN KEY (message_id)
        REFERENCES chat_messages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tool_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_turn_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    args_excerpt TEXT NOT NULL,
    result_excerpt TEXT NOT NULL,
    duration_ms REAL,
    ok INTEGER NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (agent_turn_id)
        REFERENCES agent_turns(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_turns_message
    ON agent_turns(message_id);
CREATE INDEX IF NOT EXISTS idx_tool_runs_turn
    ON tool_runs(agent_turn_id);
"""


class MetricsStoreMixin:
    """agent_turns / tool_runs / chat_messages.meta 相关操作。"""

    def initialize_metrics(self) -> None:
        """建观测表并给旧库的 chat_messages 补 meta 列（幂等）。"""
        import time

        with self.transaction() as connection:
            connection.executescript(METRICS_SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(chat_messages)"
                ).fetchall()
            }
            if "meta" not in columns:
                connection.execute(
                    "ALTER TABLE chat_messages ADD COLUMN meta TEXT"
                )

    def record_agent_turns(self, *, message_id: int, turns: list[dict]) -> None:
        """把一次 Chat 的内存账本落库（一次事务）。

        turns 由 api.py 从 TurnRecorder 导出；message_id 锚定本轮
        用户消息——轮开始前已存在，中途失败也不丢账。
        """
        import time

        with self.transaction() as connection:
            message = connection.execute(
                "SELECT id FROM chat_messages WHERE id = ?", (message_id,)
            ).fetchone()
            if message is None:
                raise StoreError("message_not_found", "消息不存在", 404)
            now = time.time()
            for turn in turns:
                cursor = connection.execute(
                    """
                    INSERT INTO agent_turns (
                        message_id, duration_ms, prompt_tokens,
                        completion_tokens, estimated_prompt_tokens,
                        estimated_completion_tokens, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        turn.get("duration_ms"),
                        turn.get("prompt_tokens"),
                        turn.get("completion_tokens"),
                        turn.get("estimated_prompt_tokens"),
                        turn.get("estimated_completion_tokens"),
                        now,
                    ),
                )
                for tool in turn.get("tools", []):
                    connection.execute(
                        """
                        INSERT INTO tool_runs (
                            agent_turn_id, name, args_excerpt,
                            result_excerpt, duration_ms, ok, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cursor.lastrowid,
                            tool["name"],
                            tool.get("args_excerpt", ""),
                            tool.get("result_excerpt", ""),
                            tool.get("duration_ms"),
                            1 if tool.get("ok") else 0,
                            now,
                        ),
                    )

    def get_session_stats(self, session_id: str) -> dict:
        """会话级观测汇总：对 agent_turns 现场 SUM 聚合，不存累计数。

        账本是唯一事实来源，能推导的数据不冗余存。
        - total_*：只累计有实测 usage 的行；
        - estimated_*：展示口径，每行优先实测，缺失时用估算补位；
        - estimated：任一行有缺失实测即 True。
        """
        session_id = normalize_session_id(session_id)
        with self.connection() as connection:
            session = connection.execute(
                "SELECT id FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise StoreError("session_not_found", "会话不存在", 404)
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS turns,
                    COALESCE(SUM(prompt_tokens), 0) AS total_prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0)
                        AS total_completion_tokens,
                    COALESCE(SUM(COALESCE(prompt_tokens, estimated_prompt_tokens)), 0)
                        AS estimated_prompt_tokens,
                    COALESCE(SUM(COALESCE(completion_tokens, estimated_completion_tokens)), 0)
                        AS estimated_completion_tokens,
                    SUM(prompt_tokens IS NULL OR completion_tokens IS NULL)
                        AS missing_usage
                FROM agent_turns
                WHERE message_id IN (
                    SELECT id FROM chat_messages WHERE session_id = ?
                )
                """,
                (session_id,),
            ).fetchone()
        missing = row["missing_usage"] or 0
        return {
            "turns": row["turns"],
            "total_prompt_tokens": row["total_prompt_tokens"],
            "total_completion_tokens": row["total_completion_tokens"],
            "estimated_prompt_tokens": row["estimated_prompt_tokens"],
            "estimated_completion_tokens": row["estimated_completion_tokens"],
            # 任一用量缺实测即标记；零账（空会话）不算 estimated
            "estimated": bool(missing),
        }

    def set_message_meta(self, message_id: int, meta: dict) -> None:
        """把汇总观测数据挂到助手消息的 meta 挂牌（JSON 列）。"""
        with self.transaction() as connection:
            result = connection.execute(
                "UPDATE chat_messages SET meta = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), message_id),
            )
            if result.rowcount == 0:
                raise StoreError("message_not_found", "消息不存在", 404)

    def get_message_meta(self, message_id: int) -> dict | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT meta FROM chat_messages WHERE id = ?", (message_id,)
            ).fetchone()
        if row is None or row["meta"] is None:
            return None
        return json.loads(row["meta"])
