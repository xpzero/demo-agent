import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("API_KEY", "test-key")

from agent.context_budget import (
    SUMMARY_PREFIX, build_context, choose_to_absorb, maybe_roll,
)
from database import Database
from database.connection import SCHEMA


def row(message_id, role, content):
    return {"id": message_id, "role": role, "content": content}


class RollingContextTests(unittest.TestCase):
    def test_summary_cursor_and_budget_fallback(self):
        chain = [row(1, "user", "旧问题"), row(2, "assistant", "旧答案"),
                 row(3, "user", "a" * 80), row(4, "assistant", "b" * 80),
                 row(5, "user", "现在的问题")]
        self.assertEqual([item["content"] for item in build_context("sys", chain, budget=1000)],
                         ["sys", "旧问题", "旧答案", "a" * 80, "b" * 80, "现在的问题"])
        items = build_context("sys", chain, budget=110, summary="决定保留", summary_upto_message_id=2)
        self.assertEqual(items[0], {"role": "system", "content": "sys"})
        self.assertEqual(items[1], {"role": "user", "content": SUMMARY_PREFIX + "决定保留"})
        self.assertEqual([item["content"] for item in items[2:]], ["b" * 80, "现在的问题"])
        self.assertEqual(build_context("sys", [row(5, "user", "x" * 200)], budget=50,
                                       summary="旧摘要", summary_upto_message_id=2)[-1]["content"], "x" * 200)

    def test_roll_threshold_cap_and_backlog(self):
        living = [row(i, "user", str(i) * 1000) for i in range(1, 18)]
        first = choose_to_absorb(living)
        self.assertEqual([item["id"] for item in first], list(range(1, 9)))
        self.assertEqual([item["id"] for item in choose_to_absorb(living[8:])], [9, 10, 11, 12])
        self.assertEqual(choose_to_absorb([row(1, "user", "x" * 12_000)]), [])
        oversized = choose_to_absorb([row(1, "user", "x" * 10_000), row(2, "user", "y" * 8_000)])
        self.assertEqual(len(oversized[0]["content"]), 8_000)


class RollingStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.sqlite3")
        self.db.initialize()
        self.sid = str(uuid4())
        self.parent = None
        for index in range(8):
            uid = self.db.create_user_turn(
                session_id=self.sid, parent_message_id=self.parent,
                content=str(index) * 1000, file_ids=[],
            )
            self.parent = self.db.add_assistant_message(
                session_id=self.sid, parent_message_id=uid, content="答" * 1000,
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_roll_updates_cursor_atomically_and_keeps_original_messages(self):
        with patch("agent.context_budget.summarize", return_value="新摘要") as summarize:
            self.assertTrue(maybe_roll(self.db, self.sid, self.parent))
        self.assertEqual(self.db.get_session_summary(self.sid), ("新摘要", 8))
        self.assertEqual(len(self.db.get_message_chain(self.sid, self.parent)), 16)
        self.assertEqual(len(self.db.get_session_history(self.sid)["messages"]), 16)
        self.assertEqual(self.db.get_session_history(self.sid)["session"]["summary_upto_message_id"], 8)
        self.assertIsNone(summarize.call_args.args[0])
        self.assertFalse(self.db.update_session_summary(self.sid, None, "过期", 10))

        uid = self.db.create_user_turn(session_id=self.sid, parent_message_id=self.parent,
                                       content="再问" * 2000, file_ids=[])
        aid = self.db.add_assistant_message(session_id=self.sid, parent_message_id=uid,
                                            content="答" * 1000)
        with patch("agent.context_budget.summarize", return_value="合并摘要") as summarize:
            self.assertTrue(maybe_roll(self.db, self.sid, aid))
        self.assertEqual(summarize.call_args.args[0], "新摘要")
        self.assertEqual(self.db.get_session_summary(self.sid), ("合并摘要", 13))

    def test_failed_roll_does_not_advance_cursor_and_can_retry(self):
        with patch("agent.context_budget.summarize", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                maybe_roll(self.db, self.sid, self.parent)
        self.assertEqual(self.db.get_session_summary(self.sid), (None, None))
        with patch("agent.context_budget.summarize", return_value="恢复"):
            self.assertTrue(maybe_roll(self.db, self.sid, self.parent))
        self.assertEqual(self.db.get_session_summary(self.sid), ("恢复", 8))

    def test_legacy_schema_migrates_idempotently(self):
        path = Path(self.tmp.name) / "old.sqlite3"
        legacy = SCHEMA.replace("    summary TEXT,\n    summary_upto_message_id INTEGER,\n", "")
        with sqlite3.connect(path) as connection:
            connection.executescript(legacy)
            connection.execute("INSERT INTO chat_sessions (id, title, created_at, updated_at) VALUES (?, 'old', 1, 1)", (self.sid,))
        db = Database(path)
        db.initialize()
        db.initialize()
        self.assertEqual(db.get_session_summary(self.sid), (None, None))
        self.assertIsNone(db.get_session_history(self.sid)["session"]["summary_upto_message_id"])
