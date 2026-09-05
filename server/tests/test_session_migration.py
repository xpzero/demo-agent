import json
import tempfile
import unittest
from pathlib import Path

from sessions.migrate_json import load_sessions


class SessionMigrationTests(unittest.TestCase):
    def test_load_sessions_preserves_ids_and_defaults_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            payload = {
                "id": 7,
                "items": [{"role": "system", "content": "system"}],
            }
            (source / "7.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )

            sessions = load_sessions(source)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].id, 7)
        self.assertEqual(sessions[0].revision, 0)

    def test_load_sessions_rejects_filename_payload_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "7.json").write_text(
                json.dumps({"id": 8, "items": []}), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "文件名与内容 id 不一致"):
                load_sessions(source)

    def test_load_sessions_rejects_non_standard_json_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "7.json").write_text(
                '{"id": 7, "items": [{"value": NaN}]}', encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "Session 文件无效"):
                load_sessions(source)


if __name__ == "__main__":
    unittest.main()
