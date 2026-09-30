import os
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("API_KEY", "test-key")

from database import Database  # noqa: E402
from documents.cleanup import cleanup_files  # noqa: E402


class FileCleanupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.root = self.base / "files"
        self.root.mkdir()
        self.database = Database(self.base / "test.sqlite3")
        self.database.initialize()
        self.now = time.time()

    def tearDown(self):
        self.directory.cleanup()

    def create_file(self, *, age_seconds: int, referenced: bool = False) -> tuple[str, Path]:
        file_id = f"file_{uuid4().hex}"
        folder = self.root / file_id
        folder.mkdir()
        (folder / "original.pdf").write_bytes(b"%PDF-test")
        self.database.create_file(
            {
                "file_id": file_id,
                "filename": "test.pdf",
                "content_type": "application/pdf",
                "size": 9,
                "storage_path": f"{file_id}/original.pdf",
                "created_at": self.now - age_seconds,
            }
        )
        if referenced:
            session_id = str(uuid4())
            self.database.create_user_turn(
                session_id=session_id,
                parent_message_id=None,
                content="read this",
                file_ids=[file_id],
            )
        return file_id, folder

    def test_expired_unreferenced_file_is_removed(self):
        file_id, folder = self.create_file(age_seconds=25 * 60 * 60)
        result = cleanup_files(database=self.database, root=self.root, now=self.now)
        self.assertEqual(result["unreferenced_files_removed"], 1)
        self.assertIsNone(self.database.get_file(file_id))
        self.assertFalse(folder.exists())

    def test_recent_or_referenced_files_are_kept(self):
        recent_id, recent_folder = self.create_file(age_seconds=60)
        referenced_id, referenced_folder = self.create_file(
            age_seconds=25 * 60 * 60, referenced=True
        )
        cleanup_files(database=self.database, root=self.root, now=self.now)
        self.assertIsNotNone(self.database.get_file(recent_id))
        self.assertTrue(recent_folder.exists())
        self.assertIsNotNone(self.database.get_file(referenced_id))
        self.assertTrue(referenced_folder.exists())

    def test_stale_temporary_file_and_old_orphan_are_removed(self):
        temp_id = f"file_{uuid4().hex}"
        temp_folder = self.root / temp_id
        temp_folder.mkdir()
        temporary = temp_folder / "original.pdf.uploading"
        temporary.write_bytes(b"partial")
        old = self.now - 25 * 60 * 60
        os.utime(temporary, (old, old))
        os.utime(temp_folder, (old, old))

        orphan_id = f"file_{uuid4().hex}"
        orphan = self.root / orphan_id
        orphan.mkdir()
        (orphan / "original.pdf").write_bytes(b"%PDF-orphan")
        os.utime(orphan, (old, old))

        result = cleanup_files(database=self.database, root=self.root, now=self.now)
        self.assertEqual(result["temporary_files_removed"], 1)
        self.assertEqual(result["orphan_directories_removed"], 1)
        self.assertFalse(orphan.exists())
        self.assertTrue(temp_folder.exists())
        self.assertFalse(temporary.exists())

    def test_recent_temporary_and_symlink_are_not_followed(self):
        file_id = f"file_{uuid4().hex}"
        folder = self.root / file_id
        folder.mkdir()
        temporary = folder / "original.pdf.uploading"
        temporary.write_bytes(b"partial")

        outside = self.base / "outside"
        outside.mkdir()
        secret = outside / "keep.txt"
        secret.write_text("keep")
        link_id = f"file_{uuid4().hex}"
        (self.root / link_id).symlink_to(outside, target_is_directory=True)

        cleanup_files(database=self.database, root=self.root, now=self.now)
        self.assertTrue(temporary.exists())
        self.assertTrue(secret.exists())
        self.assertTrue((self.root / link_id).is_symlink())


if __name__ == "__main__":
    unittest.main()
