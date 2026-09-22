import json
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from agent import SYSTEM_PROMPT, stream_events
from database import DATABASE_PATH as DEFAULT_DATABASE_PATH
from database import Database, StoreError
from documents import FILE_ROOT as DEFAULT_FILE_ROOT
from documents import MAX_FILE_BYTES, FileUploadError, save_pdf
from documents.cleanup import cleanup_files
from logging_setup import setup_logging

setup_logging()

DATABASE_PATH = DEFAULT_DATABASE_PATH
FILE_ROOT = DEFAULT_FILE_ROOT
_running_sessions: set[str] = set()
_lock = Lock()


def get_database() -> Database:
    database = Database(DATABASE_PATH)
    database.initialize()
    return database


@asynccontextmanager
async def lifespan(_app: FastAPI):
    database = get_database()
    cleanup_files(database=database, root=FILE_ROOT)
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


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


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
            root=FILE_ROOT,
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

    items = [{"role": "system", "content": SYSTEM_PROMPT}]
    items.extend(
        {"role": message["role"], "content": message["content"]}
        for message in chain
    )

    def release_session():
        with _lock:
            _running_sessions.discard(session_id)

    def sse():
        try:
            yield _sse({"type": "user_message", "message_id": user_message_id})
            terminated = False
            for event in stream_events(items):
                if event["type"] == "done":
                    content = event["content"]
                    message_id = user_message_id
                    if content:
                        try:
                            message_id = database.add_assistant_message(
                                session_id=session_id,
                                parent_message_id=user_message_id,
                                content=content,
                            )
                        except Exception as error:
                            yield _sse(
                                {
                                    "type": "error",
                                    "message": (
                                        "保存助手消息失败："
                                        f"{type(error).__name__}: {error}"
                                    ),
                                }
                            )
                            return
                    event = {**event, "message_id": message_id}
                yield _sse(event)
                if event["type"] in ("done", "error"):
                    terminated = True
                    break
            if not terminated:
                yield _sse({"type": "error", "message": "本轮回复未完成，请重试"})
        except Exception as error:
            yield _sse({"type": "error", "message": f"回复中断：{type(error).__name__}: {error}"})
        finally:
            release_session()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        background=BackgroundTask(release_session),
    )
