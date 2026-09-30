"""Tool dispatch must have a durable attempt before any external effect."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("API_KEY", "test-key")

from agent import loop
from api.chat_stream import chat_sse_stream
from database import Database, StoreError


def chunk(*, content=None, call_id=None, name="write_file",
          arguments='{"path":"note.txt","content":"hello"}'):
    delta = SimpleNamespace(content=content, tool_calls=None)
    if call_id is not None:
        delta.tool_calls = [SimpleNamespace(index=0, id=call_id, function=SimpleNamespace(
            name=name, arguments=arguments
        ))]
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = Database(self.root / "state.sqlite3")
        self.db.initialize()
        self.session = str(uuid4())
        self.user, self.run_id = self.db.create_run_turn(self.session, None, "Write a note", [])

    def stream(self, streams, execute):
        requests = iter(streams)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: next(requests)
        )))
        self.items = [{"role": "system", "content": "test"},
                      {"role": "user", "content": "Write a note"}]
        with patch.object(loop, "client", client), patch.object(loop, "execute_tool", execute):
            return [json.loads(frame.removeprefix("data: ")) for frame in chat_sse_stream(
                database=self.db, session_id=self.session, user_message_id=self.user,
                run_id=self.run_id, items=self.items,
                file_root=self.root, release_session=lambda: None,
            )]

    def test_task_is_lazy_and_attempt_precedes_effect_and_records_result(self):
        self.stream([[chunk(content="hello")]], lambda *_: self.fail("unexpected tool"))
        self.assertEqual(self.db.list_tasks(self.run_id), [])
        user, run = self.db.create_run_turn(self.session, self.db.get_session_history(self.session)["session"]["current_message_id"], "Write a note", [])
        self.user, self.run_id = user, run

        def execute(name, args, context):
            task = self.db.list_tasks(run)[0]
            op = self.db.list_operations(task["id"])[0]
            self.assertEqual(task["name"], "Write a note")
            self.assertEqual((op["status"], op["tool_call_id"], op["name"]), ("running", "call1", name))
            self.assertEqual(json.loads(op["content"]), args)
            return "written", True

        events = self.stream([[chunk(call_id="call1")], [chunk(content="done")]], execute)
        self.assertEqual(events[-1]["type"], "done")
        status = self.db.get_session_status(self.session)
        assert status is not None
        self.assertFalse(status["processing"])
        op = self.db.list_operations(self.db.list_tasks(run)[0]["id"])[0]
        self.assertEqual((op["status"], op["content"], op["reliable"]), ("succeeded", "written", 1))

    def test_stop_and_goal_change_block_dispatch(self):
        for change in ("stop", "goal"):
            with self.subTest(change=change):
                # A linked task captures its version before the model response arrives.
                task = self.db.create_task(self.run_id, "original", "running")
                def model_create(**kwargs):
                    if change == "stop":
                        self.db.request_stop(self.session)
                    else:
                        self.db.revise_task_goal(task, "new goal", 1)
                    return iter([chunk(call_id="call1")])
                client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=model_create)))
                with patch.object(loop, "client", client), patch.object(loop, "execute_tool", side_effect=AssertionError("dispatched")):
                    events = [json.loads(f.removeprefix("data: ")) for f in chat_sse_stream(
                        database=self.db, session_id=self.session, user_message_id=self.user,
                        run_id=self.run_id, items=[{"role": "user", "content": "go"}],
                        file_root=self.root, release_session=lambda: None,
                    )]
                self.assertEqual(self.db.list_operations(task), [])
                self.assertEqual(events[-1]["type"], "error")
                if change == "stop":
                    self.assertEqual(self.db.get_run(self.run_id)["status"], "stopped")
                else:
                    self.assertIn("recovery_suggestion", events[-1]["message"])
                if change == "stop":
                    self.user, self.run_id = self.db.create_run_turn(self.session, self.user, "again", [])

    def test_unknown_is_never_blindly_replayed(self):
        def execute(name, args, context):
            task = self.db.list_tasks(self.run_id)[0]
            with self.assertRaises(StoreError) as duplicate:
                self.db.begin_tool_operation(self.run_id, "call1", name, args,
                                             task_id=task["id"], goal_version=task["goal_version"])
            self.assertEqual(duplicate.exception.code, "operation_already_attempted")
            raise RuntimeError("connection lost after send")
        events = self.stream([[chunk(call_id="call1")]], execute)
        self.assertEqual(events[-1]["type"], "error")
        task = self.db.list_tasks(self.run_id)[0]
        op = self.db.list_operations(task["id"])[0]
        # A local handler crash is a definite failure of this attempt; the
        # operation is never blindly replayed with the same call id.
        self.assertEqual((op["status"], op["reliable"]), ("failed", 1))
        self.assertEqual(op["tool_call_id"], "call1")

    def test_definite_failure_lets_session_and_retry_proceed(self):
        calls = []
        def execute(name, args, context):
            calls.append(name)
            return "outcome uncertain", False

        events = self.stream([[chunk(call_id="call1")], [chunk(call_id="call2")], [chunk(content="两次都失败了")]], execute)
        # A definite local failure does not strand the session: the model may
        # correct and retry, and the run finishes normally.
        self.assertEqual(calls, ["write_file", "write_file"])
        self.assertEqual(events[-1]["type"], "done")
        task = self.db.list_tasks(self.run_id)[0]
        operations = self.db.list_operations(task["id"])
        self.assertEqual({op["status"] for op in operations}, {"failed"})
        self.assertIsNone(self.db.get_active_run(self.session))
        current = self.db.get_session_history(self.session)["session"]["current_message_id"]
        user, next_run = self.db.create_run_turn(self.session, current, "continue", [])
        self.db.attach_task(next_run, task["id"])
        # Failed attempts are definite; a fresh call id on a new run may proceed.
        op = self.db.begin_tool_operation(next_run, "fresh-call", "write_file", {},
                                          task_id=task["id"], goal_version=task["goal_version"])
        self.assertTrue(op[0] > 0)

    def test_parallel_readonly_registration_same_run_does_not_self_lock(self):
        # P2-5：同 run 只读工具批量登记时，先登记的 operation 仍处于
        # running，后登记的不能被「先核实未确定的操作结果」守卫自锁——
        # 这批操作归当前 loop 拥有，不是恢复场景。工具执行体内部模拟
        # 并发段第二个工具的登记（此刻 call1 的账仍挂 running）。
        registered = []

        def execute(name, args, context):
            task = self.db.list_tasks(self.run_id)[0]
            op = self.db.begin_tool_operation(self.run_id, "call2", name, args,
                                              task_id=task["id"], goal_version=task["goal_version"])
            registered.append(op[0])
            self.db.finish_tool_operation(op[0], "second-result", True)
            return "first-result", True

        events = self.stream(
            [[chunk(call_id="call1", name="get_weather", arguments='{"city":"北京"}')],
             [chunk(content="完成")]], execute)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(len(registered), 1)
        task = self.db.list_tasks(self.run_id)[0]
        operations = self.db.list_operations(task["id"])
        # call1（loop 登记）与 call2（工具体内登记）都成功销账
        self.assertEqual({op["tool_call_id"] for op in operations}, {"call1", "call2"})
        self.assertEqual({op["status"] for op in operations}, {"succeeded"})

    def test_cross_run_running_operation_still_blocks_new_registration(self):
        # 守卫只放宽同 run 的 running：别的 run 留下的 running/unknown
        # 照旧拦截，恢复语义不放宽。
        task = self.db.create_task(self.run_id, "stuck task", "running")
        self.db.begin_tool_operation(self.run_id, "call-stuck", "write_file",
                                     {"path": "a", "content": "b"},
                                     task_id=task, goal_version=1)
        # 模拟中断后收尾：期限已过，挂账 run 由 timed_out 关账放行会话
        with self.db.transaction() as connection:
            connection.execute("UPDATE runs SET deadline_at = ? WHERE id = ?",
                               (time.time() - 1, self.run_id))
        self.db.finish_run(self.run_id, "timed_out", "deadline")
        user, next_run = self.db.create_run_turn(self.session, self.user, "resume", [])
        self.db.attach_task(next_run, task)
        with self.assertRaises(StoreError) as blocked:
            self.db.begin_tool_operation(next_run, "call-new", "write_file", {},
                                         task_id=task, goal_version=1)
        self.assertEqual(blocked.exception.code, "operation_needs_reconciliation")


if __name__ == "__main__":
    unittest.main()
