import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.files import paths, write


class WriteFilePreviewTests(unittest.TestCase):
    def test_preview_describes_change_without_writing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "notes" / "demo.txt"
            target.parent.mkdir()
            target.write_text("old\n", encoding="utf-8")

            with patch.object(write, "ROOT", root), patch.object(
                write, "resolve", side_effect=lambda path: (root / path).resolve()
            ):
                preview = write.preview(
                    {"path": "notes/demo.txt", "content": "new\n"}
                )

            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(preview["path"], "notes/demo.txt")
            self.assertEqual(preview["additions"], 1)
            self.assertEqual(preview["deletions"], 1)
            self.assertEqual(
                preview["lines"],
                [
                    {"kind": "removed", "text": "old"},
                    {"kind": "added", "text": "new"},
                ],
            )

    def test_approved_write_rejects_a_file_changed_after_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "notes" / "demo.txt"
            target.parent.mkdir()
            target.write_text("old\n", encoding="utf-8")

            with patch.object(write, "ROOT", root), patch.object(
                write, "resolve", side_effect=lambda path: (root / path).resolve()
            ):
                preview = write.preview(
                    {"path": "notes/demo.txt", "content": "approved\n"}
                )
                target.write_text("changed elsewhere\n", encoding="utf-8")

                with self.assertRaisesRegex(ValueError, "发生变化"):
                    write.run_approved(
                        {"path": "notes/demo.txt", "content": "approved\n"},
                        preview["_guard"],
                    )

            self.assertEqual(
                target.read_text(encoding="utf-8"), "changed elsewhere\n"
            )

    def test_approved_write_applies_the_previewed_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "notes" / "demo.txt"
            target.parent.mkdir()
            target.write_text("old\n", encoding="utf-8")
            args = {"path": "notes/demo.txt", "content": "approved\n"}

            with patch.object(write, "ROOT", root), patch.object(
                write, "resolve", side_effect=lambda path: (root / path).resolve()
            ):
                preview = write.preview(args)
                result = write.run_approved(args, preview["_guard"])

            self.assertEqual(target.read_text(encoding="utf-8"), "approved\n")
            self.assertIn("已写入 notes/demo.txt", result)

    def test_session_data_directory_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(paths, "ROOT", root):
                with self.assertRaisesRegex(ValueError, "禁止访问"):
                    paths.resolve(".sessions/1.json")


if __name__ == "__main__":
    unittest.main()
