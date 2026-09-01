import os
import unittest
from unittest.mock import Mock, patch


os.environ.setdefault("OPENAI_API_KEY", "test-key")

from agent import approval  # noqa: E402
from services.permission import PermissionRule, RulePermissionService  # noqa: E402


def permission_service(*rules):
    return RulePermissionService(rules)


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
                "preview": None,
            }
        ],
    }


class ApprovalTests(unittest.TestCase):
    def test_rejection_creates_matching_output_without_executing_tool(self):
        batch = pending_batch()
        items = []
        execute = Mock()
        permissions = permission_service(PermissionRule("write", "*", "ask"))

        with patch.object(approval, "execute_tool", execute):
            approval.decide(batch, "call_write", False)
            results = approval.commit_outputs(items, batch, permissions)

        execute.assert_not_called()
        self.assertEqual(results[0]["outcome"], "rejected")
        self.assertEqual(
            items,
            [
                {
                    "type": "function_call_output",
                    "call_id": "call_write",
                    "output": "用户拒绝执行工具 write_file；工具未执行。",
                }
            ],
        )

    def test_approved_tool_executes_once_when_commit_is_retried(self):
        batch = pending_batch()
        items = []
        execute = Mock(return_value="已写入 notes/demo.txt（3 字符）")
        permissions = permission_service(PermissionRule("write", "*", "ask"))

        with patch.object(approval, "execute_approved_tool", execute):
            approval.decide(batch, "call_write", True)
            first = approval.commit_outputs(items, batch, permissions)
            second = approval.commit_outputs(items, batch, permissions)

        execute.assert_called_once_with(
            "write_file", {"path": "notes/demo.txt", "content": "new"}, None
        )
        self.assertEqual(first, second)
        self.assertEqual(len(items), 1)

    def test_repeating_same_decision_is_idempotent_but_opposite_is_rejected(self):
        batch = pending_batch()

        first = approval.decide(batch, "call_write", True)
        second = approval.decide(batch, "call_write", True)

        self.assertIs(first, second)
        with self.assertRaises(ValueError):
            approval.decide(batch, "call_write", False)

    def test_mixed_batch_commits_outputs_in_model_call_order(self):
        calls = [
            (
                type("Call", (), {"call_id": "call_auto", "name": "get_weather"})(),
                {"city": "北京"},
            ),
            (
                type("Call", (), {"call_id": "call_first", "name": "write_file"})(),
                {"path": "notes/a.txt", "content": "a"},
            ),
            (
                type("Call", (), {"call_id": "call_second", "name": "write_file"})(),
                {"path": "notes/b.txt", "content": "b"},
            ),
        ]

        permissions = permission_service(
            PermissionRule("*", "*", "ask"),
            PermissionRule("get_weather", "*", "allow"),
            PermissionRule("write", "*", "ask"),
        )

        with (
            patch.object(approval, "execute_tool", return_value="晴"),
            patch.object(approval, "preview_tool", return_value=None),
        ):
            batch = approval.prepare_batch(
                calls, remaining_turns=7, permission_service=permissions
            )

        approval.decide(batch, "call_second", False)
        approval.decide(batch, "call_first", True)
        items = []
        with patch.object(
            approval, "execute_approved_tool", return_value="已写入 notes/a.txt"
        ):
            approval.commit_outputs(items, batch, permissions)

        self.assertEqual(
            [item["call_id"] for item in items],
            ["call_auto", "call_first", "call_second"],
        )
        self.assertEqual(batch["remaining_turns"], 7)

    def test_denied_tool_is_not_executed_or_added_to_pending_calls(self):
        calls = [
            (
                type("Call", (), {"call_id": "call_write", "name": "write_file"})(),
                {"path": "notes/demo.txt", "content": "new"},
            )
        ]
        permissions = permission_service(PermissionRule("write", "*", "deny"))

        with patch.object(approval, "execute_tool") as execute:
            batch = approval.prepare_batch(calls, 9, permissions)

        execute.assert_not_called()
        self.assertEqual(approval.pending_call_ids(batch), [])
        self.assertEqual(batch["calls"][0]["permission"]["action"], "deny")
        self.assertEqual(batch["calls"][0]["outcome"], "denied")
        self.assertIn("权限规则拒绝", batch["calls"][0]["output"])

    def test_approved_call_is_blocked_if_permission_changes_to_deny(self):
        batch = pending_batch()
        approval.decide(batch, "call_write", True)
        permissions = permission_service(PermissionRule("write", "*", "deny"))

        with patch.object(approval, "execute_approved_tool") as execute:
            output = approval.execute_approved_call(
                batch["calls"][0], permissions
            )

        execute.assert_not_called()
        self.assertIn("权限规则拒绝", output)
        self.assertEqual(batch["calls"][0]["decision"], "approved")
        self.assertEqual(batch["calls"][0]["outcome"], "denied")

    def test_approved_tool_failure_has_failed_outcome(self):
        batch = pending_batch()
        approval.decide(batch, "call_write", True)
        permissions = permission_service(PermissionRule("write", "*", "ask"))

        with patch.object(
            approval, "execute_approved_tool", side_effect=ValueError("stale preview")
        ):
            approval.execute_approved_call(batch["calls"][0], permissions)

        self.assertEqual(batch["calls"][0]["decision"], "approved")
        self.assertEqual(batch["calls"][0]["outcome"], "failed")

    def test_permission_service_failure_is_closed_without_execution(self):
        calls = [
            (
                type("Call", (), {"call_id": "call_weather", "name": "get_weather"})(),
                {"city": "北京"},
            )
        ]

        class BrokenPermissionService:
            def check(self, requests, context):
                raise TimeoutError("permission backend unavailable")

        with patch.object(approval, "execute_tool") as execute:
            batch = approval.prepare_batch(calls, 9, BrokenPermissionService())

        execute.assert_not_called()
        self.assertEqual(batch["calls"][0]["permission"]["action"], "deny")
        self.assertIn("权限检查失败", batch["calls"][0]["output"])


if __name__ == "__main__":
    unittest.main()
