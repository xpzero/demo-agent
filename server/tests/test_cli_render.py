import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cli import render as render_module
from sessions import SessionStorageError


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
                    "reason": "test",
                },
                "decision": None,
                "outcome": None,
                "output": None,
                "preview": {
                    "type": "code_diff",
                    "path": "notes/demo.txt",
                    "additions": 1,
                    "deletions": 0,
                    "lines": [{"kind": "added", "text": "new"}],
                },
                "guard": None,
            }
        ],
    }


class CliRenderTests(unittest.TestCase):
    def test_restored_approval_shows_args_and_diff_before_prompt(self):
        printed = []
        save_pending = Mock()

        with (
            patch("builtins.input", return_value="n"),
            patch("builtins.print", side_effect=lambda *args, **_: printed.append(" ".join(map(str, args)))),
        ):
            render_module.render_events(
                [],
                SimpleNamespace(permission=Mock()),
                pending_approval=pending_batch(),
                on_pending=save_pending,
            )

        output = "\n".join(printed)
        self.assertIn("notes/demo.txt", output)
        self.assertIn("[拟议改动] notes/demo.txt +1 -0", output)
        self.assertEqual(save_pending.call_count, 1)
        self.assertIsNone(save_pending.call_args_list[-1].args[0])

    def test_failed_approval_checkpoint_never_prompts(self):
        ask = Mock()
        batch = pending_batch()

        def stream_events(*_, on_approval, **__):
            try:
                on_approval(batch)
            except Exception as error:
                yield {"type": "error", "message": str(error)}

        with (
            patch.object(render_module, "stream_events", stream_events),
            patch("builtins.input", ask),
            patch("builtins.print"),
        ):
            render_module.render_events(
                [],
                SimpleNamespace(permission=Mock()),
                on_pending=Mock(side_effect=SessionStorageError("unavailable")),
            )

        ask.assert_not_called()


if __name__ == "__main__":
    unittest.main()
