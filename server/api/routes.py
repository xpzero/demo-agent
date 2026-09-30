"""HTTP 路由：documents / sessions / chat（stats 接口将来也落这里）。"""

from contextlib import asynccontextmanager
from queue import Queue
from threading import Event, Thread, Timer
import time

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent import SYSTEM_PROMPT
from agent.context_budget import build_context
from agent.loop import MAX_RUN_SECONDS
from database import StoreError
from documents import MAX_FILE_BYTES, FileUploadError, save_pdf
from documents.cleanup import cleanup_files
from tools.context import SessionContext  # noqa: F401  re-export for readability

from .chat_stream import chat_sse_stream
from .event_buffer import buffers
from .resume_stream import resume_response, session_history, watch_terminal
from . import deps
from .deps import _lock, _running_sessions, get_database
from recovery.reconciler import start_reconciliation


@asynccontextmanager
async def lifespan(_app: FastAPI):
    database = get_database()
    database.recover_orphan_runs()
    # Restarted work remains attached to its original Run and Session. Without
    # a business adapter we cannot assert completion or resubmit the operation;
    # honor the original persisted deadline and preserve unknown evidence.
    orphan_timers = []
    adapters = getattr(app.state, "business_adapters", None)
    for run in database.list_orphan_runs():
        # The startup claim already moved lost running Runs to finishing;
        # this worker keeps querying original operations until reliable facts
        # or the original deadline release the Session slot.
        start_reconciliation(database, run["id"], adapters)
    cleanup_files(database=database, root=deps.FILE_ROOT)
    try:
        yield
    finally:
        for timer in orphan_timers:
            timer.cancel()


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
    return session_history(database, session_id)


@app.get("/api/sessions/{session_id}/resume")
def resume_session(session_id: str, request: Request, last_event_id: str | None = Header(default=None)):
    database = get_database()
    try:
        session_id = database.normalize_session_id(session_id)
        return resume_response(database, session_id, request, last_event_id)
    except StoreError as error:
        raise _http_error(error) from error


@app.post("/api/sessions/{session_id}/stop")
def stop_session(session_id: str):
    database = get_database()
    try:
        session_id = database.normalize_session_id(session_id)
        # 定位与停止标记由数据库在一个事务里完成；等待只追踪这一次 Run。
        result = database.request_stop(session_id)
        run_id = result.get("run_id")
        if run_id is not None and result["result"] == "processing":
            end = time.monotonic() + 2
            while time.monotonic() < end:
                current = database.get_active_run(session_id)
                if current is None or current["id"] != run_id:
                    return {"result": "ended"}
                time.sleep(0.05)
        return {"result": result["result"]}
    except StoreError as error:
        raise _http_error(error) from error


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

    try:
        with buffers.condition:
            user_message_id, run_id = database.create_run_turn(
                session_id=session_id,
                parent_message_id=body.parent_message_id,
                content=body.message,
                file_ids=body.ref_file_ids,
                deadline_at=time.time() + MAX_RUN_SECONDS,
            )
            event_buffer = buffers.register(database, run_id, user_message_id)
        watch_terminal(database, run_id, event_buffer)
        with _lock:
            _running_sessions.add(session_id)
        chain = database.get_message_chain(session_id, user_message_id)
    except Exception as error:
        if "run_id" in locals():
            database.finish_run(run_id, "failed", reason="context_setup_failed")
            event_buffer.finish_producing()
            buffers.terminal(database, run_id)
        with _lock:
            _running_sessions.discard(session_id)
        if isinstance(error, StoreError):
            if error.code == "active_run_exists":
                raise HTTPException(status_code=409, detail={"code": "session_busy", "message": "当前会话正在处理"}) from error
            raise _http_error(error) from error
        raise

    # 上下文预算控制在 build_context 内：超预算丢最旧历史，
    # system 永在；库里仍存全量原文（存储 ≠ 上下文）
    try:
        session_files = database.list_session_files(session_id)
        system_prompt = SYSTEM_PROMPT
        if session_files:
            system_prompt = f"{SYSTEM_PROMPT}\n{ATTACHED_DOCUMENT_RULE}"
        summary_state = database.get_session_summary(session_id)
        summary, summary_cursor = summary_state if summary_state is not None else (None, None)
        items = build_context(
            system_prompt, chain, summary=summary,
            summary_upto_message_id=summary_cursor,
        )
    except Exception:
        database.finish_run(run_id, "failed", reason="context_setup_failed")
        event_buffer.finish_producing()
        buffers.terminal(database, run_id)
        with _lock:
            _running_sessions.discard(session_id)
        raise

    # 工作线程独立于 SSE 连接。浏览器断连只关闭消费端，不取消后台 Run。
    frames: Queue[str | None] = Queue()
    disconnected = Event()

    def release_session():
        with _lock:
            _running_sessions.discard(session_id)

    def produce():
        try:
            for frame in chat_sse_stream(
                database=database, session_id=session_id,
                user_message_id=user_message_id, run_id=run_id,
                items=items, file_root=deps.FILE_ROOT,
                release_session=release_session,
                business_adapters=getattr(app.state, "business_adapters", None),
            ):
                frame = event_buffer.append(frame)
                if not disconnected.is_set():
                    frames.put(frame)
        except Exception:
            database.finish_run(run_id, "failed", reason="worker_crashed")
            release_session()
        finally:
            event_buffer.finish_producing()
            if not disconnected.is_set():
                frames.put(None)

    def consume():
        try:
            while True:
                frame = frames.get()
                if frame is None:
                    return
                yield frame
        finally:
            disconnected.set()

    Thread(target=produce, name=f"agent-run-{run_id}", daemon=True).start()
    return StreamingResponse(consume(), media_type="text/event-stream")
