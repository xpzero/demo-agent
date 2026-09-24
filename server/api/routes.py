"""HTTP 路由：documents / sessions / chat（stats 接口将来也落这里）。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from agent import SYSTEM_PROMPT
from agent.context_budget import build_context
from database import StoreError
from documents import MAX_FILE_BYTES, FileUploadError, save_pdf
from documents.cleanup import cleanup_files
from tools.context import SessionContext  # noqa: F401  re-export for readability

from .chat_stream import chat_sse_stream
from . import deps
from .deps import _lock, _running_sessions, get_database


@asynccontextmanager
async def lifespan(_app: FastAPI):
    database = get_database()
    cleanup_files(database=database, root=deps.FILE_ROOT)
    yield


app = FastAPI(title="demo-agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    session_id: str
    parent_message_id: int | None = None
    message: str = Field(min_length=1, max_length=20_000)
    ref_file_ids: list[str] = Field(default_factory=list, max_length=1)


# 会话带有 PDF 附件时追加到系统提示的规则：引导模型先解析再回答，
# 而不是凭空猜测文档内容
ATTACHED_DOCUMENT_RULE = (
    "本会话带有 PDF 附件。回答与文档内容相关的问题前，"
    "必须先调用 parse_attached_document 工具解析文档，再基于解析结果回答。"
)


def _http_error(error: StoreError | FileUploadError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": str(error)},
    )


@app.post("/api/documents", status_code=201)
async def upload_file(file: UploadFile = File(...)):
    try:
        return await save_pdf(
            file,
            database=get_database(),
            root=deps.FILE_ROOT,
            max_bytes=MAX_FILE_BYTES,
        )
    except FileUploadError as error:
        raise _http_error(error) from error


@app.get("/api/sessions")
def list_sessions():
    return {"sessions": get_database().list_sessions()}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    database = get_database()
    try:
        session_id = database.normalize_session_id(session_id)
    except StoreError as error:
        raise _http_error(error) from error
    history = database.get_session_history(session_id)
    if history is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "session_not_found", "message": "会话不存在"},
        )
    return history


@app.get("/api/sessions/{session_id}/stats")
def get_session_stats(session_id: str):
    database = get_database()
    try:
        session_id = database.normalize_session_id(session_id)
    except StoreError as error:
        raise _http_error(error) from error
    try:
        return database.get_session_stats(session_id)
    except StoreError as error:
        raise _http_error(error) from error


@app.post("/api/chat")
def chat(body: ChatRequest):
    database = get_database()
    try:
        session_id = database.normalize_session_id(body.session_id)
    except StoreError as error:
        raise _http_error(error) from error

    with _lock:
        if session_id in _running_sessions:
            raise HTTPException(status_code=409, detail="当前会话已有任务正在运行")
        _running_sessions.add(session_id)

    try:
        user_message_id = database.create_user_turn(
            session_id=session_id,
            parent_message_id=body.parent_message_id,
            content=body.message,
            file_ids=body.ref_file_ids,
        )
        chain = database.get_message_chain(session_id, user_message_id)
    except Exception as error:
        with _lock:
            _running_sessions.discard(session_id)
        if isinstance(error, StoreError):
            raise _http_error(error) from error
        raise

    # 上下文预算控制在 build_context 内：超预算丢最旧历史，
    # system 永在；库里仍存全量原文（存储 ≠ 上下文）
    session_files = database.list_session_files(session_id)
    system_prompt = SYSTEM_PROMPT
    if session_files:
        system_prompt = f"{SYSTEM_PROMPT}\n{ATTACHED_DOCUMENT_RULE}"
    items = build_context(system_prompt, chain)

    def release_session():
        with _lock:
            _running_sessions.discard(session_id)

    # 编排细节（事件循环、done 落库、埋点落库）在 chat_stream；
    # 这里只负责请求校验、会话锁与 StreamingResponse 组装
    return StreamingResponse(
        chat_sse_stream(
            database=database,
            session_id=session_id,
            user_message_id=user_message_id,
            items=items,
            file_root=deps.FILE_ROOT,
            release_session=release_session,
        ),
        media_type="text/event-stream",
        background=BackgroundTask(release_session),
    )
