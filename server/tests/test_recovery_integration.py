"""Recovery suggestions are advisory and cannot dispatch unapproved Task steps."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("API_KEY", "test-key")

from agent import loop
from api.chat_stream import _task_snapshot, chat_sse_stream
from database import Database, StoreError
from recovery import validate_suggestion
from recovery.adapter import Capabilities, Observation


def chunk(call_id=None, name=None, args=None, content=None):
    calls = None
    if call_id:
        calls = [SimpleNamespace(index=0, id=call_id, function=SimpleNamespace(
            name=name, arguments=json.dumps(args or {}, ensure_ascii=False)))]
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
        content=content, tool_calls=calls))], usage=None)


class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = Database(self.root / "db.sqlite3")
        self.db.initialize()
        self.session = str(uuid4())
        self.user, self.run_id = self.db.create_run_turn(self.session, None, "继续", [])
        self.task = self.db.create_task(self.run_id, "写笔记", "running")

    def stream(self, responses, execute=None, adapters=None):
        requests = iter(responses)
        sent = []
        def create(**kwargs):
            sent.append(kwargs)
            return iter(next(requests))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        items = [{"role": "user", "content": "继续"}]
        with patch.object(loop, "client", client), patch.object(
            loop, "execute_tool", execute or (lambda *_: self.fail("dispatched"))
        ):
            events = [json.loads(frame.removeprefix("data: ")) for frame in chat_sse_stream(
                database=self.db, session_id=self.session, user_message_id=self.user,
                run_id=self.run_id, items=items, file_root=self.root,
                release_session=lambda: None, business_adapters=adapters,
            )]
        return sent, events, items

    def test_structured_reply_and_order_with_no_operation(self):
        suggestion = {"intent": "reply", "action": "reply", "task_id": str(self.task),
                      "task_version": 1, "operation_refs": []}
        sent, events, items = self.stream([
            [chunk(content="先核实", call_id="r1", name="recovery_suggestion", args=suggestion)],
            [chunk(content="已收到")],
        ])
        self.assertIn("recovery_suggestion", [tool["function"]["name"] for tool in sent[0]["tools"]])
        self.assertEqual([e["type"] for e in events],
                         ["user_message", "tool_call", "tool_result", "done"])
        # tool_result 保留机器可读裁决，done.content 是核对后事实的可读呈现
        self.assertEqual(json.loads(events[2]["content"])["outcome"], "allow")
        self.assertEqual(events[3]["content"], "已核对当前任务事实。")
        self.assertEqual(items[-1]["content"], "已核对当前任务事实。")
        self.assertEqual(self.db.list_operations(self.task), [])

    def test_unknown_and_foreign_records_fail_closed(self):
        op = self.db.create_operation(self.task, "write_file", "unknown", run_id=self.run_id)
        snapshot = _task_snapshot(self.db, self.session, self.run_id, "继续")
        self.assertEqual(snapshot.candidates[0].operations[0].status, "unconfirmed")
        suggestion = {"intent": "resume", "action": "execute", "task_id": str(self.task),
                      "task_version": 1, "operation_refs": [{"id": str(op), "status": "unconfirmed",
                      "result": None, "conflict": False}], "step_id": "x", "tool": "write_file", "args": {}}
        self.assertNotEqual(validate_suggestion(suggestion, snapshot, snapshot).outcome, "allow")
        _, events, _ = self.stream([[chunk(call_id="new", name="write_file", args={})]])
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(len(self.db.list_operations(self.task)), 1)
        with self.assertRaises(StoreError):
            _task_snapshot(self.db, str(uuid4()), self.run_id, "继续")

    def test_changed_goal_before_dispatch(self):
        suggestion = {"intent": "reply", "action": "reply", "task_id": str(self.task),
                      "task_version": 1, "operation_refs": []}
        def create(**kwargs):
            self.db.revise_task_goal(self.task, "改成天气", 1)
            return iter([chunk(call_id="call", name="recovery_suggestion", args=suggestion)])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(loop, "client", client), patch.object(loop, "execute_tool", side_effect=AssertionError("dispatched")):
            events = [json.loads(frame.removeprefix("data: ")) for frame in chat_sse_stream(
                database=self.db, session_id=self.session, user_message_id=self.user,
                run_id=self.run_id, items=[{"role": "user", "content": "继续"}],
                file_root=self.root, release_session=lambda: None)]
        # done.content 现在是可读文本；rejudge 从 tool_result 的机器可读裁决判断
        verdict_event = next(e for e in events if e["type"] == "tool_result")
        self.assertEqual(json.loads(verdict_event["content"])["outcome"], "rejudge")
        self.assertIn("任务事实已变化", events[-1]["content"])
        self.assertEqual(self.db.list_operations(self.task), [])

    def test_snapshot_includes_other_current_session_task_and_rejects_stale_facts(self):
        other = self.db.create_task(self.run_id, "另一个任务", "pending")
        snapshot = _task_snapshot(self.db, self.session, self.run_id, "继续")
        self.assertEqual({task.id for task in snapshot.candidates}, {str(self.task), str(other)})
        suggestion = {"intent": "reply", "action": "reply", "task_id": str(self.task),
                      "task_version": 1, "operation_refs": []}
        self.assertEqual(validate_suggestion(suggestion, snapshot, snapshot).outcome, "clarify")
        self.db.revise_task_goal(self.task, "新目标", 1)
        fresh = _task_snapshot(self.db, self.session, self.run_id, "继续")
        self.assertEqual(validate_suggestion(suggestion, snapshot, fresh).outcome, "clarify")

    def test_unlinked_run_with_existing_session_task_blocks_tool(self):
        self.db.finish_run(self.run_id, "failed")
        self.user, self.run_id = self.db.create_run_turn(self.session, self.user, "继续", [])
        self.assertEqual(self.db.list_tasks(self.run_id), [])
        sent, events, _ = self.stream([[chunk(call_id="new", name="write_file", args={})]])
        self.assertIn(str(self.task), sent[0]["messages"][0]["content"])
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(self.db.list_operations(self.task), [])

    def test_approved_step_submits_once_and_saves_business_evidence(self):
        self.db.update_task(self.task, "active")
        calls = []
        class Adapter:
            capabilities = Capabilities(queryable=True, cancellable=False, idempotent_submission=False)
            def approve(self, task_id, goal_version, step_id, tool, args, depends_on):
                return True
            def cancel(self, operation_id, business_operation_id):
                raise AssertionError("unexpected cancel")
            def submit(self, operation_id, tool, args):
                calls.append((operation_id, tool, args))
                operation = self_db.list_operations(task_id)[0]
                self_test.assertEqual(operation["status"], "maybe_submitted")
                return "receipt-1", Observation("succeeded", "已确认")
            def query(self, operation_id, business_operation_id):
                raise AssertionError("unexpected query")
        self_db, task_id, self_test = self.db, self.task, self
        adapter = Adapter()
        self.db.approve_step(self.task, "step-a", "business_send", {"value": 1},
                             run_id=self.run_id, goal_version=1, adapter=adapter)
        suggestion = {"intent": "resume", "action": "execute", "task_id": str(self.task),
                      "task_version": 1, "operation_refs": [], "step_id": "step-a",
                      "tool": "business_send", "args": {"value": 1}}
        _, events, _ = self.stream([[chunk(call_id="r", name="recovery_suggestion",
                                             args=suggestion)]], adapters={"business_send": Adapter()})
        self.assertEqual(events[-1]["type"], "done")
        # done.content 为可读文本：业务结果来自已确认事实并标注核对时间
        self.assertIn("业务操作", events[-1]["content"])
        self.assertIn("已成功：已确认", events[-1]["content"])
        self.assertIn("核对时间", events[-1]["content"])
        self.assertEqual(len(calls), 1)
        operation = self.db.list_operations(self.task)[0]
        self.assertEqual(operation["confirmed_result"], "已确认")
        self.assertEqual(self.db.list_operation_observations(operation["id"])[0]["source"], "Adapter")
        self.assertIsNone(self.db.get_active_run(self.session))

    def test_unapproved_suggestion_cannot_dispatch(self):
        suggestion = {"intent": "resume", "action": "execute", "task_id": str(self.task),
                      "task_version": 1, "operation_refs": [], "step_id": "invented",
                      "tool": "write_file", "args": {}}
        _, events, _ = self.stream([
            [chunk(call_id="r", name="recovery_suggestion", args=suggestion)],
            [chunk(call_id="r2", name="recovery_suggestion", args=suggestion)],
        ])
        self.assertEqual(json.loads(next(e for e in events if e["type"] == "tool_result")["content"])["outcome"], "invalid")
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("未通过校验", events[-1]["message"])
        self.assertEqual(self.db.list_operations(self.task), [])


if __name__ == "__main__":
    unittest.main()
