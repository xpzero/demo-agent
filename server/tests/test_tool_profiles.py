import unittest
from unittest.mock import patch

import tools


class ToolProfileTests(unittest.TestCase):
    def test_public_profile_excludes_dangerous_tools(self):
        schemas, handlers = tools.build_registry("public")
        names = {schema["name"] for schema in schemas}

        self.assertEqual(names, tools.PUBLIC_TOOL_NAMES)
        self.assertEqual(set(handlers), tools.PUBLIC_TOOL_NAMES)
        self.assertNotIn("calculate", names)
        self.assertNotIn("write_file", names)

    def test_public_profile_keeps_allowed_tool_callable(self):
        _, handlers = tools.build_registry("public")

        result = handlers["get_weather"]({"city": "北京"})

        self.assertEqual(result, "北京今天晴，最高气温38℃")

    def test_public_handlers_reject_forged_dangerous_call(self):
        _, handlers = tools.build_registry("public")

        with patch.dict(tools.TOOL_HANDLERS, handlers, clear=True):
            result = tools.execute_tool(
                "write_file", {"path": "unsafe.txt", "content": "x"}
            )

        self.assertEqual(result, "未知工具：write_file")

    def test_local_profile_keeps_teaching_tools(self):
        schemas, handlers = tools.build_registry("local")
        names = {schema["name"] for schema in schemas}

        self.assertIn("calculate", names)
        self.assertIn("write_file", names)
        self.assertIn("calculate", handlers)
        self.assertIn("write_file", handlers)

    def test_unknown_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "未知 TOOL_PROFILE"):
            tools.build_registry("typo")


if __name__ == "__main__":
    unittest.main()
