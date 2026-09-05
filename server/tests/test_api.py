import os
import unittest
from unittest.mock import Mock, patch


os.environ.setdefault("OPENAI_API_KEY", "test-key")

import api  # noqa: E402
from agent import approval  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from fakes import FakeSessionService  # noqa: E402
from sessions import SessionStorageError  # noqa: E402


def pending_batch():
    return {
        "schema_version": 2,
        "remaining_turns": 9,
        "outputs_committed": False,
        "calls": [
            {
                "id": "call_write",
                "name": "write_file",
                "args": {"path": "notes/demo.txt", "content": "new"},
                "permission": {
                    "action": "ask",
                    "requests": [
                        {"permission": "write", "target": "notes/demo.txt"}
                    ],
                    "reason": "测试规则",
                },
                "decision": None,
                "outcome": None,
                "output": None,
                "guard": {
                    "canonical_path": "/private/server/notes/demo.txt",
                    "existed": True,
                    "content_hash": "hash",
                },
                "preview": {
                    "type": "code_diff",
                    "path": "notes/demo.txt",
                    "additions": 1,
                    "deletions": 0,
                    "lines": [{"kind": "added", "text": "new"}],
                },
            }
        ],
    }


class ApprovalApiTests(unittest.TestCase):
    def setUp(self):
        self.sessions = FakeSessionService()
        self.app = api.create_app(session_service_factory=lambda: self.sessions)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.session = self.sessions.create("system")
        self.session.items.append({"role": "user", "content": "write"})
        self.session.items.append(
            {
                "type": "function_call",
                "id": "fc_write",
                "call_id": "call_write",
                "name": "write_file",
                "arguments": '{"path":"notes/demo.txt","content":"new"}',
            }
        )
        self.session.pending_approval = pending_batch()
        self.sessions.save(self.session)

    def tearDown(self):
        self.client_context.__exit__(None, None, None)

    def test_pending_snapshot_and_chat_conflict(self):
        snapshot = self.client.get(f"/api/sessions/{self.session.id}")
        conflict = self.client.post(
            f"/api/sessions/{self.session.id}/chat",
            json={"message": "another message"},
        )

        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(
            snapshot.json()["pending_approval"]["calls"][0]["id"], "call_write"
        )
        self.assertNotIn("guard", snapshot.json()["pending_approval"]["calls"][0])
        self.assertEqual(conflict.status_code, 409)

    def test_create_persists_empty_session_without_global_current(self):
        created = self.client.post("/api/sessions")
        session_id = created.json()["id"]
        loaded = self.client.get(f"/api/sessions/{session_id}")
        listing = self.client.get("/api/sessions")

        self.assertEqual(created.status_code, 200)
        self.assertEqual(loaded.status_code, 200)
        self.assertEqual(loaded.json()["summary"], "(空会话)")
        self.assertNotIn("current", listing.json()[-1])

    def test_chat_save_failure_releases_run_and_keeps_persisted_input_unchanged(self):
        session = self.sessions.get(self.session.id)
        session.pending_approval = None
        self.sessions.save(session)
        original = self.sessions.get(self.session.id).to_dict()
        self.sessions.next_save_error = SessionStorageError("database unavailable")

        response = self.client.post(
            f"/api/sessions/{self.session.id}/chat",
            json={"message": "not persisted"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.app.state.runtime.is_running(self.session.id))
        self.assertEqual(self.sessions.get(self.session.id).to_dict(), original)

    def test_final_save_failure_sends_error_instead_of_done(self):
        session = self.sessions.get(self.session.id)
        session.pending_approval = None
        self.sessions.save(session)

        def stream(items, *_, **__):
            yield {"type": "text_delta", "text": "visible"}
            items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "visible"}],
                }
            )
            self.sessions.next_save_error = SessionStorageError("database unavailable")
            yield {"type": "done", "content": "visible"}

        with patch.object(api, "stream_events", stream):
            response = self.client.post(
                f"/api/sessions/{self.session.id}/chat",
                json={"message": "hello"},
            )

        self.assertIn('"type": "text_delta"', response.text)
        self.assertIn('"type": "error"', response.text)
        self.assertNotIn('"type": "done"', response.text)
        self.assertFalse(self.app.state.runtime.is_running(self.session.id))

    def test_terminal_event_releases_run_before_stream_finishes(self):
        session = self.sessions.get(self.session.id)
        session.pending_approval = None
        self.sessions.save(session)
        observed = []

        def stream(*_, **__):
            yield {"type": "approval_required", "call_ids": ["call_1"]}
            observed.append(self.app.state.runtime.is_running(self.session.id))

        with patch.object(api, "stream_events", stream):
            response = self.client.post(
                f"/api/sessions/{self.session.id}/chat",
                json={"message": "hello"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed, [False])

    def test_approve_is_idempotent_and_resume_executes_once(self):
        endpoint = f"/api/sessions/{self.session.id}/approvals/call_write/approve"
        execute = Mock(return_value="已写入 notes/demo.txt（3 字符）")
        stream_events = Mock(
            return_value=iter([{"type": "done", "content": "完成"}])
        )

        with (
            patch.object(approval, "execute_approved_tool", execute),
            patch.object(api, "stream_events", stream_events),
        ):
            first = self.client.post(endpoint)
            saves_after_first = len(self.sessions.save_calls)
            second = self.client.post(endpoint)
            saves_after_second = len(self.sessions.save_calls)
            resumed = self.client.post(f"/api/sessions/{self.session.id}/resume")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(saves_after_first, saves_after_second)
        execute.assert_called_once()
        self.assertEqual(stream_events.call_args.kwargs["max_turns"], 9)
        self.assertIn("已写入 notes/demo.txt", resumed.text)
        restored = self.sessions.get(self.session.id)
        self.assertIsNone(restored.pending_approval)
        self.assertEqual(
            restored.items[-1],
            {
                "type": "function_call_output",
                "call_id": "call_write",
                "output": "已写入 notes/demo.txt（3 字符）",
            },
        )

    def test_approved_tool_save_failure_reports_uncertain_outcome(self):
        execute = Mock(return_value="已写入 notes/demo.txt（3 字符）")
        self.sessions.next_save_error = SessionStorageError("database unavailable")

        with patch.object(approval, "execute_approved_tool", execute):
            response = self.client.post(
                f"/api/sessions/{self.session.id}/approvals/call_write/approve"
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("工具执行已经开始", response.json()["detail"])
        execute.assert_called_once()
        restored = self.sessions.get(self.session.id)
        self.assertIsNone(restored.pending_approval["calls"][0]["decision"])

    def test_reject_never_executes_tool_but_resume_returns_protocol_output(self):
        rejected = self.client.post(
            f"/api/sessions/{self.session.id}/approvals/call_write/reject"
        )
        execute = Mock()

        with (
            patch.object(approval, "execute_tool", execute),
            patch.object(
                api,
                "stream_events",
                return_value=iter([{"type": "done", "content": "已取消"}]),
            ),
        ):
            resumed = self.client.post(f"/api/sessions/{self.session.id}/resume")

        self.assertEqual(rejected.status_code, 200)
        execute.assert_not_called()
        self.assertIn("用户拒绝执行工具 write_file", resumed.text)
        self.assertEqual(
            self.sessions.get(self.session.id).items[-1]["call_id"], "call_write"
        )


if __name__ == "__main__":
    unittest.main()
