import json
from threading import Lock, RLock

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent import SYSTEM_PROMPT, stream_events
from agent.approval import (
    commit_outputs,
    decide,
    execute_approved_call,
    is_ready,
    pending_call_ids,
)
from logging_setup import setup_logging
from services import create_default_services
from sessions import SessionManager

# uvicorn 先配好自己的日志再导入本模块，放在这里设置才不会被它覆盖
setup_logging()

app = FastAPI(title="demo-agent")

# vite dev server 跑在 5173，跨端口即跨源，不放行浏览器会直接拦掉请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 单进程单例，与 CLI 共用同一套 .sessions/ 数据。
manager = SessionManager(SYSTEM_PROMPT)
services = create_default_services()
_state_lock = Lock()
_session_locks: dict[int, RLock] = {}
_running_sessions: dict[int, object] = {}


class ChatRequest(BaseModel):
    message: str


def _get_session(session_id: int):
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"没有 {session_id} 号会话")
    return session


def _get_session_lock(session_id: int) -> RLock:
    with _state_lock:
        return _session_locks.setdefault(session_id, RLock())


def _reserve_run(session_id: int) -> object:
    with _state_lock:
        if session_id in _running_sessions:
            raise HTTPException(status_code=409, detail="该会话已有任务正在运行")
        token = object()
        _running_sessions[session_id] = token
        return token


def _release_run(session_id: int, token: object) -> None:
    with _state_lock:
        if _running_sessions.get(session_id) is token:
            del _running_sessions[session_id]


def _is_running(session_id: int) -> bool:
    with _state_lock:
        return session_id in _running_sessions


def _sse_event(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class RunStreamingResponse(StreamingResponse):
    def __init__(self, content, *, session_id: int, run_token: object):
        super().__init__(content, media_type="text/event-stream")
        self._session_id = session_id
        self._run_token = run_token

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            _release_run(self._session_id, self._run_token)


def _public_pending(batch: dict | None) -> dict | None:
    if batch is None:
        return None

    calls = []
    for call in batch["calls"]:
        public = {
            key: value
            for key, value in call.items()
            if key != "guard"
        }
        calls.append(public)

    return {
        "remaining_turns": batch["remaining_turns"],
        "outputs_committed": batch["outputs_committed"],
        "calls": calls,
    }


@app.get("/api/sessions")
def list_sessions():
    return [
        {
            "id": session.id,
            "summary": session.summary,
            "message_count": len(session.items),
            "current": is_current,
        }
        for is_current, session in manager.listing()
    ]


@app.post("/api/sessions")
def create_session():
    session = manager.new()
    return {"id": session.id}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: int):
    session = _get_session(session_id)
    with _get_session_lock(session_id):
        return {
            "id": session.id,
            "summary": session.summary,
            "pending_approval": _public_pending(session.pending_approval),
        }


@app.post("/api/sessions/{session_id}/chat")
def chat(session_id: int, body: ChatRequest):
    session = _get_session(session_id)
    lock = _get_session_lock(session_id)
    with lock:
        if session.pending_approval is not None:
            raise HTTPException(status_code=409, detail="该会话仍有工具调用等待处理")
        run_token = _reserve_run(session_id)
        item_count = len(session.items)
        try:
            session.items.append({"role": "user", "content": body.message})
            manager.save(session)
        except Exception:
            del session.items[item_count:]
            _release_run(session_id, run_token)
            raise

    def checkpoint(batch: dict) -> None:
        with lock:
            session.pending_approval = batch
            manager.save(session)

    def sse():
        released = False
        try:
            for event in stream_events(
                session.items,
                services,
                on_approval=checkpoint,
                session_id=session.id,
            ):
                if event["type"] in {
                    "approval_required",
                    "done",
                    "max_turns",
                    "error",
                }:
                    with lock:
                        manager.save(session)
                    _release_run(session_id, run_token)
                    released = True
                yield _sse_event(event)
        finally:
            if not released:
                _release_run(session_id, run_token)

    return RunStreamingResponse(sse(), session_id=session_id, run_token=run_token)


def _record_decision(session_id: int, call_id: str, approved: bool):
    session = _get_session(session_id)
    if _is_running(session_id):
        raise HTTPException(status_code=409, detail="该会话仍在运行，请稍后重试")

    lock = _get_session_lock(session_id)
    with lock:
        batch = session.pending_approval
        if batch is None:
            raise HTTPException(status_code=409, detail="该会话没有待审批工具")
        try:
            call = decide(batch, call_id, approved)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"没有工具调用 {call_id}") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

        if approved:
            execute_approved_call(call, services.permission, session.id)

        manager.save(session)
        return {
            "call_id": call_id,
            "decision": call["decision"],
            "outcome": call.get("outcome"),
            "output": call["output"],
            "pending_count": len(pending_call_ids(batch)),
            "ready_to_resume": is_ready(batch),
        }


@app.post("/api/sessions/{session_id}/approvals/{call_id}/approve")
def approve_tool(session_id: int, call_id: str):
    return _record_decision(session_id, call_id, True)


@app.post("/api/sessions/{session_id}/approvals/{call_id}/reject")
def reject_tool(session_id: int, call_id: str):
    return _record_decision(session_id, call_id, False)


@app.post("/api/sessions/{session_id}/resume")
def resume(session_id: int):
    session = _get_session(session_id)
    lock = _get_session_lock(session_id)
    with lock:
        batch = session.pending_approval
        if batch is None:
            raise HTTPException(status_code=409, detail="该会话没有可以继续的审批任务")
        if not is_ready(batch):
            raise HTTPException(status_code=409, detail="仍有工具调用等待审批")
        run_token = _reserve_run(session_id)

    def checkpoint(next_batch: dict) -> None:
        with lock:
            session.pending_approval = next_batch
            manager.save(session)

    def sse():
        released = False
        try:
            with lock:
                results = commit_outputs(
                    session.items, batch, services.permission, session.id
                )
                manager.save(session)

            for result in results:
                yield _sse_event(result)

            for event in stream_events(
                session.items,
                services,
                max_turns=batch["remaining_turns"],
                on_approval=checkpoint,
                session_id=session.id,
            ):
                if event["type"] in {"done", "max_turns"}:
                    with lock:
                        if session.pending_approval is batch:
                            session.pending_approval = None
                        manager.save(session)
                    _release_run(session_id, run_token)
                    released = True
                elif event["type"] in {"approval_required", "error"}:
                    with lock:
                        manager.save(session)
                    _release_run(session_id, run_token)
                    released = True
                yield _sse_event(event)
        finally:
            if not released:
                _release_run(session_id, run_token)

    return RunStreamingResponse(sse(), session_id=session_id, run_token=run_token)
