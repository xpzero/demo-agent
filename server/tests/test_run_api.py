"""Session-level run ownership and SSE-independent execution checks."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("API_KEY", "test-key")

import api  # noqa: E402
from api import chat_stream  # noqa: E402
from api.routes import chat, ChatRequest  # noqa: E402
from database import Database, StoreError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class RunApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "runs.sqlite3"
        self.patched = patch.object(api.deps, "DATABASE_PATH", self.path)
        self.patched.start()
        api._running_sessions.clear()
        self.database = Database(self.path)
        self.database.initialize()
        self.client = TestClient(api.app)
        self.session_id = str(uuid4())

    def tearDown(self):
        self.patched.stop()
        self.directory.cleanup()

    def payload(self):
        return {"session_id": self.session_id, "parent_message_id": None,
                "message": "你好", "ref_file_ids": []}

    def test_busy_request_is_not_persisted_and_stop_is_session_scoped(self):
        entered = threading.Event()
        proceed = threading.Event()
        result = []

        def slow_stream(items, context=None, recorder=None):
            entered.set()
            proceed.wait(8)
            yield {"type": "done", "content": "完成"}

        def request():
            result.append(self.client.post("/api/chat", json=self.payload()))

        with patch.object(chat_stream, "stream_events", side_effect=slow_stream):
            worker = threading.Thread(target=request)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                with patch.object(Database, "get_session_history", side_effect=AssertionError("status loaded history")):
                    status = self.client.get(f"/api/sessions/{self.session_id}/status").json()
                self.assertEqual(status, {"processing": True, "can_send_message": False})
                busy = self.client.post("/api/chat", json=self.payload())
                self.assertEqual(busy.status_code, 409)
                self.assertEqual(busy.json()["detail"]["code"], "session_busy")
                history = self.client.get(f"/api/sessions/{self.session_id}").json()
                self.assertEqual(len(history["messages"]), 1)
                self.assertFalse(history["can_send_message"])
                stopped = self.client.post(f"/api/sessions/{self.session_id}/stop").json()
                self.assertEqual(stopped["result"], "processing")
                self.assertTrue(self.database.get_active_run(self.session_id)["stop_requested"])
            finally:
                proceed.set()
                worker.join(8)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0].status_code, 200)
        self.assertEqual(self.client.get(f"/api/sessions/{self.session_id}/status").json(),
                         {"processing": False, "can_send_message": True})

    def test_worker_completes_without_consuming_sse(self):
        import time

        def stream(items, context=None, recorder=None):
            yield {"type": "text_delta", "text": "回答"}
            yield {"type": "done", "content": "回答"}

        with patch.object(chat_stream, "stream_events", side_effect=stream):
            response = chat(ChatRequest(**self.payload()))
            # A disconnected client never reads the response iterator.
            self.assertIsNotNone(response.body_iterator)
            deadline = time.monotonic() + 3
            while self.database.get_active_run(self.session_id) and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertIsNone(self.database.get_active_run(self.session_id))
        history = self.database.get_session_history(self.session_id)
        self.assertEqual(history["messages"][-1]["content"], "回答")
        self.assertEqual(history["messages"][-1]["status"], "finished")

    def test_failed_stream_keeps_partial_message_and_releases_slot(self):
        def broken(items, context=None, recorder=None):
            yield {"type": "text_delta", "text": "已经开始"}
            yield {"type": "error", "message": "gateway unavailable"}

        with patch.object(chat_stream, "stream_events", side_effect=broken):
            response = self.client.post("/api/chat", json=self.payload())
        self.assertEqual(response.status_code, 200)
        history = self.client.get(f"/api/sessions/{self.session_id}").json()
        self.assertTrue(history["can_send_message"])
        self.assertEqual([m["role"] for m in history["messages"]], ["user", "assistant"])
        self.assertEqual(history["messages"][1]["content"], "已经开始")
        self.assertEqual(history["messages"][1]["status"], "incomplete")
        self.assertIsNone(self.database.get_active_run(self.session_id))

    def test_startup_reconciles_orphan_at_original_deadline(self):
        _, run = self.database.create_run_turn(
            self.session_id, None, "故障前已接收", [], deadline_at=time.time() + 0.15,
        )
        task = self.database.create_task(run, "提交", "running")
        self.database.create_operation(task, "提交", "running", run_id=run)
        with TestClient(api.app) as client:
            self.assertEqual(client.get(f"/api/sessions/{self.session_id}/status").json()["processing"], True)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and self.database.get_active_run(self.session_id):
                time.sleep(0.01)
            self.assertFalse(client.get(f"/api/sessions/{self.session_id}/status").json()["processing"])
        state = self.database.get_run(run)
        assert state is not None
        self.assertEqual(state["status"], "timed_out")
        self.assertEqual(self.database.list_operations(task)[0]["status"], "unknown")

    def test_context_setup_failure_releases_session_slot(self):
        with patch.object(Database, "list_session_files", side_effect=RuntimeError("storage unavailable")):
            with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
                chat(ChatRequest(**self.payload()))
        self.assertIsNone(self.database.get_active_run(self.session_id))
        status = self.database.get_session_status(self.session_id)
        self.assertIsNotNone(status)
        self.assertTrue(status["can_send_message"] if status else False)

    def test_stop_atomically_enters_finishing_and_blocks_new_tool(self):
        _, run = self.database.create_run_turn(self.session_id, None, "执行", [],
                                               deadline_at=time.time() + 300)
        self.assertEqual(self.database.request_stop(self.session_id)["result"], "processing")
        state = self.database.get_run(run)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["status"], "finishing")
        self.assertTrue(state["stop_requested"])
        self.assertEqual(state["reason"], "user_stop")
        self.assertLessEqual(state["deadline_at"], time.time() + 120)
        with self.assertRaises(StoreError):
            self.database.begin_tool_operation(run, "later", "write_file", {})
        self.assertEqual(self.database.list_tasks(run), [])
        self.assertTrue(self.database.finish_run(run, "stopped"))
        self.assertIsNone(self.database.get_active_run(self.session_id))

    def test_session_history_includes_run_conclusion_and_task_facts(self):
        _, first_run = self.database.create_run_turn(
            self.session_id, None, "提交订单", [], deadline_at=time.time() + 300,
        )
        task = self.database.create_task(first_run, "提交订单", "running")
        self.database.begin_tool_operation(
            first_run, "call-1", "business_send", {"value": 1},
            task_id=task, goal_version=1,
        )
        self.database.finish_tool_operation(
            self.database.list_operations(task)[0]["id"], "transport ok", True, business=True,
        )
        # 超时结论只能在原截止时间耗尽后落定
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET deadline_at = ? WHERE id = ?",
                (time.time() - 1, first_run),
            )
        self.database.finish_run(first_run, "timed_out", reason="business_unconfirmed")
        history = self.client.get(f"/api/sessions/{self.session_id}").json()
        self.assertIn("progress", history)
        progress = history["progress"]
        self.assertEqual(len(progress["runs"]), 1)
        conclusion = progress["runs"][0]
        self.assertEqual(conclusion["status"], "timed_out")
        self.assertEqual(conclusion["reason"], "business_unconfirmed")
        self.assertIn("超时结束", conclusion["conclusion"])
        self.assertEqual(len(progress["tasks"]), 1)
        task_progress = progress["tasks"][0]
        self.assertEqual(task_progress["goal"], "提交订单")
        # 运输层成功不是业务证据：操作仍待核实，不宣称成功或失败
        self.assertEqual(task_progress["operations"][0]["status"], "unconfirmed")
        self.assertEqual(task_progress["operations"][0]["tool"], "business_send")
        self.assertTrue(history["can_send_message"])

    def test_session_progress_marks_confirmed_result_and_stopped_conclusion(self):
        from unittest.mock import Mock
        from recovery.adapter import Observation
        _, run = self.database.create_run_turn(
            self.session_id, None, "下单", [], deadline_at=time.time() + 300,
        )
        task = self.database.create_task(run, "下单", "active")
        self.database.approve_step(
            run_id=run, task_id=task, step_id="order-1", tool="business_send",
            args={"value": 1}, goal_version=1,
            adapter=Mock(approve=Mock(return_value=True)),
        )
        op = self.database.begin_approved_step(
            run, task, "order-1", "call-1", "business_send", {"value": 1}, goal_version=1,
        )
        self.database.record_confirmed_feedback(
            op, Observation("succeeded", "订单已确认"), source="query", observed_at=time.time(),
        )
        self.database.finish_run(run, "completed")
        history = self.client.get(f"/api/sessions/{self.session_id}").json()
        progress = history["progress"]
        self.assertIn("处理完毕", progress["runs"][0]["conclusion"])
        operation = progress["tasks"][0]["operations"][0]
        self.assertEqual(operation["status"], "succeeded")
        self.assertEqual(operation["confirmed_result"], "订单已确认")
        self.assertEqual(progress["tasks"][0]["steps"][0]["step_id"], "order-1")
        self.assertTrue(progress["tasks"][0]["steps"][0]["approved"])
        # 历史与进度呈现不改写持久化事实
        self.assertEqual(self.database.list_operations(task)[0]["status"], "succeeded")
