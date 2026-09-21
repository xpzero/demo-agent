"""上传文档的隔离存储。"""

from .storage import DOCUMENT_ROOT, MAX_DOCUMENT_BYTES, DocumentUploadError, save_pdf

__all__ = ["DOCUMENT_ROOT", "MAX_DOCUMENT_BYTES", "DocumentUploadError", "save_pdf"]
