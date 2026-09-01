import json
import tempfile
import unittest
from pathlib import Path

from services import DEFAULT_PERMISSION_PATH, create_default_services
from services.permission import (
    PermissionCheckContext,
    PermissionRequest,
    PermissionRule,
    RulePermissionService,
    load_permission_rules,
)
from tools import describe_tool_permissions


CONTEXT = PermissionCheckContext(call_id="call_1", tool_name="tool")


class RulePermissionServiceTests(unittest.TestCase):
    def test_last_matching_rule_wins(self):
        service = RulePermissionService(
            [
                PermissionRule("write", "*", "ask"),
                PermissionRule("write", "notes/*", "allow"),
                PermissionRule("write", "notes/private/*", "deny"),
            ]
        )

        self.assertEqual(
            service.check([PermissionRequest("write", "src/main.py")], CONTEXT).action,
            "ask",
        )
        self.assertEqual(
            service.check([PermissionRequest("write", "notes/demo.txt")], CONTEXT).action,
            "allow",
        )
        self.assertEqual(
            service.check(
                [PermissionRequest("write", "notes/private/key.txt")], CONTEXT
            ).action,
            "deny",
        )

    def test_permission_and_target_must_both_match(self):
        service = RulePermissionService(
            [
                PermissionRule("read", "notes/*", "allow"),
                PermissionRule("write", "*", "ask"),
            ]
        )

        decision = service.check(
            [PermissionRequest("write", "notes/demo.txt")], CONTEXT
        )

        self.assertEqual(decision.action, "ask")

    def test_missing_rule_defaults_to_ask(self):
        service = RulePermissionService([])

        decision = service.check([PermissionRequest("new_tool")], CONTEXT)

        self.assertEqual(decision.action, "ask")

    def test_multiple_requests_use_deny_then_ask_then_allow_precedence(self):
        service = RulePermissionService(
            [
                PermissionRule("read", "*", "allow"),
                PermissionRule("write", "*", "ask"),
                PermissionRule("execute", "*", "deny"),
            ]
        )

        self.assertEqual(
            service.check(
                [PermissionRequest("read"), PermissionRequest("write")], CONTEXT
            ).action,
            "ask",
        )
        self.assertEqual(
            service.check(
                [
                    PermissionRequest("read"),
                    PermissionRequest("write"),
                    PermissionRequest("execute"),
                ],
                CONTEXT,
            ).action,
            "deny",
        )

    def test_empty_request_list_is_denied(self):
        service = RulePermissionService([])

        self.assertEqual(service.check([], CONTEXT).action, "deny")

    def test_path_separators_are_normalized(self):
        service = RulePermissionService(
            [PermissionRule("read", "notes/*", "allow")]
        )

        decision = service.check(
            [PermissionRequest("read", r"notes\demo.txt")], CONTEXT
        )

        self.assertEqual(decision.action, "allow")

    def test_default_config_preserves_current_tool_behavior(self):
        service = create_default_services().permission

        self.assertEqual(
            service.check([PermissionRequest("read", "notes/a.txt")], CONTEXT).action,
            "allow",
        )
        self.assertEqual(
            service.check([PermissionRequest("write", "notes/a.txt")], CONTEXT).action,
            "ask",
        )
        self.assertEqual(
            service.check([PermissionRequest("calculate")], CONTEXT).action,
            "ask",
        )
        self.assertEqual(
            service.check([PermissionRequest("web_search")], CONTEXT).action,
            "allow",
        )
        self.assertEqual(
            service.check(
                [PermissionRequest("write", "notes/permission-deny-demo.txt")],
                CONTEXT,
            ).action,
            "deny",
        )

    def test_default_permission_file_is_resolved_from_server_directory(self):
        self.assertEqual(DEFAULT_PERMISSION_PATH.name, "permission.json")
        self.assertTrue(DEFAULT_PERMISSION_PATH.is_file())

    def test_config_is_flattened_in_json_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "permission.json"
            path.write_text(
                json.dumps(
                    {
                        "*": "ask",
                        "write": {"*": "ask", "notes/private/*": "deny"},
                    }
                ),
                encoding="utf-8",
            )

            rules = load_permission_rules(path)

        self.assertEqual(
            rules,
            (
                PermissionRule("*", "*", "ask", "permission.json"),
                PermissionRule("write", "*", "ask", "permission.json"),
                PermissionRule(
                    "write", "notes/private/*", "deny", "permission.json"
                ),
            ),
        )

    def test_invalid_json_and_actions_fail_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "permission.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "无法加载权限配置"):
                load_permission_rules(path)

            path.write_text('{"read": "sometimes"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "allow、ask 或 deny"):
                load_permission_rules(path)

    def test_file_tools_describe_normalized_paths(self):
        read = describe_tool_permissions("read_file", {"path": "tools/calculate.py"})
        write = describe_tool_permissions(
            "write_file", {"path": "notes/demo.txt", "content": "new"}
        )

        self.assertEqual(read, (PermissionRequest("read", "tools/calculate.py"),))
        self.assertEqual(write, (PermissionRequest("write", "notes/demo.txt"),))
        self.assertEqual(
            describe_tool_permissions("calculate", {"expression": "1 + 2"}),
            (PermissionRequest("calculate", "1 + 2"),),
        )

    def test_unknown_tool_is_rejected_before_authorization(self):
        with self.assertRaisesRegex(ValueError, "未知工具"):
            describe_tool_permissions("missing_tool", {})


if __name__ == "__main__":
    unittest.main()
