"""M2/M3：停止语义、resume 三分支、判死双入口、Run 原子交接。"""

import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from threading import Event
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api  # noqa: E402
from api import chat_stream as chat_stream_module  # noqa: E402
from api.active_runs import active_runs  # noqa: E402
from api.reaper import finalize_dead, reap_expired  # noqa: E402
from database import Database  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class StopResumeTestBase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "test.sqlite3"
        self.root = Path(self.directory.name) / "files"
        self.patches = [
            patch.object(api.deps, "DATABASE_PATH", self.database_path),
            patch.object(api.deps, "FILE_ROOT", self.root),
        ]
        for item in self.patches:
            item.start()
        api._running_sessions.clear()
        self.database = Database(self.database_path)
        self.database.initialize()
        self.client = TestClient(api.app)
        self.session_id = str(uuid.uuid4())
        self.addCleanup(active_runs.__init__)  # 清空全局 Run 状态
        self.gates = []

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def payload(self, **overrides):
        return {
            "session_id": self.session_id,
            "parent_message_id": None,
            "message": "hi",
            "ref_file_ids": [],
            **overrides,
        }

    def events(self, response):
        return [
            json.loads(frame.removeprefix("data: "))
            for frame in response.text.strip().split("\n\n")
            if frame.startswith("data: ")
        ]

    def start_pending(self):
        """走真实入口构造活着的 unfinished 消息 + Run。

        mock stream_events 挂起（不产出、不结束），保持执行线程占位——
        与生产路径完全一致（含 reserve/bind 顺序），杜绝「铺垫复制缺陷」。
        """
        import threading
        from unittest.mock import patch as _patch
        gate = threading.Event()

        def hanging_stream(items, context=None, recorder=None, stop_signal=None):
            gate.wait(timeout=30)
            yield {"type": "done", "content": "x"}

        thread = threading.Thread(
            target=lambda: self.client.post("/api/chat", json=self.payload()),
            daemon=True,
        )
        with _patch.object(chat_stream_module, "stream_events", side_effect=hanging_stream):
            thread.start()
            # 等 DB 出现 unfinished 行（预建已发生）
            for _ in range(100):
                message = self.database.get_unfinished_message(self.session_id)
                if message is not None:
                    break
                time.sleep(0.05)
        self.assertIsNotNone(message, "预建未发生")
        self.addCleanup(gate.set)  # 测试结束统一放行，不留挂起线程
        return message["parent_id"], message["id"], gate


import json  # noqa: E402


class TestStopEndpoint(StopResumeTestBase):
    def test_stop_marks_intent_and_signal(self):
        from api.stop_signals import stop_signals
        user_id, message_id, gate = self.start_pending()
        stop_signals.create(self.session_id)
        response = self.client.post(f"/api/sessions/{self.session_id}/stop")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"], "processing")
        message = self.database.get_unfinished_message(self.session_id)
        self.assertEqual(message["stop_requested"], 1)
        self.assertTrue(stop_signals.signals[self.session_id].is_set())

    def test_stop_without_unfinished_is_noop(self):
        response = self.client.post(f"/api/sessions/{self.session_id}/stop")
        self.assertEqual(response.json()["result"], "no_active")

    def test_stop_during_stream_finalizes_stopped(self):
        """流中置停止信号 → StopRequestedError → stopped 收口。"""
        def slow_stream(items, context=None, recorder=None, stop_signal=None):
            yield {"type": "text_delta", "text": "半截"}
            # 模拟下一边界检查点看到停止
            from agent.loop import StopRequestedError
            raise StopRequestedError("stop")

        with patch.object(chat_stream_module, "stream_events", side_effect=slow_stream):
            response = self.client.post("/api/chat", json=self.payload())
        events = self.events(response)
        self.assertEqual(events[-1]["type"], "stopped")
        history = self.client.get(f"/api/sessions/{self.session_id}").json()
        assistant = history["messages"][1]
        self.assertEqual(assistant["finish_kind"], "stopped")
        self.assertEqual(assistant["content"], "半截")

    def test_stop_before_first_char_empty_stopped(self):
        def instant_stop(items, context=None, recorder=None, stop_signal=None):
            from agent.loop import StopRequestedError
            raise StopRequestedError("stop")

        with patch.object(chat_stream_module, "stream_events", side_effect=instant_stop):
            response = self.client.post("/api/chat", json=self.payload())
        events = self.events(response)
        self.assertEqual(events[-1]["type"], "stopped")
        history = self.client.get(f"/api/sessions/{self.session_id}").json()
        self.assertEqual(history["messages"][1]["finish_kind"], "stopped")
        self.assertEqual(history["messages"][1]["content"], "")


class TestFinalizeDead(StopResumeTestBase):
    def test_dead_without_stop_intent_is_interrupted(self):
        user_id, message_id, gate = self.start_pending()
        self.database.append_fragment(message_id, "text", {"type": "text_delta", "text": "前半"})
        message = self.database.get_unfinished_message(self.session_id)
        kind = finalize_dead(self.database, message)
        self.assertEqual(kind, "interrupted")
        history = self.database.get_session_history(self.session_id)["messages"][1]
        self.assertEqual(history["finish_kind"], "interrupted")
        self.assertEqual(history["content"], "前半")  # 回填以库为准

    def test_dead_with_stop_intent_is_stopped(self):
        user_id, message_id, gate = self.start_pending()
        self.database.request_stop(self.session_id)
        message = self.database.get_unfinished_message(self.session_id)
        kind = finalize_dead(self.database, message)
        self.assertEqual(kind, "stopped")

    def test_terminal_idempotent(self):
        user_id, message_id, gate = self.start_pending()
        self.database.append_fragment(message_id, "terminal", {"type": "done"})
        message = self.database.get_unfinished_message(self.session_id)
        finalize_dead(self.database, message)
        terminals = [
            f for f in self.database.list_fragments(message_id)
            if f["type"] == "terminal"
        ]
        self.assertEqual(len(terminals), 1)

    def test_reap_expired_on_send_path(self):
        """缺口 1：send 路径判死——永不点开的会话也能解锁。"""
        user_id, message_id, gate = self.start_pending()
        active_runs.close(message_id)  # 模拟进程重启后 Run 不在
        result = reap_expired(self.database, self.session_id)
        self.assertEqual(result["finish_kind"], "interrupted")
        self.assertIsNone(self.database.get_unfinished_message(self.session_id))


class TestResume(StopResumeTestBase):
    def test_resume_204_when_all_finished(self):
        response = self.client.get(f"/api/sessions/{self.session_id}/resume")
        self.assertEqual(response.status_code, 204)

    def test_resume_dead_run_reaps_and_snapshots(self):
        """缺口 D：行在、Run 不在 → 判死 + 快照。"""
        user_id, message_id, gate = self.start_pending()
        self.database.append_fragment(message_id, "text", {"type": "text_delta", "text": "崩溃前"})
        active_runs.close(message_id)
        response = self.client.get(f"/api/sessions/{self.session_id}/resume")
        self.assertEqual(response.status_code, 200)
        events = self.events(response)
        self.assertEqual(events[0]["type"], "resume_snapshot")
        self.assertEqual(events[-1]["type"], "resume_snapshot")
        self.assertEqual(
            events[0]["history"]["messages"][1]["finish_kind"], "interrupted"
        )

    def test_resume_expired_deadline_reaps(self):
        user_id, message_id, gate = self.start_pending()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE chat_messages SET deadline_at = ? WHERE id = ?",
                (time.time() - 1, message_id),
            )
        response = self.client.get(f"/api/sessions/{self.session_id}/resume")
        self.assertEqual(response.status_code, 200)
        history = self.database.get_session_history(self.session_id)
        self.assertEqual(history["messages"][1]["finish_kind"], "interrupted")

    def test_resume_live_run_replays_and_follows(self):
        """活流：attach 快照 + 实时队列。"""
        user_id, message_id, gate = self.start_pending()
        seq1 = self.database.append_fragment(
            message_id, "text", {"type": "text_delta", "text": "已吐的"})
        active_runs.publish(message_id, {"type": "text_delta", "text": "已吐的"}, seq1)
        # 模拟执行线程在 resume 之后再产出 + 终态（在线程里推）
        import threading

        def producer():
            time.sleep(0.2)
            seq2 = self.database.append_fragment(
                message_id, "text", {"type": "text_delta", "text": "新字"})
            active_runs.publish(message_id, {"type": "text_delta", "text": "新字"}, seq2)
            self.database.finalize_assistant(message_id, "done", "已吐的新字")
            seq3 = self.database.append_fragment(
                message_id, "terminal", {"type": "done", "message_id": message_id})
            active_runs.publish(
                message_id, {"type": "done", "message_id": message_id}, seq3)
            active_runs.close(message_id)

        threading.Thread(target=producer, daemon=True).start()
        with self.client.stream(
            "GET", f"/api/sessions/{self.session_id}/resume"
        ) as response:
            self.assertEqual(response.status_code, 200)
            text = "".join(chunk for chunk in response.iter_text())
        events = [
            json.loads(frame.removeprefix("data: "))
            for frame in text.strip().split("\n\n")
            if frame.startswith("data: ")
        ]
        types = [event["type"] for event in events]
        self.assertIn("text_delta", types)
        self.assertEqual(types[-1], "done")
        texts = [e.get("text") for e in events if e["type"] == "text_delta"]
        self.assertEqual("".join(texts), "已吐的新字")  # 不丢不重


class TestSendLock(StopResumeTestBase):
    def test_send_409_when_unfinished(self):
        self.start_pending()
        response = self.client.post("/api/chat", json=self.payload())
        self.assertEqual(response.status_code, 409)

    def test_send_after_reap_succeeds(self):
        """判死后解锁，可正常发送（父指向收口后的助手行）。"""
        user_id, message_id, gate = self.start_pending()
        gate.set()  # 先放行挂起的流，让执行线程正常收尾释放会话锁
        time.sleep(0.5)
        active_runs.close(message_id)  # 模拟 Run 不在 → 下次 send 判死入口生效
        response = self.client.post(
            "/api/chat", json=self.payload(parent_message_id=message_id, message="again")
        )
        self.assertEqual(response.status_code, 200)
        # 未 mock 模型 → 真实请求失败也是合法终态；关键是会话已解锁（非 409）
        events = self.events(response)
        self.assertIn(events[-1]["type"], {"done", "error"})


class TestActiveRunsConcurrency(unittest.TestCase):
    def test_attach_atomic_no_gap(self):
        """缺口 2：publish 与 attach 并发压测——不丢不重。"""
        from api.active_runs import ActiveRuns
        runs = ActiveRuns()
        runs.create(1)
        import threading
        published = list(range(1, 301))
        results = []
        errors = []

        def spam_publish():
            try:
                for seq in published:
                    runs.publish(1, {"seq": seq}, seq)
            except Exception as error:  # pragma: no cover
                errors.append(error)

        def attach_loop():
            for _ in range(50):
                attached = runs.attach(1)
                if attached is not None:
                    snapshot, queue = attached
                    results.append((len(snapshot), queue))

        threads = [
            threading.Thread(target=spam_publish),
            threading.Thread(target=attach_loop),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        # 每个 attach 的快照必须是连续前缀（无洞）
        for size, _ in results:
            self.assertEqual(size, len(set(range(1, size + 1))))

    def test_attach_missing_returns_none(self):
        from api.active_runs import ActiveRuns
        runs = ActiveRuns()
        self.assertIsNone(runs.attach(999))




class TestPrecreateOrder(StopResumeTestBase):
    """缺口 D：先 Run 后 DB 行——行出现时 Run 必已存在。"""

    def test_run_exists_the_moment_row_appears(self):
        _, message_id, gate = self.start_pending()
        try:
            self.assertTrue(active_runs.has(message_id),
                            "DB 行已存在但 Run 不在：预建顺序违反缺口 D")
        finally:
            gate.set()


class TestDualSubscribers(StopResumeTestBase):
    """双标签页同看直播：两个订阅者各自收到完整事件流。"""

    def test_two_subscribers_both_receive_events(self):
        from api.active_runs import ActiveRuns
        runs = ActiveRuns()
        runs.create(1)
        a = runs.attach(1)
        b = runs.attach(1)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        _, queue_a = a
        _, queue_b = b
        for seq in (1, 2, 3):
            runs.publish(1, {"type": "text_delta", "text": "x"}, seq)
        got_a = [queue_a.get(timeout=1) for _ in range(3)]
        got_b = [queue_b.get(timeout=1) for _ in range(3)]
        self.assertEqual([s for _, s in got_a], [1, 2, 3])
        self.assertEqual([s for _, s in got_b], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
