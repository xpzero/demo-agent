"""消息生命周期域：message_fragments 表与助手消息的预建、终态、判死回填。"""

import json
import time

from .connection import StoreError
from .sessions import normalize_session_id

# 发送即预建时刻起算的执行截止（秒）；孤儿判死唯一依据
MESSAGE_DEADLINE_SECONDS = 180

FRAGMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_fragments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    status TEXT,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (message_id)
        REFERENCES chat_messages(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fragments_msg_seq
    ON message_fragments(message_id, seq);
"""

LIFECYCLE_COLUMNS = (
    ("status", "ALTER TABLE chat_messages ADD COLUMN status TEXT NOT NULL DEFAULT 'finished'"),
    ("finish_kind", "ALTER TABLE chat_messages ADD COLUMN finish_kind TEXT"),
    ("deadline_at", "ALTER TABLE chat_messages ADD COLUMN deadline_at REAL"),
    ("stop_requested", "ALTER TABLE chat_messages ADD COLUMN stop_requested INTEGER NOT NULL DEFAULT 0"),
)

# 片段类型；terminal 行是恢复回放的终点标志
FRAGMENT_TYPES = frozenset({"text", "tool_call", "tool_result", "terminal"})
# 终态细分；unfinished → finished 单向，落定后不可改写
FINISH_KINDS = frozenset({"done", "stopped", "error", "max_turns", "interrupted"})


class MessageLifecycleMixin:
    """chat_messages 生命周期列与 message_fragments 相关操作。"""

    def initialize_message_lifecycle(self) -> None:
        """幂等迁移：补齐生命周期四列并建 fragments 表。

        旧表 status 列带 CHECK（基座与 #11 时代两种变体），与 unfinished
        冲突；检测到即重建 chat_messages（去 CHECK，单向流转由应用层
        保证——终态只在 finalize 的 WHERE 中写入）。
        重建必须关外键（message_files 引用本表），而 SQLite 禁止事务内
        改 PRAGMA foreign_keys——因此本迁移用独立连接手动管理顺序：
        PRAGMA OFF → BEGIN → DDL → COMMIT → PRAGMA ON。
        幂等：残留的 chat_messages_new 先清理；已是新表则跳过重建。
        """
        import sqlite3 as _sqlite3

        connection = self.connect()
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DROP TABLE IF EXISTS chat_messages_new")
                schema_row = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_messages'"
                ).fetchone()
                has_status_check = bool(
                    schema_row and "CHECK (status IN (" in schema_row["sql"]
                )
                if has_status_check:
                    existing = {
                        row["name"]
                        for row in connection.execute("PRAGMA table_info(chat_messages)")
                    }
                    extras = [name for name in existing if name not in
                              ("id", "session_id", "parent_id", "role", "status",
                               "content", "created_at")]
                    create_extra = "".join(f", {name} TEXT" for name in extras)
                    select_extra = "".join(f", {name}" for name in extras)
                    connection.execute(f"""
                        CREATE TABLE chat_messages_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            session_id TEXT NOT NULL,
                            parent_id INTEGER,
                            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                            status TEXT NOT NULL,
                            content TEXT NOT NULL,
                            created_at REAL NOT NULL{create_extra},
                            FOREIGN KEY (session_id)
                                REFERENCES chat_sessions(id) ON DELETE CASCADE,
                            FOREIGN KEY (parent_id)
                                REFERENCES chat_messages(id)
                        )""")
                    connection.execute(f"""
                        INSERT INTO chat_messages_new
                            SELECT id, session_id, parent_id, role, status, content,
                                   created_at{select_extra}
                            FROM chat_messages""")
                    connection.execute("DROP TABLE chat_messages")
                    connection.execute(
                        "ALTER TABLE chat_messages_new RENAME TO chat_messages")
                    # #11 时代的 incomplete 行归一：历史会话仍可读
                    connection.execute(
                        "UPDATE chat_messages SET status = 'finished' WHERE status = 'incomplete'"
                    )
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(chat_messages)")
                }
                for column, ddl in LIFECYCLE_COLUMNS:
                    if column not in columns:
                        connection.execute(ddl)
                for statement in FRAGMENTS_SCHEMA.strip().split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.close()

    # ── 预建与终态 ──────────────────────────────────────────────

    def create_pending_assistant(
        self, session_id: str, parent_message_id: int
    ) -> int:
        """发送即预建：unfinished 助手行 + deadline；无第二条路径。"""
        session_id = normalize_session_id(session_id)
        now = time.time()
        with self.transaction() as connection:
            parent = connection.execute(
                "SELECT id FROM chat_messages WHERE id = ? AND session_id = ?",
                (parent_message_id, session_id),
            ).fetchone()
            if parent is None:
                raise StoreError("invalid_parent_message", "父消息不存在", 404)
            cursor = connection.execute(
                """
                INSERT INTO chat_messages (
                    session_id, parent_id, role, status, content, created_at,
                    deadline_at
                ) VALUES (?, ?, 'assistant', 'unfinished', '', ?, ?)
                """,
                (session_id, parent_message_id, now,
                 now + MESSAGE_DEADLINE_SECONDS),
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

    def get_unfinished_message(self, session_id: str) -> dict | None:
        """会话内至多一条未完成助手行（会话锁保证）。"""
        session_id = normalize_session_id(session_id)
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT id, parent_id, status, content, deadline_at, stop_requested
                FROM chat_messages
                WHERE session_id = ? AND role = 'assistant'
                  AND status = 'unfinished'
                ORDER BY id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        message = dict(row) if row else None
        if message is not None and message["deadline_at"] is not None:
            # ALTER 增列的 affinity 可能返回 str（表重建历史），统一转 float
            message["deadline_at"] = float(message["deadline_at"])
        return message

    def request_stop(self, session_id: str) -> dict:
        """置停止意图（判死者读它区分 stopped / interrupted）。"""
        session_id = normalize_session_id(session_id)
        message = self.get_unfinished_message(session_id)
        if message is None:
            return {"result": "no_active", "message_id": None}
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE chat_messages SET stop_requested = 1
                WHERE id = ? AND status = 'unfinished'
                """,
                (message["id"],),
            )
        return {"result": "processing", "message_id": message["id"]}

    def finalize_assistant(
        self, message_id: int, finish_kind: str, content: str | None = None
    ) -> bool:
        """终态单向落定：unfinished → finished 一次写入，此后 no-op。

        content 为 None 时保留原值（判死回填由调用方先拼好传入）。
        返回是否实际落定（False = 已是终态）。
        """
        if finish_kind not in FINISH_KINDS:
            raise StoreError("invalid_finish_kind", f"无效终态：{finish_kind}")
        with self.transaction() as connection:
            result = connection.execute(
                """
                UPDATE chat_messages
                SET status = 'finished',
                    finish_kind = ?,
                    content = COALESCE(?, content),
                    deadline_at = NULL
                WHERE id = ? AND status = 'unfinished'
                """,
                (finish_kind, content, message_id),
            )
            return result.rowcount == 1

    # ── fragments ──────────────────────────────────────────────

    def append_fragment(
        self, message_id: int, type_: str, payload: dict,
        status: str | None = None, *, connection=None,
    ) -> int:
        """INSERT 子查询发号（单写者 + SQLite 串行写保证原子），返回 seq。"""
        if type_ not in FRAGMENT_TYPES:
            raise StoreError("invalid_fragment_type", f"无效片段类型：{type_}")
        insert = """
            INSERT INTO message_fragments (message_id, seq, type, status, payload, created_at)
            VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1
                        FROM message_fragments WHERE message_id = ?),
                    ?, ?, ?, ?)
            RETURNING seq
        """
        params = (message_id, message_id, type_, status,
                  json.dumps(payload, ensure_ascii=False), time.time())
        if connection is not None:
            row = connection.execute(insert, params).fetchone()
            return int(row[0])
        with self.transaction() as conn:
            row = conn.execute(insert, params).fetchone()
            return int(row[0])

    def list_fragments(self, message_id: int) -> list[dict]:
        """按 seq 升序返回全部片段；payload 解析为 dict。"""
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, seq, type, status, payload, created_at
                FROM message_fragments WHERE message_id = ?
                ORDER BY seq
                """,
                (message_id,),
            ).fetchall()
        fragments = []
        for row in rows:
            fragment = dict(row)
            fragment["payload"] = json.loads(fragment["payload"])
            fragments.append(fragment)
        return fragments

    def has_terminal_fragment(self, message_id: int) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM message_fragments WHERE message_id = ? AND type = 'terminal' LIMIT 1",
                (message_id,),
            ).fetchone()
        return row is not None

    def assemble_text(self, message_id: int) -> str:
        """按 seq 拼 text 片段全文；终态回填与判死共用。"""
        return "".join(
            fragment["payload"].get("text", "")
            for fragment in self.list_fragments(message_id)
            if fragment["type"] == "text"
        )
