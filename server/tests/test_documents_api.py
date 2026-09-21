import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("API_KEY", "test-key")

import api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class DocumentUploadApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.root_patch = patch.object(api, "DOCUMENT_ROOT", self.root)
        self.root_patch.start()
        self.client = TestClient(api.app)

    def tearDown(self):
        self.root_patch.stop()
        self.directory.cleanup()

    def upload(self, name, data, content_type="application/pdf"):
        return self.client.post(
            "/api/documents", files={"file": (name, data, content_type)}
        )

    def test_success_and_path_is_generated_by_server(self):
        data = b"%PDF-1.4\nexample bytes"
        response = self.upload("../../report.pdf", data)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["filename"], "report.pdf")
        self.assertEqual(body["size"], len(data))
        self.assertEqual(body["status"], "uploaded")
        folder = self.root / body["document_id"]
        self.assertEqual((folder / "original.pdf").read_bytes(), data)
        self.assertEqual(json.loads((folder / "metadata.json").read_text()), body)
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_same_name_does_not_overwrite(self):
        a = self.upload("report.pdf", b"%PDF-one").json()
        b = self.upload("report.pdf", b"%PDF-two").json()
        self.assertNotEqual(a["document_id"], b["document_id"])
        self.assertEqual((self.root / a["document_id"] / "original.pdf").read_bytes(), b"%PDF-one")
        self.assertEqual((self.root / b["document_id"] / "original.pdf").read_bytes(), b"%PDF-two")

    def test_extension_and_mime_are_rejected(self):
        response = self.upload("notes.txt", b"%PDF-1.4")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "unsupported_file_type")
        response = self.upload("notes.pdf", b"%PDF-1.4", "text/plain")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_header_and_empty_file_are_rejected_without_remnants(self):
        response = self.upload("fake.pdf", b"not a pdf")
        self.assertEqual(response.json()["detail"]["code"], "invalid_pdf_header")
        response = self.upload("empty.pdf", b"")
        self.assertEqual(response.json()["detail"]["code"], "empty_file")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_oversize_is_rejected_without_remnants(self):
        with patch.object(api, "MAX_DOCUMENT_BYTES", 8):
            response = self.upload("big.pdf", b"%PDF-123456")
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["detail"]["code"], "file_too_large")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_file_is_rejected(self):
        response = self.client.post("/api/documents")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
