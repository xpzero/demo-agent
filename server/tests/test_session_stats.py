import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from database import Database

SESSION_ID = "0b6f3c4e-8d5a-4c0a-9d5e-2f1a7b8c9d02"


class SessionStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tmp.name) / "test.sqlite3")
        self.database.initialize()
        self.session_id = str(uuid4())
        self.first_user_id = self.database.create_user_turn(
            session_id=self.session_id,
            parent_message_id=None,
            content="第一问",
            file_ids=[],
        )
        self.assistant_id = self.database.add_assistant_message(
            session_id=self.session_id,
            parent_message_id=self.first_user_id,
            content="第一答",
        )
        self.second_user_id = self.database.create_user_turn(
            session_id=self.session_id,
            parent_message_id=self.assistant_id,
            content="第二问",
            file_ids=[],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def record_for(self, message_id, **turn):
        turn.setdefault("duration_ms", 100.0)
        turn.setdefault("tools", [])
        self.database.record_agent_turns(
            message_id=message_id, turns=[turn]
        )

    def test_empty_session_returns_zero_stats(self):
        stats = self.database.get_session_stats(self.session_id)
        self.assertEqual(
            stats,
            {
                "turns": 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "estimated_prompt_tokens": 0,
                "estimated": False,
            },
        )

    def test_aggregates_turns_across_messages(self):
        # 第一轮：实测 usage；第二轮：只有估算值
        self.record_for(
            self.first_user_id,
            prompt_tokens=100,
            completion_tokens=20,
            estimated_prompt_tokens=100,
            estimated_completion_tokens=20,
        )
        self.record_for(
            self.second_user_id,
            prompt_tokens=None,
            completion_tokens=None,
            estimated_prompt_tokens=200,
            estimated_completion_tokens=30,
        )

        stats = self.database.get_session_stats(self.session_id)
        self.assertEqual(stats["turns"], 2)
        # 实测合计只算有实测的行
        self.assertEqual(stats["total_prompt_tokens"], 100)
        self.assertEqual(stats["total_completion_tokens"], 20)
        # 展示口径：估算列人人都有，缺失行用估算补位
        self.assertEqual(stats["estimated_prompt_tokens"], 300)
        # 任一行缺实测即整体标 estimated
        self.assertTrue(stats["estimated"])

    def test_all_measured_session_not_estimated(self):
        self.record_for(
            self.first_user_id,
            prompt_tokens=10,
            completion_tokens=5,
            estimated_prompt_tokens=10,
            estimated_completion_tokens=5,
        )
        stats = self.database.get_session_stats(self.session_id)
        self.assertFalse(stats["estimated"])
        self.assertEqual(stats["total_prompt_tokens"], 10)

    def test_unknown_session_raises(self):
        from database.connection import StoreError

        with self.assertRaises(StoreError):
            self.database.get_session_stats(str(uuid4()))


if __name__ == "__main__":
    unittest.main()
