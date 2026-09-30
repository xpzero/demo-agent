"""Storage contracts and upgrade of a populated pre-run database."""

import sqlite3
import json
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from database import Database, StoreError
from database.connection import SCHEMA
from database.metrics import METRICS_SCHEMA


class RunStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "store.sqlite3"
        self.db = Database(self.path)
        self.db.initialize()
        self.session = str(uuid4())

    def test_lifecycle_and_partial_message(self):
        user_id, run_id = self.db.create_run_turn(self.session, None, "hello", [])
        self.assertEqual(self.db.get_active_run(self.session)["id"], run_id)
        with self.assertRaises(StoreError) as busy:
            self.db.create_run_turn(self.session, user_id, "again", [])
        self.assertEqual(busy.exception.code, "active_run_exists")
        first = self.db.upsert_partial_assistant(run_id, self.session, user_id, "hel")
        self.assertEqual(first, self.db.upsert_partial_assistant(run_id, self.session, user_id, "hello"))
        self.assertEqual(self.db.get_session_history(self.session)["messages"][-1]["content"], "hello")
        self.assertEqual(self.db.finalize_assistant_message(run_id), first)
        self.assertEqual(self.db.request_stop(self.session)["result"], "processing")
        self.assertTrue(self.db.get_run(run_id)["stop_requested"])
        self.assertFalse(self.db.enter_finishing(9999))
        state = self.db.get_run(run_id)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["status"], "finishing")
        self.assertFalse(self.db.enter_finishing(run_id))
        self.assertTrue(self.db.finish_run(run_id, "completed"))
        self.assertEqual(self.db.get_run(run_id)["status"], "stopped")
        self.assertFalse(self.db.finish_run(run_id, "failed"))
        self.assertIsNone(self.db.get_active_run(self.session))
        self.assertEqual(self.db.request_stop(self.session)["result"], "no_active_request")
        with self.assertRaises(StoreError):
            self.db.upsert_partial_assistant(run_id, self.session, user_id, "late")
        next_id, _ = self.db.create_run_turn(self.session, first, "next", [])
        self.assertEqual(self.db.get_message_chain(self.session, next_id)[-2]["id"], first)

    def test_tasks_operations_counters_and_recovery(self):
        user_id, run_id = self.db.create_run_turn(self.session, None, "first", [], deadline_at=time.time() + 30)
        self.assertEqual(self.db.record_model_attempt(run_id, True), {"logical_count": 1, "attempt_count": 1})
        self.assertEqual(self.db.record_model_attempt(run_id, False), {"logical_count": 1, "attempt_count": 2})
        task = self.db.create_task(run_id, "work", "running")
        self.assertTrue(self.db.revise_task_goal(task, "better work", 1))
        self.assertFalse(self.db.revise_task_goal(task, "stale", 1))
        self.assertEqual(json.loads(self.db.list_tasks(run_id)[0]["goal_history"]), [{"version": 1, "name": "work"}])
        operation = self.db.create_operation(task, "send", "running", run_id=run_id)
        self.assertTrue(self.db.record_operation_feedback(operation, 1, "accepted", "receipt", True))
        self.assertFalse(self.db.record_operation_feedback(operation, 1, "stale", "", False))
        self.assertEqual(self.db.list_operations(task)[0]["reliable"], 1)
        self.assertEqual(self.db.get_run(run_id)["operation_count"], 1)
        self.db.upsert_partial_assistant(run_id, self.session, user_id, "draft")
        with self.db.transaction() as connection:
            connection.execute("UPDATE runs SET deadline_at = ? WHERE id = ?", (time.time() - 1, run_id))
        with self.assertRaises(StoreError):
            self.db.record_model_attempt(run_id, True)
        reopened = Database(self.path)
        reopened.initialize()
        self.assertEqual(reopened.recover_orphan_runs(), 1)
        self.assertEqual(reopened.recover_orphan_runs(), 0)
        self.assertEqual(reopened.get_run(run_id)["status"], "timed_out")
        self.assertIsNone(reopened.get_active_run(self.session))
        self.assertEqual(reopened.list_operations(task)[0]["status"], "unknown")
        self.assertEqual(reopened.get_session_history(self.session)["messages"][-1]["status"], "incomplete")
        next_user, next_run = reopened.create_run_turn(self.session, reopened.get_session_history(self.session)["session"]["current_message_id"], "retry", [])
        reopened.attach_task(next_run, task)
        self.assertEqual(reopened.list_tasks(next_run)[0]["id"], task)
        with self.assertRaises(StoreError):
            reopened.create_operation(task, "ambiguous")
        self.assertTrue(reopened.finish_run(next_run, "limit_reached"))

    def test_restart_keeps_unknown_operation_and_original_budget(self):
        user, run = self.db.create_run_turn(
            self.session, None, "send once", [], deadline_at=time.time() + 60
        )
        task = self.db.create_task(run, "send once", "running")
        operation = self.db.create_operation(task, "send", "running", run_id=run, evidence="receipt")
        draft = self.db.upsert_partial_assistant(run, self.session, user, "sending")
        before = self.db.get_run(run)
        reopened = Database(self.path)
        reopened.initialize()
        self.assertEqual(reopened.recover_orphan_runs(), 1)
        self.assertEqual(reopened.recover_orphan_runs(), 0)
        after = reopened.get_run(run)
        self.assertEqual(after["status"], "finishing")
        self.assertEqual(after["reason"], "worker_lost_on_restart")
        self.assertEqual(after["deadline_at"], before["deadline_at"])
        self.assertEqual(after["id"], before["id"])
        self.assertIsNone(after["finished_at"])
        self.assertEqual(reopened.get_active_run(self.session)["id"], run)
        self.assertEqual(reopened.get_session_history(self.session)["messages"][-1]["id"], draft)
        self.assertEqual(reopened.get_session_history(self.session)["messages"][-1]["status"], "incomplete")
        fact = reopened.list_operations(task)[0]
        self.assertEqual(fact["id"], operation)
        self.assertEqual(fact["status"], "unknown")
        self.assertEqual(fact["evidence"], "receipt")
        self.assertEqual(fact["reliable"], 0)
        with self.assertRaises(StoreError) as busy:
            reopened.create_run_turn(self.session, draft, "send again", [])
        self.assertEqual(busy.exception.code, "active_run_exists")
        with self.assertRaises(StoreError) as blocked:
            reopened.begin_tool_operation(run, "call-again", "send", {})
        self.assertEqual(blocked.exception.code, "run_not_active")
        with self.assertRaises(StoreError):
            reopened.record_model_attempt(run, True)
        self.assertEqual(reopened.list_operations(task), [fact])
        pending_runs = reopened.list_orphan_runs()
        self.assertEqual([item["id"] for item in pending_runs], [run])
        self.assertEqual(pending_runs[0]["operations"][0]["id"], operation)
        self.assertEqual(pending_runs[0]["operations"][0]["status"], "unknown")
        self.assertEqual(reopened.reconcile_orphan_run(run), {
            "status": "pending", "next_check_at": before["deadline_at"]
        })
        with reopened.transaction() as connection:
            connection.execute("UPDATE runs SET deadline_at = ? WHERE id = ?", (time.time() - 1, run))
        self.assertEqual(reopened.reconcile_orphan_run(run), {
            "status": "timed_out", "next_check_at": None
        })
        self.assertEqual(reopened.list_orphan_runs(), [])
        self.assertEqual(reopened.reconcile_orphan_run(run)["status"], "timed_out")
        self.assertEqual(reopened.get_run(run)["status"], "timed_out")
        self.assertIsNone(reopened.get_active_run(self.session))
        self.assertEqual(reopened.list_operations(task)[0]["status"], "unknown")
        _, later = reopened.create_run_turn(self.session, draft, "continue", [])
        reopened.attach_task(later, task)
        with self.assertRaises(StoreError) as blocked:
            reopened.begin_tool_operation(later, "new-call", "send", {}, task_id=task, goal_version=1)
        self.assertEqual(blocked.exception.code, "operation_needs_reconciliation")

    def test_recovery_deadline_finishing_and_finished_run(self):
        _, run = self.db.create_run_turn(self.session, None, "first", [], deadline_at=time.time() + 60)
        task = self.db.create_task(run, "first", "running")
        pending = self.db.create_operation(task, "not sent", "pending", run_id=run)
        self.assertTrue(self.db.enter_finishing(run, reason="user_stop"))
        with self.db.transaction() as connection:
            connection.execute("UPDATE runs SET deadline_at = ? WHERE id = ?", (time.time() - 1, run))
        reopened = Database(self.path)
        reopened.initialize()
        self.assertEqual(reopened.recover_orphan_runs(), 1)
        self.assertEqual(reopened.get_run(run)["status"], "timed_out")
        self.assertEqual(reopened.get_run(run)["reason"], "user_stop")
        self.assertEqual(reopened.list_operations(task)[0]["id"], pending)
        self.assertEqual(reopened.list_operations(task)[0]["status"], "pending")
        self.assertIsNone(reopened.get_active_run(self.session))
        snapshot = reopened.get_run(run)
        self.assertEqual(reopened.recover_orphan_runs(), 0)
        self.assertEqual(reopened.get_run(run), snapshot)
        parent = snapshot["user_message_id"]
        user2, run2 = reopened.create_run_turn(self.session, parent, "done", [])
        self.assertGreater(user2, parent)
        self.assertTrue(reopened.finish_run(run2, "completed"))
        finished = reopened.get_run(run2)
        self.assertEqual(reopened.recover_orphan_runs(), 0)
        self.assertEqual(reopened.get_run(run2), finished)

    def test_restart_without_deadline_uses_creation_time(self):
        _, run = self.db.create_run_turn(self.session, None, "legacy", [])
        with self.db.transaction() as connection:
            connection.execute("UPDATE runs SET deadline_at = NULL WHERE id = ?", (run,))
        self.assertEqual(self.db.recover_orphan_runs(), 1)
        state = self.db.get_run(run)
        self.assertAlmostEqual(state["deadline_at"], state["created_at"] + 300)
        self.assertEqual(self.db.recover_orphan_runs(), 0)

    def test_list_session_tasks_is_scoped_and_does_not_attach(self):
        _, run = self.db.create_run_turn(self.session, None, "first", [])
        first = self.db.create_task(run, "first")
        other_session = str(uuid4())
        _, other_run = self.db.create_run_turn(other_session, None, "other", [])
        self.db.create_task(other_run, "other")
        self.assertEqual([task["id"] for task in self.db.list_session_tasks(self.session)], [first])
        self.assertEqual([task["id"] for task in self.db.list_tasks(run)], [first])
        self.assertEqual(self.db.list_session_tasks(str(uuid4())), [])
        with self.assertRaises(StoreError):
            self.db.list_session_tasks("not-a-uuid")

    def test_atomic_finish_and_stop_race(self):
        user, run = self.db.create_run_turn(self.session, None, "hello", [])
        message = self.db.upsert_partial_assistant(run, self.session, user, "partial")
        self.assertEqual(self.db.request_stop(self.session)["result"], "processing")
        self.assertTrue(self.db.finish_run(run, "completed", assistant_status="finished", assistant_content="full"))
        self.assertEqual(self.db.get_run(run)["status"], "stopped")
        history = self.db.get_session_history(self.session)
        self.assertEqual(history["messages"][-1]["id"], message)
        self.assertEqual(history["messages"][-1]["status"], "incomplete")
        self.assertIsNone(self.db.get_active_run(self.session))
        with self.assertRaises(StoreError):
            self.db.finish_run(run, "failed", assistant_status="finished")

    def test_legacy_upgrade_preserves_ids_links_and_history(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        session = str(uuid4())
        connection = sqlite3.connect(legacy)
        connection.executescript(SCHEMA + METRICS_SCHEMA)
        connection.execute("INSERT INTO chat_sessions (id,title,created_at,updated_at) VALUES (?,?,?,?)", (session, "old", 1, 1))
        connection.execute("INSERT INTO chat_messages (id,session_id,role,status,content,created_at) VALUES (?,?,?,?,?,?)", (41, session, "user", "finished", "old text", 1))
        connection.execute("INSERT INTO chat_messages (id,session_id,parent_id,role,status,content,created_at) VALUES (?,?,?,?,?,?,?)", (42, session, 41, "assistant", "finished", "reply", 2))
        connection.execute("UPDATE chat_sessions SET current_message_id = 42 WHERE id = ?", (session,))
        connection.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?)", ("file1", "a.txt", "text/plain", 1, "a", "uploaded", 1))
        connection.execute("INSERT INTO message_files VALUES (?,?,?)", (41, "file1", 0))
        connection.execute("INSERT INTO agent_turns (id,message_id,created_at) VALUES (?,?,?)", (8, 41, 1))
        connection.execute("INSERT INTO tool_runs (id,agent_turn_id,name,args_excerpt,result_excerpt,ok,created_at) VALUES (?,?,?,?,?,?,?)", (9, 8, "tool", "{}", "ok", 1, 1))
        connection.commit()
        connection.close()
        db = Database(legacy)
        db.initialize()
        db.initialize()
        with db.connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("SELECT message_id FROM message_files").fetchone()[0], 41)
            self.assertEqual(connection.execute("SELECT message_id FROM agent_turns").fetchone()[0], 41)
            self.assertEqual(connection.execute("SELECT agent_turn_id FROM tool_runs").fetchone()[0], 8)
        history = db.get_session_history(session)
        self.assertEqual([m["id"] for m in history["messages"]], [41, 42])
        self.assertEqual(history["messages"][1]["tool_runs"][0]["id"], 9)
        self.assertEqual(db.create_user_turn(session_id=session, parent_message_id=42, content="new", file_ids=[]), 43)


if __name__ == "__main__":
    unittest.main()
