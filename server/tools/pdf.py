"""parse_attached_document：解析当前会话的 PDF 附件，按页提取文本。"""

import json
import re
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .context import SessionContext

# 硬限制：防止模型把超大 PDF 全文灌进上下文
MAX_PAGES = 50
MAX_PAGE_CHARS = 8000
MAX_TOTAL_CHARS = 200_000

# 与 documents.cleanup 保持一致的 file_id 形状；解析前再校验一次，
# 避免数据库被污染后把路径拼接变成目录穿越
FILE_ID_PATTERN = re.compile(r"^file_[0-9a-f]{32}$")

SCHEMA = {
    "type": "function",
    "name": "parse_attached_document",
    "description": (
        "解析当前会话上传的 PDF 附件，按页提取文本并保存为 pages.json，"
        "返回总页数与各页字符数。回答文档相关问题前应先调用本工具"
    ),
    "parameters": {"type": "object", "properties": {}, "required": [], "strict": False},
}


def _read_pages(reader: PdfReader) -> tuple[list[dict], bool]:
    """按硬限制提取文本；返回写入 pages.json 的页列表与是否发生截断。"""
    total_pages = len(reader.pages)
    pages: list[dict] = []
    remaining = MAX_TOTAL_CHARS
    truncated = total_pages > MAX_PAGES

    for index in range(min(total_pages, MAX_PAGES)):
        text = reader.pages[index].extract_text() or ""
        page_truncated = False
        if len(text) > MAX_PAGE_CHARS:
            text = text[:MAX_PAGE_CHARS]
            page_truncated = True
        if len(text) > remaining:
            text = text[:remaining]
            page_truncated = True
        remaining -= len(text)
        pages.append(
            {
                "page": index + 1,
                "text": text,
                "chars": len(text),
                "truncated": page_truncated,
            }
        )
        truncated = truncated or page_truncated
        if remaining <= 0:
            break
    return pages, truncated


def run(args: dict, context: SessionContext | None = None) -> str:
    if context is None:
        raise ValueError("缺少会话上下文，无法定位附件；请通过正常聊天流程调用")

    records = context.database.list_session_files(context.session_id)
    if not records:
        return (
            "当前会话没有关联的上传附件，无法解析。"
            "请让用户先上传 PDF，或直接回答问题。"
        )

    record = records[0]
    note = ""
    if len(records) > 1:
        note = (
            f"会话共关联 {len(records)} 个附件，当前仅支持解析最近上传的一个"
            f"（{record['filename']}），其余附件暂不支持。\n"
        )

    file_id = record["id"]
    if FILE_ID_PATTERN.fullmatch(file_id) is None:
        raise ValueError(f"附件 file_id 非法，拒绝解析：{file_id}")
    directory = context.file_root / file_id
    if directory.parent.resolve() != context.file_root.resolve():
        raise ValueError(f"附件目录越界，拒绝解析：{file_id}")

    pdf_path = directory / "original.pdf"
    if not pdf_path.is_file():
        raise FileNotFoundError(f"附件文件在磁盘上不存在：{record['filename']}")

    try:
        reader = PdfReader(BytesIO(pdf_path.read_bytes()))
        pages, truncated = _read_pages(reader)
    except PdfReadError as error:
        raise ValueError(
            f"PDF 解析失败（{record['filename']}）：文件损坏或格式不合法"
        ) from error

    payload = {
        "file_id": file_id,
        "filename": record["filename"],
        "total_pages": len(reader.pages),
        "pages": pages,
    }
    temporary = directory / "pages.json.parsing"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(directory / "pages.json")

    summary = "；".join(
        f"第{page['page']}页 {page['chars']} 字符" for page in pages
    )
    truncation_note = (
        "截断：无"
        if not truncated
        else (
            "截断：已触发（"
            f"页数上限 {MAX_PAGES}、单页上限 {MAX_PAGE_CHARS} 字符、"
            f"总字符上限 {MAX_TOTAL_CHARS}，超限部分未提取）"
        )
    )
    return (
        f"{note}已解析附件「{record['filename']}」（{file_id}）："
        f"PDF 共 {len(reader.pages)} 页，提取 {len(pages)} 页，"
        f"结果已写入 pages.json。\n各页字符数：{summary}。\n{truncation_note}。"
    )
