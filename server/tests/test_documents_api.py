import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("API_KEY", "test-key")

import api  # noqa: E402
from database import Database  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class DocumentUploadApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "files"
        self.database_path = Path(self.directory.name) / "test.sqlite3"
        self.patches = [
            patch.object(api, "FILE_ROOT", self.root),
            patch.object(api, "DATABASE_PATH", self.database_path),
        ]
        for item in self.patches:
            item.start()
        self.client = TestClient(api.app)
        self.database = Database(self.database_path)
        self.database.initialize()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def upload(self, name, data, content_type="application/pdf"):
        return self.client.post(
            "/api/documents", files={"file": (name, data, content_type)}
        )

    def test_success_is_recorded_in_sqlite_without_metadata_file(self):
        data = b"%PDF-1.4\nexample bytes"
        response = self.upload("../../report.pdf", data)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["filename"], "report.pdf")
        self.assertEqual(body["size"], len(data))
        self.assertEqual(body["status"], "uploaded")
        self.assertTrue(body["file_id"].startswith("file_"))
        folder = self.root / body["file_id"]
        self.assertEqual((folder / "original.pdf").read_bytes(), data)
        self.assertFalse((folder / "metadata.json").exists())
        record = self.database.get_file(body["file_id"])
        self.assertEqual(record["filename"], "report.pdf")
        self.assertEqual(record["storage_path"], f"{body['file_id']}/original.pdf")

    def test_same_name_does_not_overwrite(self):
        a = self.upload("report.pdf", b"%PDF-one").json()
        b = self.upload("report.pdf", b"%PDF-two").json()
        self.assertNotEqual(a["file_id"], b["file_id"])
        self.assertEqual((self.root / a["file_id"] / "original.pdf").read_bytes(), b"%PDF-one")
        self.assertEqual((self.root / b["file_id"] / "original.pdf").read_bytes(), b"%PDF-two")

    def test_extension_and_mime_are_rejected(self):
        response = self.upload("notes.txt", b"%PDF-1.4")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "unsupported_file_type")
        response = self.upload("notes.pdf", b"%PDF-1.4", "text/plain")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(list(self.root.iterdir()) if self.root.exists() else [], [])

    def test_header_empty_and_oversize_leave_no_remnants(self):
        self.assertEqual(
            self.upload("fake.pdf", b"not a pdf").json()["detail"]["code"],
            "invalid_pdf_header",
        )
        self.assertEqual(
            self.upload("empty.pdf", b"").json()["detail"]["code"],
            "empty_file",
        )
        with patch.object(api, "MAX_FILE_BYTES", 8):
            response = self.upload("big.pdf", b"%PDF-123456")
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["detail"]["code"], "file_too_large")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_database_failure_removes_saved_file(self):
        with patch.object(Database, "create_file", side_effect=RuntimeError("db failed")):
            with self.assertRaises(RuntimeError):
                self.upload("report.pdf", b"%PDF-data")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_file_is_rejected(self):
        response = self.client.post("/api/documents")
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
