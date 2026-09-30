"""End-to-end demo: HTTP -> background Run -> approved order -> business evidence -> Session ready.

Run: uv run python -m unittest tests.test_demo_business_adapter -v
No external API, credentials, or network service are needed.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("API_KEY", "test-key")

from fastapi.testclient import TestClient
from agent import loop
from api import routes
from database import Database
from recovery.adapter import Observation
from recovery.demo_adapter import DemoBusinessAdapter


def chunk(call_id=None, name=None, args=None):
    calls = None
    if call_id:
        calls = [SimpleNamespace(index=0, id=call_id, function=SimpleNamespace(
            name=name, arguments=json.dumps(args, ensure_ascii=False)))]
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
        content=None, tool_calls=calls))], usage=None)


class DemoBusinessFlow(unittest.TestCase):
    def test_approved_order_completes_http_run_and_unblocks_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "agent.sqlite3")
            db.initialize()
            adapter = DemoBusinessAdapter(root / "business.sqlite3")
            session = str(uuid4())
            first_user, first_run = db.create_run_turn(session, None, "准备演示订单", [])
            task = db.create_task(first_run, "演示订单", "active")
            adapter_tool = adapter.TOOL
            db.approve_step(task, "order-1", adapter_tool, {"value": "样品 A"},
                            run_id=first_run, goal_version=1, adapter=adapter)
            db.finish_run(first_run, "completed")
            suggestion = {"intent": "resume", "action": "execute", "task_id": str(task),
                          "task_version": 1, "operation_refs": [], "step_id": "order-1",
                          "tool": adapter_tool, "args": {"value": "样品 A"}}
            calls = []
            def create(**kwargs):
                calls.append(kwargs)
                return iter([chunk("suggestion-1", "recovery_suggestion", suggestion)])
            model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            previous = getattr(routes.app.state, "business_adapters", None)
            routes.app.state.business_adapters = {adapter_tool: adapter}
            try:
                with patch.object(routes, "get_database", return_value=db), patch.object(loop, "client", model):
                    with TestClient(routes.app) as client:
                        before = client.get(f"/api/sessions/{session}").json()
                        self.assertTrue(before["can_send_message"])
                        with client.stream("POST", "/api/chat", json={
                            "session_id": session, "parent_message_id": first_user,
                            "message": "继续处理已批准的演示订单",
                        }) as response:
                            self.assertEqual(response.status_code, 200)
                            events = [json.loads(line[6:]) for line in response.iter_lines()
                                      if line.startswith("data: ")]
                        status = client.get(f"/api/sessions/{session}").json()
                        history = client.get(f"/api/sessions/{session}").json()
                self.assertEqual([e["type"] for e in events],
                                 ["user_message", "tool_call", "tool_result", "done"])
                verdict = json.loads(events[2]["content"])
                self.assertEqual(verdict["status"], "succeeded")
                self.assertEqual(len(calls), 1)
                self.assertEqual(db.get_run(db.list_operations(task)[0]["run_id"])["status"], "completed")
                self.assertTrue(status["can_send_message"])
                self.assertTrue(history["can_send_message"])
                self.assertEqual(history["messages"][-1]["role"], "assistant")
                op = db.list_operations(task)[0]
                self.assertEqual(op["confirmed_result"], "订单已确认：样品 A")
                self.assertEqual(len(db.list_operation_observations(op["id"])), 2)
                self.assertEqual([item["status"] for item in db.list_operation_observations(op["id"])],
                                 ["processing", "succeeded"])
                reopened = DemoBusinessAdapter(root / "business.sqlite3")
                self.assertEqual(reopened.query(op["id"], op["business_operation_id"]).status, "succeeded")
                with sqlite3.connect(root / "business.sqlite3") as business:
                    self.assertEqual(business.execute("SELECT count(*) FROM demo_orders").fetchone()[0], 1)
            finally:
                routes.app.state.business_adapters = previous

    def test_unconfirmed_business_keeps_session_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "agent.sqlite3")
            db.initialize()
            class UnconfirmedAdapter(DemoBusinessAdapter):
                def query(self, operation_id, business_operation_id):
                    return Observation("unconfirmed", detail="业务系统暂不可查")
            adapter = UnconfirmedAdapter(root / "business.sqlite3")
            session = str(uuid4())
            first_user, first_run = db.create_run_turn(session, None, "准备订单", [])
            task = db.create_task(first_run, "订单", "active")
            db.approve_step(task, "order-1", adapter.TOOL, {"value": "样品 B"},
                            run_id=first_run, goal_version=1, adapter=adapter)
            db.finish_run(first_run, "completed")
            suggestion = {"intent": "resume", "action": "execute", "task_id": str(task),
                          "task_version": 1, "operation_refs": [], "step_id": "order-1",
                          "tool": adapter.TOOL, "args": {"value": "样品 B"}}
            model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=lambda **kwargs: iter([chunk("suggestion-2", "recovery_suggestion", suggestion)]))))
            previous = getattr(routes.app.state, "business_adapters", None)
            routes.app.state.business_adapters = {adapter.TOOL: adapter}
            try:
                with patch.object(routes, "get_database", return_value=db), patch.object(loop, "client", model):
                    with TestClient(routes.app) as client:
                        response = client.post("/api/chat", json={
                            "session_id": session, "parent_message_id": first_user, "message": "继续订单"})
                        events = [json.loads(line[6:]) for line in response.text.splitlines()
                                  if line.startswith("data: ")]
                        self.assertEqual(events[-1]["type"], "error")
                        self.assertFalse(client.get(f"/api/sessions/{session}").json()["can_send_message"])
                        rejected = client.post("/api/chat", json={
                            "session_id": session, "parent_message_id": first_user, "message": "再来一次"})
                        self.assertEqual(rejected.status_code, 409)
                        self.assertEqual(rejected.json()["detail"]["code"], "session_busy")
                self.assertEqual(len(db.list_operations(task)), 1)
                active = db.get_active_run(session)
                if active is None:
                    self.fail("unconfirmed business released the Session")
                self.assertEqual(active["status"], "finishing")
            finally:
                routes.app.state.business_adapters = previous


if __name__ == "__main__":
    unittest.main()
