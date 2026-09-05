import unittest
from unittest.mock import patch

from fakes import FakeSessionService
from sessions import handle_command


class SessionCommandTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeSessionService()
        self.current = self.service.create("system")

    def test_new_and_switch_update_only_cli_current(self):
        with patch("builtins.print"):
            created = handle_command("/new", self.service, self.current, "system")
            switched = handle_command(
                f"/switch {self.current.id}",
                self.service,
                created.current,
                "system",
            )

        self.assertTrue(created.handled)
        self.assertNotEqual(created.current.id, self.current.id)
        self.assertEqual(switched.current.id, self.current.id)

    def test_delete_removes_non_current_session(self):
        target = self.service.create("system")

        with patch("builtins.print"):
            result = handle_command(
                f"/del {target.id}", self.service, self.current, "system"
            )

        self.assertTrue(result.handled)
        self.assertEqual(result.current.id, self.current.id)
        self.assertIsNone(self.service.get(target.id))

    def test_non_command_is_not_consumed(self):
        result = handle_command("hello", self.service, self.current, "system")

        self.assertFalse(result.handled)
        self.assertIs(result.current, self.current)


if __name__ == "__main__":
    unittest.main()
