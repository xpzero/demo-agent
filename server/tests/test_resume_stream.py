"""Resume contracts: independent replay, terminal facts, and orphan arbitration."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("API_KEY", "test-key")

from api.event_buffer import EventBufferRegistry, RunEventBuffer, buffers, sse
from api.resume_stream import resume_response
from database import Database
from fastapi import HTTPException, Request as StarletteRequest
from fastapi.responses import StreamingResponse
from typing import AsyncGenerator, cast
from recovery.demo_adapter import DemoBusinessAdapter


class Request(StarletteRequest):
    def __init__(self):
        super().__init__({"type": "http"})

    async def is_disconnected(self):
        return False


def events(frames):
    return [json.loads(line[6:]) for frame in frames for line in frame.splitlines()
            if line.startswith("data: ")]


class EventBufferTests(unittest.TestCase):
    def test_subscribers_replay_independently_and_cursor_skips_seen_events(self):
        buffer = RunEventBuffer(7)
        first = buffer.append(sse({"type": "text_delta", "text": "甲"}))
        second = buffer.append(sse({"type": "text_delta", "text": "乙"}))
        valid, position = buffer.subscribe(None)
        self.assertFalse(valid)
        self.assertEqual([frame for _, frame in buffer.read(position)[1]], [first, second])
        valid, position = buffer.subscribe((7, 1))
        self.assertTrue(valid)
        self.assertEqual([frame for _, frame in buffer.read(position)[1]], [second])
        # Reading another subscriber does not remove the first subscriber's data.
        self.assertEqual(len(buffer.read(0)[1]), 2)
        self.assertTrue(first.startswith("id: 7:1\n"))
        buffer.unsubscribe()
        buffer.unsubscribe()
        self.assertEqual(buffer.subscribers, 0)

    def test_overflow_disables_partial_replay_and_invalidates_cursor(self):
        for options in ({"max_events": 1}, {"max_bytes": 1}):
            buffer = RunEventBuffer(7, **options)
            buffer.append(sse({"type": "text_delta", "text": "甲"}))
            buffer.append(sse({"type": "text_delta", "text": "乙"}))
            self.assertEqual(buffer.read(0), (False, [], False))
            self.assertFalse(buffer.subscribe((7, 1))[0])
            self.assertEqual(buffer.size, 0)
            buffer.unsubscribe()

    def test_subscription_limit_and_releasing_slot(self):
        buffer = RunEventBuffer(7)
        for _ in range(8):
            buffer.subscribe(None)
        with self.assertRaises(OverflowError):
            buffer.subscribe(None)
        buffer.unsubscribe()
        buffer.subscribe(None)
        self.assertEqual(buffer.subscribers, 8)

    def test_terminal_retention_removes_registry_but_existing_reader_survives(self):
        registry = EventBufferRegistry(retention_seconds=60)
        class DB:
            path = Path("retention-test")
        db = DB()
        buffer = registry.register(db, 1, 7)
        frame = buffer.append(sse({"type": "done", "content": "甲"}))
        registry.terminal(db, 1)
        with registry.condition:
            registry.expiry[registry.key(db, 1)] = time.monotonic() - 1
            registry._cleanup()
        self.assertIsNone(registry.get(db, 1))
        self.assertEqual(buffer.read(0)[1], [(1, frame)])


class ResumeStreamTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.directory.name) / "agent.sqlite3")
        self.db.initialize()
        self.session = str(uuid4())
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.clear_buffers)

    def clear_buffers(self):
        with buffers.condition:
            for key in list(buffers.buffers):
                if key[0] == str(self.db.path):
                    buffers.buffers.pop(key, None)
                    buffers.expiry.pop(key, None)

    def create_run(self):
        return self.db.create_run_turn(self.session, None, "开始", [], deadline_at=time.time() + 200)

    def response(self, cursor=None):
        return resume_response(self.db, self.session, Request(), cursor)

    def iterator(self, response) -> AsyncGenerator[str, None]:
        assert isinstance(response, StreamingResponse)
        return cast(AsyncGenerator[str, None], response.body_iterator)

    def collect(self, response):
        async def consume():
            return [frame async for frame in self.iterator(response)]
        return asyncio.run(asyncio.wait_for(consume(), 4))

    def test_live_replay_includes_new_events_and_keeps_text_tool_order(self):
        user, run = self.create_run()
        buffer = buffers.register(self.db, run, user)
        buffer.append(sse({"type": "text_delta", "text": "工具前"}))
        response = self.response()

        async def consume():
            iterator = self.iterator(response)
            first = await anext(iterator)
            buffer.append(sse({"type": "tool_start", "call_id": "c", "name": "weather", "arguments": {}}))
            buffer.append(sse({"type": "tool_end", "call_id": "c", "result": "晴"}))
            buffer.append(sse({"type": "text_delta", "text": "工具后"}))
            buffer.append(sse({"type": "done", "content": "工具前工具后"}))
            self.db.finish_run(run, "completed")
            buffer.finish_producing()
            return [first] + [frame async for frame in iterator]
        payload = events(asyncio.run(asyncio.wait_for(consume(), 4)))
        self.assertTrue(payload[0]["replay"])
        self.assertEqual(payload[0]["user_message_id"], user)
        self.assertEqual([event["type"] for event in payload], ["resume_snapshot", "text_delta", "tool_start", "tool_end", "text_delta", "done", "stream_end"])
        self.assertEqual(buffer.subscribers, 0)

    def test_valid_cursor_resumes_without_snapshot_or_previous_deltas(self):
        user, run = self.create_run()
        buffer = buffers.register(self.db, run, user)
        buffer.append(sse({"type": "text_delta", "text": "已看到"}))
        buffer.append(sse({"type": "text_delta", "text": "新的"}))
        response = self.response(f"{user}:1")
        self.db.finish_run(run, "completed")
        buffer.finish_producing()
        payload = events(self.collect(response))
        self.assertEqual(payload, [{"type": "text_delta", "text": "新的"}, {"type": "stream_end"}])

    def test_invalid_cursor_gets_full_snapshot_and_replay(self):
        user, run = self.create_run()
        buffer = buffers.register(self.db, run, user)
        buffer.append(sse({"type": "text_delta", "text": "完整"}))
        response = self.response(f"{user + 1}:999")
        self.db.finish_run(run, "completed")
        buffer.finish_producing()
        payload = events(self.collect(response))
        self.assertTrue(payload[0]["replay"])
        self.assertEqual(payload[1]["text"], "完整")

    def test_done_does_not_end_stream_while_business_is_finishing(self):
        user, run = self.create_run()
        buffer = buffers.register(self.db, run, user)
        buffer.append(sse({"type": "done", "content": "文字完成"}))
        self.db.enter_finishing(run)
        buffer.finish_producing()
        response = self.response()

        async def consume():
            iterator = self.iterator(response)
            self.assertEqual(events([await anext(iterator)])[0]["type"], "resume_snapshot")
            self.assertEqual(events([await anext(iterator)])[0]["type"], "done")
            pending = asyncio.create_task(anext(iterator))
            await asyncio.sleep(0.1)
            self.assertFalse(pending.done(), "finishing must remain subscribed after done")
            self.db.finish_run(run, "completed")
            self.assertEqual(events([await pending])[0]["type"], "stream_end")
            await iterator.aclose()
        asyncio.run(asyncio.wait_for(consume(), 4))

    def test_terminal_run_emits_facts_even_if_old_buffer_is_retained(self):
        user, run = self.create_run()
        self.db.upsert_partial_assistant(run, self.session, user, "部分正文")
        buffer = buffers.register(self.db, run, user)
        buffer.append(sse({"type": "text_delta", "text": "部分正文"}))
        self.db.finish_run(run, "stopped")
        payload = events(self.collect(self.response()))
        self.assertEqual([event["type"] for event in payload], ["resume_snapshot", "stream_end"])
        self.assertFalse(payload[0]["replay"])
        self.assertTrue(payload[0]["history"]["can_send_message"])
        self.assertEqual(payload[0]["history"]["messages"][-1]["content"], "部分正文")
        self.assertNotIn("id", payload[0]["history"]["progress"]["runs"][0])

    def test_orphan_uses_original_deadline_before_releasing_slot(self):
        _, run = self.create_run()
        # Process startup owns orphan reconciliation; resume is read-only.
        self.db.recover_orphan_runs()
        self.assertIsNotNone(self.db.get_active_run(self.session))
        with self.db.transaction() as conn:
            conn.execute("UPDATE runs SET deadline_at=? WHERE id=?", (time.time() - 1, run))
        payload = events(self.collect(self.response()))
        current = self.db.get_run(run)
        assert current is not None
        self.assertEqual(current["reason"], "worker_lost_on_restart")
        self.assertFalse(payload[0]["replay"])
        self.assertTrue(payload[0]["history"]["can_send_message"])
        self.assertEqual(payload[-1]["type"], "stream_end")

    def test_unconfirmed_business_orphan_preserves_original_deadline(self):
        _, run = self.create_run()
        adapter = DemoBusinessAdapter(Path(self.directory.name) / "business.sqlite3")
        task = self.db.create_task(run, "订单", "active")
        args = {"value": "样品"}
        self.db.approve_step(task, "order", adapter.TOOL, args, run_id=run, goal_version=1, adapter=adapter)
        self.db.begin_approved_step(run, task, "order", "call", adapter.TOOL, args, goal_version=1)
        before = self.db.get_run(run)
        assert before is not None
        deadline = before["deadline_at"]
        self.db.recover_orphan_runs()
        response = self.response()
        current = self.db.get_run(run)
        assert current is not None
        self.assertEqual(current["status"], "finishing")
        self.assertEqual(current["deadline_at"], deadline)
        self.assertIsNotNone(self.db.get_active_run(self.session))
        with self.db.transaction() as conn:
            conn.execute("UPDATE runs SET deadline_at=? WHERE id=?", (time.time() - 1, run))
        self.db.reconcile_orphan_run(run)
        payload = events(self.collect(response))
        self.assertFalse(payload[0]["replay"])
        self.assertEqual(payload[-1]["type"], "stream_end")

    def test_subscriber_limit_returns_429_and_cancel_releases_slot(self):
        user, run = self.create_run()
        buffer = buffers.register(self.db, run, user)
        for _ in range(8):
            buffer.subscribe(None)
        with self.assertRaises(HTTPException) as error:
            self.response()
        self.assertEqual(error.exception.status_code, 429)
        for _ in range(8):
            buffer.unsubscribe()
        response = self.response()
        async def cancel():
            iterator = self.iterator(response)
            await anext(iterator)
            await iterator.aclose()
        asyncio.run(cancel())
        self.assertEqual(buffer.subscribers, 0)

    def test_complete_idle_session_returns_204(self):
        user, run = self.create_run()
        self.db.upsert_partial_assistant(run, self.session, user, "完成")
        self.db.finish_run(run, "completed", assistant_status="finished", assistant_content="完成")
        self.assertEqual(self.response().status_code, 204)

    def test_malformed_cursor_returns_400(self):
        self.create_run()
        for cursor in ("bad", "1:0", "-1:2"):
            with self.assertRaises(HTTPException) as error:
                self.response(cursor)
            self.assertEqual(error.exception.status_code, 400)

    def test_missing_buffer_does_not_reclaim_a_running_producer(self):
        _, run = self.create_run()
        response = self.response()
        active = self.db.get_active_run(self.session)
        assert active is not None
        self.assertEqual(active["status"], "running")
        async def cancel():
            iterator = self.iterator(response)
            snapshot = events([await anext(iterator)])[0]
            self.assertFalse(snapshot["replay"])
            self.assertFalse(snapshot["history"]["can_send_message"])
            await iterator.aclose()
        asyncio.run(cancel())
        current = self.db.get_run(run)
        assert current is not None
        self.assertEqual(current["status"], "running")

    def test_missing_session_returns_404(self):
        with self.assertRaises(HTTPException) as error:
            self.response()
        self.assertEqual(error.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
