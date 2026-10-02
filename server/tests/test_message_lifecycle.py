"""M1 数据层：预建、终态单向、fragments 发号与拼接。"""

import sys
import tempfile
import uuid
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import Database  # noqa: E402


def make_database() -> Database:
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    tmp.close()
    database = Database(Path(tmp.name))
    database.initialize()
    self = unittest.TestCase()
    return database


class LifecycleTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        tmp.close()
        self.database = Database(Path(tmp.name))
        self.database.initialize()
        self.addCleanup(Path(tmp.name).unlink)

    def start_turn(self, content="你好"):
        session_id = str(uuid.uuid4())
        user_id = self.database.create_user_turn(
            session_id=session_id, parent_message_id=None,
            content=content, file_ids=[],
        )
        return session_id, user_id


class TestPendingAssistant(LifecycleTestBase):
    def test_create_pending_sets_unfinished_and_deadline(self):
        session_id, user_id = self.start_turn()
        before = time.time()
        message_id = self.database.create_pending_assistant(session_id, user_id)
        message = self.database.get_unfinished_message(session_id)
        self.assertEqual(message["id"], message_id)
        self.assertEqual(message["status"], "unfinished")
        self.assertEqual(message["content"], "")
        self.assertEqual(message["stop_requested"], 0)
        self.assertGreater(message["deadline_at"], before + 179)
        self.assertLess(message["deadline_at"], before + 181)

    def test_no_unfinished_returns_none(self):
        session_id, user_id = self.start_turn()
        self.assertIsNone(self.database.get_unfinished_message(session_id))

    def test_invalid_parent_rejected(self):
        session_id, _ = self.start_turn()
        with self.assertRaises(Exception):
            self.database.create_pending_assistant(session_id, 99999)

    def test_pending_updates_session_cursor(self):
        session_id, user_id = self.start_turn()
        message_id = self.database.create_pending_assistant(session_id, user_id)
        sessions = self.database.list_sessions()
        self.assertEqual(sessions[0]["current_message_id"], message_id)


class TestFinalize(LifecycleTestBase):
    def _fresh_turn(self):
        session_id = str(uuid.uuid4())
        uid = self.database.create_user_turn(
            session_id=session_id, parent_message_id=None,
            content="q", file_ids=[],
        )
        return session_id, uid

    def test_finalize_done_writes_content_once(self):
        session_id, user_id = self.start_turn()
        message_id = self.database.create_pending_assistant(session_id, user_id)
        self.assertTrue(
            self.database.finalize_assistant(message_id, "done", "回复全文")
        )
        self.assertIsNone(self.database.get_unfinished_message(session_id))
        # 终态单向：二次 finalize 为 no-op
        self.assertFalse(
            self.database.finalize_assistant(message_id, "stopped", "覆盖")
        )
        history = self.database.get_session_history(session_id)
        assistant = history["messages"][-1]
        self.assertEqual(assistant["content"], "回复全文")
        self.assertEqual(assistant["status"], "finished")

    def test_finalize_null_content_keeps_existing(self):
        session_id, user_id = self.start_turn()
        message_id = self.database.create_pending_assistant(session_id, user_id)
        self.database.finalize_assistant(message_id, "done", "第一版")
        self.database.finalize_assistant(message_id, "error", None)
        history = self.database.get_session_history(session_id)
        self.assertEqual(history["messages"][-1]["content"], "第一版")

    def test_all_finish_kinds_accepted(self):
        session_id, user_id = self.start_turn()
        for kind in ("done", "stopped", "error", "max_turns", "interrupted"):
            session_id, uid = self._fresh_turn()
            mid = self.database.create_pending_assistant(session_id, uid)
            self.assertTrue(self.database.finalize_assistant(mid, kind, None))

    def test_invalid_kind_rejected(self):
        session_id, user_id = self.start_turn()
        message_id = self.database.create_pending_assistant(session_id, user_id)
        with self.assertRaises(Exception):
            self.database.finalize_assistant(message_id, "running", None)


class TestStopRequest(LifecycleTestBase):
    def test_request_stop_marks_unfinished_row(self):
        session_id, user_id = self.start_turn()
        message_id = self.database.create_pending_assistant(session_id, user_id)
        result = self.database.request_stop(session_id)
        self.assertEqual(result["result"], "processing")
        self.assertEqual(result["message_id"], message_id)
        message = self.database.get_unfinished_message(session_id)
        self.assertEqual(message["stop_requested"], 1)

    def test_request_stop_without_unfinished_is_noop(self):
        session_id, _ = self.start_turn()
        result = self.database.request_stop(session_id)
        self.assertEqual(result["result"], "no_active")

    def test_stop_idempotent(self):
        session_id, user_id = self.start_turn()
        message_id = self.database.create_pending_assistant(session_id, user_id)
        self.database.request_stop(session_id)
        result = self.database.request_stop(session_id)
        self.assertEqual(result["message_id"], message_id)


class TestFragments(LifecycleTestBase):
    def fragment_parent(self):
        session_id, user_id = self.start_turn()
        message_id = self.database.create_pending_assistant(session_id, user_id)
        return message_id

    def test_seq_monotonic_from_subquery(self):
        message_id = self.fragment_parent()
        first = self.database.append_fragment(message_id, "text", {"text": "第一段"})
        second = self.database.append_fragment(message_id, "tool_call", {"name": "web_search"})
        third = self.database.append_fragment(message_id, "tool_result", {"content": "结果"})
        self.assertEqual((first, second, third), (1, 2, 3))

    def test_status_column_for_tool_rows(self):
        message_id = self.fragment_parent()
        self.database.append_fragment(message_id, "tool_call", {"name": "x"}, status="running")
        self.database.append_fragment(message_id, "tool_result", {"content": "y"}, status="finished")
        fragments = self.database.list_fragments(message_id)
        self.assertEqual(fragments[0]["status"], "running")
        self.assertEqual(fragments[1]["status"], "finished")

    def test_payload_roundtrip_chinese(self):
        message_id = self.fragment_parent()
        self.database.append_fragment(message_id, "text", {"text": "你好，世界🌍"})
        fragments = self.database.list_fragments(message_id)
        self.assertEqual(fragments[0]["payload"]["text"], "你好，世界🌍")

    def test_unique_index_rejects_duplicate_seq(self):
        message_id = self.fragment_parent()
        self.database.append_fragment(message_id, "text", {"text": "a"})
        with self.assertRaises(Exception):
            with self.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO message_fragments (message_id, seq, type, status, payload, created_at)"
                    " VALUES (?, 1, 'text', NULL, '{}', 0)",
                    (message_id,),
                )

    def test_assemble_text_in_seq_order(self):
        message_id = self.fragment_parent()
        self.database.append_fragment(message_id, "text", {"text": "先查一下"})
        self.database.append_fragment(message_id, "tool_call", {"name": "web_search"})
        self.database.append_fragment(message_id, "tool_result", {"content": "…"})
        self.database.append_fragment(message_id, "text", {"text": "，再总结。"})
        self.assertEqual(self.database.assemble_text(message_id), "先查一下，再总结。")

    def test_has_terminal_fragment(self):
        message_id = self.fragment_parent()
        self.assertFalse(self.database.has_terminal_fragment(message_id))
        self.database.append_fragment(message_id, "terminal", {"kind": "done"})
        self.assertTrue(self.database.has_terminal_fragment(message_id))

    def test_invalid_type_rejected(self):
        message_id = self.fragment_parent()
        with self.assertRaises(Exception):
            self.database.append_fragment(message_id, "think", {})

    def test_fragments_cascade_on_message_delete(self):
        message_id = self.fragment_parent()
        self.database.append_fragment(message_id, "text", {"text": "x"})
        with self.database.transaction() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DELETE FROM chat_messages WHERE id = ?", (message_id,))
        self.assertEqual(self.database.list_fragments(message_id), [])


class TestOldDatabaseMigration(unittest.TestCase):
    def test_old_columns_absent_then_migrated(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        tmp.close()
        path = Path(tmp.name)
        self.addCleanup(path.unlink)
        database = Database(path)
        database.initialize()
        # 第二次 initialize（模拟旧库启动）必须幂等
        database.initialize()
        with database.connection() as connection:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(chat_messages)")
            }
        self.assertTrue(
            {"status", "finish_kind", "deadline_at", "stop_requested"} <= columns
        )


if __name__ == "__main__":
    unittest.main()
