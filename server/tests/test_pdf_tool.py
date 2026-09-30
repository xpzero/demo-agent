"""parse_attached_document 工具测试：解析、截断与错误路径。

PDF fixture 全部在测试内手写构造（最小合法 PDF 字节串），
不依赖网络下载。
"""

import json
import os
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("API_KEY", "test-key")

from database import Database  # noqa: E402
from tools import execute_tool, pdf  # noqa: E402
from tools.context import SessionContext  # noqa: E402


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(texts: list[str]) -> bytes:
    """构造每页一段文本的最小合法 PDF（xref 精确到字节偏移）。"""
    page_count = len(texts)
    font_id = 3 + 2 * page_count
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(page_count))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode(),
    ]
    for i, text in enumerate(texts):
        page_id = 3 + 2 * i
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {page_id + 1} 0 R "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>"
            ).encode()
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({_escape(text)}) Tj ET".encode()
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


class ParseAttachedDocumentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "files"
        self.root.mkdir()
        self.database = Database(Path(self.directory.name) / "test.sqlite3")
        self.database.initialize()
        self.session_id = str(uuid4())
        self.context = SessionContext(
            session_id=self.session_id, database=self.database, file_root=self.root
        )
        self.parent: int | None = None

    def tearDown(self):
        self.directory.cleanup()

    def attach_pdf(self, data: bytes, filename: str = "doc.pdf") -> str:
        """落盘一个 PDF、建库记录并挂到当前会话，返回 file_id。"""
        file_id = f"file_{uuid4().hex}"
        folder = self.root / file_id
        folder.mkdir()
        (folder / "original.pdf").write_bytes(data)
        self.database.create_file(
            {
                "file_id": file_id,
                "filename": filename,
                "content_type": "application/pdf",
                "size": len(data),
                "storage_path": f"{file_id}/original.pdf",
                "created_at": time.time(),
            }
        )
        self.parent = self.database.create_user_turn(
            session_id=self.session_id,
            parent_message_id=self.parent,
            content="please read this",
            file_ids=[file_id],
        )
        return file_id

    def read_pages_json(self, file_id: str) -> dict:
        return json.loads(
            (self.root / file_id / "pages.json").read_text(encoding="utf-8")
        )

    def test_parse_without_arguments_writes_pages_json(self):
        file_id = self.attach_pdf(build_pdf(["hello world", "second page"]))

        result = pdf.run({}, self.context)

        self.assertIn("共 2 页", result)
        self.assertIn("第1页 11 字符", result)
        self.assertIn("第2页 11 字符", result)
        self.assertIn("截断：无", result)
        payload = self.read_pages_json(file_id)
        self.assertEqual(payload["file_id"], file_id)
        self.assertEqual(payload["filename"], "doc.pdf")
        self.assertEqual(payload["total_pages"], 2)
        self.assertEqual(
            payload["pages"],
            [
                {"page": 1, "text": "hello world", "chars": 11, "truncated": False},
                {"page": 2, "text": "second page", "chars": 11, "truncated": False},
            ],
        )
        self.assertFalse((self.root / file_id / "pages.json.parsing").exists())

    def test_page_count_over_limit_truncates(self):
        file_id = self.attach_pdf(build_pdf(["one", "two", "three"]))

        with unittest.mock.patch.object(pdf, "MAX_PAGES", 2):
            result = pdf.run({}, self.context)

        self.assertIn("PDF 共 3 页，提取 2 页", result)
        self.assertIn("截断：已触发", result)
        payload = self.read_pages_json(file_id)
        self.assertEqual(payload["total_pages"], 3)
        self.assertEqual([page["page"] for page in payload["pages"]], [1, 2])

    def test_page_chars_over_limit_truncates(self):
        file_id = self.attach_pdf(build_pdf(["A" * 9000]))

        result = pdf.run({}, self.context)

        payload = self.read_pages_json(file_id)
        self.assertEqual(payload["pages"][0]["chars"], 8000)
        self.assertTrue(payload["pages"][0]["truncated"])
        self.assertIn("截断：已触发", result)

    def test_total_chars_over_limit_truncates(self):
        file_id = self.attach_pdf(build_pdf(["B" * 4000, "C" * 4000, "D" * 4000]))

        with unittest.mock.patch.object(pdf, "MAX_TOTAL_CHARS", 10_000):
            result = pdf.run({}, self.context)

        payload = self.read_pages_json(file_id)
        total = sum(page["chars"] for page in payload["pages"])
        self.assertEqual(total, 10_000)
        self.assertEqual(payload["pages"][2]["chars"], 2000)
        self.assertTrue(payload["pages"][2]["truncated"])
        self.assertIn("截断：已触发", result)

    def test_session_without_attachment_returns_readable_error(self):
        result = pdf.run({}, self.context)

        self.assertIn("没有关联的上传附件", result)

    def test_missing_context_is_rejected(self):
        output, ok = execute_tool("parse_attached_document", {})

        self.assertFalse(ok)
        self.assertIn("执行出错", output)
        self.assertIn("缺少会话上下文", output)

    def test_corrupt_pdf_becomes_readable_tool_error(self):
        self.attach_pdf(b"%PDF-1.4\nnot a real pdf body")

        output, ok = execute_tool(
            "parse_attached_document", {}, self.context
        )

        self.assertFalse(ok)
        self.assertIn("执行出错", output)
        self.assertIn("文件损坏或格式不合法", output)

    def test_multiple_attachments_parse_most_recent_with_note(self):
        self.attach_pdf(build_pdf(["old doc"]), filename="old.pdf")
        latest_id = self.attach_pdf(build_pdf(["new doc"]), filename="new.pdf")

        result = pdf.run({}, self.context)

        self.assertIn("共关联 2 个附件", result)
        self.assertIn("new.pdf", result)
        payload = self.read_pages_json(latest_id)
        self.assertEqual(payload["file_id"], latest_id)
        self.assertEqual(payload["pages"][0]["text"], "new doc")


if __name__ == "__main__":
    unittest.main()
