import tempfile
import unittest
from pathlib import Path

from database import Database
from database.connection import StoreError


class MetricsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tmp.name) / "test.sqlite3")
        self.database.initialize()
        self.session_id = self.database.create_user_turn(
            session_id="0b6f3c4e-8d5a-4c0a-9d5e-2f1a7b8c9d01",
            parent_message_id=None,
            content="你好",
            file_ids=[],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_agent_turns_inserts_rows_and_tool_runs(self):
        self.database.record_agent_turns(
            message_id=self.session_id,
            turns=[
                {
                    "duration_ms": 1200.5,
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "estimated_prompt_tokens": 120,
                    "estimated_completion_tokens": 60,
                    "tools": [
                        {
                            "name": "web_search",
                            "args_excerpt": '{"query": "天气"}',
                            "result_excerpt": "北京晴",
                            "duration_ms": 300.0,
                            "ok": True,
                        },
                        {
                            "name": "calculate",
                            "args_excerpt": '{"expression": "1/0"}',
                            "result_excerpt": "calculate 执行出错：…",
                            "duration_ms": 5.0,
                            "ok": False,
                        },
                    ],
                },
                {
                    "duration_ms": 800.0,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "estimated_prompt_tokens": 200,
                    "estimated_completion_tokens": 30,
                    "tools": [],
                },
            ],
        )
        with self.database.connection() as connection:
            turns = connection.execute(
                "SELECT * FROM agent_turns ORDER BY id"
            ).fetchall()
            runs = connection.execute(
                "SELECT * FROM tool_runs ORDER BY id"
            ).fetchall()
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["message_id"], self.session_id)
        self.assertEqual(turns[0]["prompt_tokens"], 100)
        self.assertIsNone(turns[1]["prompt_tokens"])
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["agent_turn_id"], turns[0]["id"])
        self.assertEqual(runs[1]["agent_turn_id"], turns[0]["id"])
        self.assertEqual(runs[0]["ok"], 1)
        self.assertEqual(runs[1]["ok"], 0)

    def test_record_rejects_unknown_message(self):
        with self.assertRaises(StoreError):
            self.database.record_agent_turns(message_id=99999, turns=[])

    def test_set_and_get_message_meta(self):
        self.database.set_message_meta(
            self.session_id, {"turns": 2, "total_tokens": 150}
        )
        meta = self.database.get_message_meta(self.session_id)
        self.assertEqual(meta, {"turns": 2, "total_tokens": 150})

    def test_get_message_meta_returns_none_when_absent(self):
        self.assertIsNone(self.database.get_message_meta(self.session_id))

    def test_set_message_meta_rejects_unknown_message(self):
        with self.assertRaises(StoreError):
            self.database.set_message_meta(99999, {"turns": 1})

    def test_existing_database_gets_meta_column_via_migration(self):
        # 模拟旧库：手工建一张没有 meta 列的 chat_messages 再 initialize
        with self.database.connection() as connection:
            connection.execute("DROP TABLE chat_messages")
            connection.execute(
                """
                CREATE TABLE chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    parent_id INTEGER,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
        self.database.initialize()  # 迁移：补 meta 列
        with self.database.connection() as connection:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(chat_messages)"
                ).fetchall()
            }
        self.assertIn("meta", columns)

    def test_initialize_is_idempotent(self):
        self.database.initialize()
        self.database.initialize()  # 不应因重复加列报错
        with self.database.connection() as connection:
            columns = [
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(chat_messages)"
                ).fetchall()
            ]
        self.assertEqual(columns.count("meta"), 1)


if __name__ == "__main__":
    unittest.main()
