"""小型 PDF 的隔离上传；SQLite 是上传元数据的唯一事实来源。"""

import time
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from database import Database

FILE_ROOT = Path(__file__).resolve().parents[1] / ".data" / "files"
MAX_FILE_BYTES = 10 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024


class FileUploadError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


async def save_pdf(
    upload: UploadFile,
    *,
    database: Database,
    root: Path = FILE_ROOT,
    max_bytes: int = MAX_FILE_BYTES,
) -> dict:
    """使用服务端 file_id 保存 PDF，任一步失败都清理本次目录。"""
    filename = (upload.filename or "").replace("\\", "/").split("/")[-1]
    if not filename or filename in {".", ".."}:
        raise FileUploadError("missing_filename", "缺少文件名")
    if Path(filename).suffix.lower() != ".pdf":
        raise FileUploadError("unsupported_file_type", "只允许上传 PDF 文件")
    if upload.content_type != "application/pdf":
        raise FileUploadError("unsupported_file_type", "文件类型必须是 application/pdf")

    root.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        file_id = f"file_{uuid4().hex}"
        directory = root / file_id
        try:
            directory.mkdir()
            break
        except FileExistsError:
            continue
    else:
        raise FileUploadError("storage_failed", "无法生成文件 ID", 500)

    temporary = directory / "original.pdf.uploading"
    final = directory / "original.pdf"
    size = 0
    try:
        with temporary.open("xb") as output:
            first = True
            while chunk := await upload.read(READ_CHUNK_BYTES):
                size += len(chunk)
                if size > max_bytes:
                    raise FileUploadError("file_too_large", "PDF 不能超过 10 MiB", 413)
                if first:
                    first = False
                    if not chunk.startswith(b"%PDF-"):
                        raise FileUploadError("invalid_pdf_header", "文件内容不是 PDF")
                output.write(chunk)
        if not size:
            raise FileUploadError("empty_file", "PDF 文件不能为空")

        temporary.replace(final)
        created_at = time.time()
        database.create_file(
            {
                "file_id": file_id,
                "filename": filename,
                "content_type": "application/pdf",
                "size": size,
                "storage_path": f"{file_id}/original.pdf",
                "created_at": created_at,
            }
        )
        return {
            "file_id": file_id,
            "filename": filename,
            "content_type": "application/pdf",
            "size": size,
            "status": "uploaded",
            "created_at": created_at,
        }
    except BaseException:
        temporary.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        directory.rmdir()
        raise
    finally:
        await upload.close()
