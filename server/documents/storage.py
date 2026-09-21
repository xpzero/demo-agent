"""小型文本 PDF 的上传保存；解析与索引留给后续阶段。"""

import json
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

DOCUMENT_ROOT = Path(__file__).resolve().parents[1] / ".data" / "documents"
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024


class DocumentUploadError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


async def save_pdf(
    upload: UploadFile,
    *,
    root: Path = DOCUMENT_ROOT,
    max_bytes: int = MAX_DOCUMENT_BYTES,
) -> dict:
    """使用服务端 ID 存储，任何失败均清理本次新建目录。"""
    filename = (upload.filename or "").replace("\\", "/").split("/")[-1]
    if not filename or filename in {".", ".."}:
        raise DocumentUploadError("missing_filename", "缺少文件名")
    if Path(filename).suffix.lower() != ".pdf":
        raise DocumentUploadError("unsupported_file_type", "只允许上传 PDF 文件")
    if upload.content_type != "application/pdf":
        raise DocumentUploadError("unsupported_file_type", "文件类型必须是 application/pdf")

    # mkdir(exist_ok=False) 让随机 ID 的意外碰撞也不能覆盖原文件。
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        document_id = f"doc_{uuid4().hex}"
        directory = root / document_id
        try:
            directory.mkdir()
            break
        except FileExistsError:
            continue
    else:
        raise DocumentUploadError("storage_failed", "无法生成文档 ID", 500)

    temporary = directory / "original.pdf.uploading"
    final = directory / "original.pdf"
    metadata_file = directory / "metadata.json"
    size = 0
    try:
        with temporary.open("xb") as output:
            first = True
            while chunk := await upload.read(READ_CHUNK_BYTES):
                size += len(chunk)
                if size > max_bytes:
                    raise DocumentUploadError("file_too_large", "PDF 不能超过 10 MiB", 413)
                if first:
                    first = False
                    if not chunk.startswith(b"%PDF-"):
                        raise DocumentUploadError("invalid_pdf_header", "文件内容不是 PDF")
                output.write(chunk)
        if not size:
            raise DocumentUploadError("empty_file", "PDF 文件不能为空")

        temporary.replace(final)
        metadata = {
            "document_id": document_id,
            "filename": filename,
            "content_type": "application/pdf",
            "size": size,
            "status": "uploaded",
        }
        metadata_file.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        return metadata
    except BaseException:
        temporary.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        metadata_file.unlink(missing_ok=True)
        directory.rmdir()
        raise
    finally:
        await upload.close()
