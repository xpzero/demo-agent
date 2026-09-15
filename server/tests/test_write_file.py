import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.files import paths, write


class WriteFileTests(unittest.TestCase):
    def test_run_creates_parent_directories_and_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "notes" / "demo.txt"

            with patch.object(write, "ROOT", root), patch.object(
                write, "resolve", side_effect=lambda path: (root / path).resolve()
            ):
                result = write.run({"path": "notes/demo.txt", "content": "new\n"})

            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
            self.assertIn("已写入 notes/demo.txt", result)

    def test_run_overwrites_existing_file_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "demo.txt"
            target.write_text("old\n", encoding="utf-8")

            with patch.object(write, "ROOT", root), patch.object(
                write, "resolve", side_effect=lambda path: (root / path).resolve()
            ):
                write.run({"path": "demo.txt", "content": "new\n"})

            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_run_rejects_non_string_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()

            with patch.object(write, "ROOT", root), patch.object(
                write, "resolve", side_effect=lambda path: (root / path).resolve()
            ):
                with self.assertRaises(TypeError):
                    write.run({"path": "demo.txt", "content": 123})

    def test_session_data_directory_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(paths, "ROOT", root):
                with self.assertRaisesRegex(ValueError, "禁止访问"):
                    paths.resolve(".sessions/1.json")


if __name__ == "__main__":
    unittest.main()
