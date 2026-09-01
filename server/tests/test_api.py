import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


os.environ.setdefault("OPENAI_API_KEY", "test-key")

from sessions import manager as manager_module  # noqa: E402


TEST_DATA = tempfile.TemporaryDirectory()
manager_module.DATA_DIR = Path(TEST_DATA.name)

import api  # noqa: E402
from agent import approval  # noqa: E402
from sessions import SessionManager  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


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
        for path in Path(TEST_DATA.name).glob("*"):
            path.unlink()
        api.manager = SessionManager("system")
        api._running_sessions.clear()
        api._session_locks.clear()
        self.client = TestClient(api.app)
        self.session = api.manager.current
        self.session.items.append({"role": "user", "content": "write"})
        self.session.pending_approval = pending_batch()
        api.manager.save(self.session)

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
        self.assertEqual(
            snapshot.json()["pending_approval"]["calls"][0]["permission"],
            {
                "action": "ask",
                "requests": [
                    {"permission": "write", "target": "notes/demo.txt"}
                ],
                "reason": "测试规则",
            },
        )
        self.assertEqual(conflict.status_code, 409)

    def test_pending_snapshot_exposes_three_permission_actions_without_legacy_policy(self):
        self.session.pending_approval["calls"].append(
            {
                "id": "call_denied",
                "name": "write_file",
                "args": {
                    "path": "notes/permission-deny-demo.txt",
                    "content": "blocked",
                },
                "permission": {
                    "action": "deny",
                    "requests": [
                        {
                            "permission": "write",
                            "target": "notes/permission-deny-demo.txt",
                        }
                    ],
                    "reason": "测试拒绝规则",
                },
                "decision": None,
                "outcome": "denied",
                "output": "权限规则拒绝执行工具 write_file；工具未执行。",
                "guard": None,
                "preview": None,
            }
        )
        self.session.pending_approval["calls"].append(
            {
                "id": "call_allowed",
                "name": "read_file",
                "args": {"path": "notes/demo.txt"},
                "permission": {
                    "action": "allow",
                    "requests": [
                        {"permission": "read", "target": "notes/demo.txt"}
                    ],
                    "reason": "测试允许规则",
                },
                "decision": None,
                "outcome": "completed",
                "output": "old",
                "guard": None,
                "preview": None,
            }
        )
        api.manager.save(self.session)

        response = self.client.get(f"/api/sessions/{self.session.id}")
        calls = response.json()["pending_approval"]["calls"]
        denied = calls[1]
        allowed = calls[2]

        self.assertEqual(calls[0]["permission"]["action"], "ask")
        self.assertEqual(denied["permission"]["action"], "deny")
        self.assertIsNone(denied["decision"])
        self.assertEqual(denied["outcome"], "denied")
        self.assertEqual(allowed["permission"]["action"], "allow")
        self.assertNotIn("policy", denied)

    def test_chat_save_failure_releases_run_reservation_and_rolls_back_input(self):
        self.session.pending_approval = None
        original_items = list(self.session.items)

        with patch.object(api.manager, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.client.post(
                    f"/api/sessions/{self.session.id}/chat",
                    json={"message": "not persisted"},
                )

        self.assertNotIn(self.session.id, api._running_sessions)
        self.assertEqual(self.session.items, original_items)

    def test_approve_is_idempotent_and_resume_executes_once(self):
        endpoint = (
            f"/api/sessions/{self.session.id}/approvals/call_write/approve"
        )
        execute = Mock(return_value="已写入 notes/demo.txt（3 字符）")
        stream_events = Mock(
            return_value=iter([{"type": "done", "content": "完成"}])
        )

        with (
            patch.object(approval, "execute_approved_tool", execute),
            patch.object(api, "stream_events", stream_events),
        ):
            first = self.client.post(endpoint)
            second = self.client.post(endpoint)
            resumed = self.client.post(f"/api/sessions/{self.session.id}/resume")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        execute.assert_called_once()
        self.assertEqual(stream_events.call_args.kwargs["max_turns"], 9)
        self.assertIs(stream_events.call_args.args[1], api.services)
        self.assertIn("已写入 notes/demo.txt", resumed.text)
        self.assertIsNone(self.session.pending_approval)
        self.assertEqual(
            self.session.items[-1],
            {
                "type": "function_call_output",
                "call_id": "call_write",
                "output": "已写入 notes/demo.txt（3 字符）",
            },
        )
        self.assertEqual(
            self.client.post(f"/api/sessions/{self.session.id}/resume").status_code,
            409,
        )

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
            self.session.items[-1]["call_id"],
            "call_write",
        )


if __name__ == "__main__":
    unittest.main()
