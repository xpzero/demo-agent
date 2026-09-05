import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from threading import Lock, RLock

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
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
from services import ServiceContainer, create_default_services
from sessions import (
    SessionDataError,
    SessionError,
    SessionNotFound,
    SessionOutcomeUncertain,
    SessionRevisionConflict,
    SessionService,
    SessionStorageError,
    create_default_session_service,
)

# uvicorn 先配好自己的日志再导入本模块，放在这里设置才不会被它覆盖
setup_logging()


class ChatRequest(BaseModel):
    message: str


class SessionRuntime:
    """单个 API 进程内的会话运行保护。"""

    def __init__(self):
        self._state_lock = Lock()
        self._session_locks: dict[int, RLock] = {}
        self._running_sessions: dict[int, object] = {}

    def session_lock(self, session_id: int) -> RLock:
        with self._state_lock:
            return self._session_locks.setdefault(session_id, RLock())

    def reserve(self, session_id: int) -> object:
        with self._state_lock:
            if session_id in self._running_sessions:
                raise HTTPException(status_code=409, detail="该会话已有任务正在运行")
            token = object()
            self._running_sessions[session_id] = token
            return token

    def release(self, session_id: int, token: object) -> None:
        with self._state_lock:
            if self._running_sessions.get(session_id) is token:
                del self._running_sessions[session_id]

    def is_running(self, session_id: int) -> bool:
        with self._state_lock:
            return session_id in self._running_sessions


class RunStreamingResponse(StreamingResponse):
    def __init__(self, content, *, on_close: Callable[[], None]):
        super().__init__(content, media_type="text/event-stream")
        self._on_close = on_close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._on_close()


def _sse_event(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _error_event(error: Exception) -> dict:
    return {"type": "error", "message": f"{type(error).__name__}: {error}"}


def _public_pending(batch: dict | None) -> dict | None:
    if batch is None:
        return None

    calls = []
    for call in batch["calls"]:
        calls.append({key: value for key, value in call.items() if key != "guard"})

    return {
        "remaining_turns": batch["remaining_turns"],
        "outputs_committed": batch["outputs_committed"],
        "calls": calls,
    }


def create_app(
    session_service_factory: Callable[[], SessionService] = create_default_session_service,
    agent_services: ServiceContainer | None = None,
) -> FastAPI:
    runtime = SessionRuntime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        session_service = session_service_factory()
        app.state.session_service = session_service
        try:
            yield
        finally:
            session_service.close()

    app = FastAPI(title="demo-agent", lifespan=lifespan)
    app.state.runtime = runtime
    app.state.agent_services = agent_services or create_default_services()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(SessionNotFound)
    async def session_not_found(_, error: SessionNotFound):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(SessionRevisionConflict)
    async def session_revision_conflict(_, error: SessionRevisionConflict):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(SessionDataError)
    async def session_data_error(_, error: SessionDataError):
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @app.exception_handler(SessionOutcomeUncertain)
    async def session_outcome_uncertain(_, error: SessionOutcomeUncertain):
        return JSONResponse(status_code=503, content={"detail": str(error)})

    @app.exception_handler(SessionStorageError)
    async def session_storage_error(_, error: SessionStorageError):
        return JSONResponse(status_code=503, content={"detail": str(error)})

    def sessions() -> SessionService:
        return app.state.session_service

    def get_session(session_id: int):
        session = sessions().get(session_id)
        if session is None:
            raise SessionNotFound(session_id)
        return session

    @app.get("/api/sessions")
    def list_sessions():
        return [
            {
                "id": session.id,
                "summary": session.summary,
                "message_count": session.message_count,
            }
            for session in sessions().list_sessions()
        ]

    @app.post("/api/sessions")
    def create_session():
        session = sessions().create(SYSTEM_PROMPT)
        return {"id": session.id}

    @app.get("/api/sessions/{session_id}")
    def read_session(session_id: int):
        session = get_session(session_id)
        return {
            "id": session.id,
            "summary": session.summary,
            "pending_approval": _public_pending(session.pending_approval),
        }

    @app.post("/api/sessions/{session_id}/chat")
    def chat(session_id: int, body: ChatRequest):
        get_session(session_id)
        lock = runtime.session_lock(session_id)
        with lock:
            session = get_session(session_id)
            if session.pending_approval is not None:
                raise HTTPException(
                    status_code=409, detail="该会话仍有工具调用等待处理"
                )
            run_token = runtime.reserve(session_id)
            item_count = len(session.items)
            try:
                session.items.append({"role": "user", "content": body.message})
                sessions().save(session)
            except Exception:
                del session.items[item_count:]
                runtime.release(session_id, run_token)
                raise

        def save_checkpoint() -> None:
            with lock:
                sessions().save(session)

        def save_approval(batch: dict) -> None:
            with lock:
                session.pending_approval = batch
                sessions().save(session)

        def sse():
            try:
                for event in stream_events(
                    session.items,
                    app.state.agent_services,
                    on_approval=save_approval,
                    on_checkpoint=save_checkpoint,
                    session_id=session.id,
                ):
                    if event["type"] == "done":
                        with lock:
                            sessions().save(session)
                    if event["type"] in {
                        "approval_required",
                        "done",
                        "max_turns",
                        "error",
                    }:
                        runtime.release(session_id, run_token)
                    yield _sse_event(event)
            except Exception as error:
                runtime.release(session_id, run_token)
                yield _sse_event(_error_event(error))

        return RunStreamingResponse(
            sse(), on_close=lambda: runtime.release(session_id, run_token)
        )

    def record_decision(session_id: int, call_id: str, approved: bool):
        get_session(session_id)
        lock = runtime.session_lock(session_id)
        with lock:
            if runtime.is_running(session_id):
                raise HTTPException(
                    status_code=409, detail="该会话仍在运行，请稍后重试"
                )

            session = get_session(session_id)
            batch = session.pending_approval
            if batch is None:
                raise HTTPException(status_code=409, detail="该会话没有待审批工具")

            existing = next(
                (entry for entry in batch["calls"] if entry["id"] == call_id), None
            )
            before = (
                existing.get("decision") if existing is not None else None,
                existing.get("outcome") if existing is not None else None,
                existing.get("output") if existing is not None else None,
            )
            try:
                call = decide(batch, call_id, approved)
            except KeyError:
                raise HTTPException(
                    status_code=404, detail=f"没有工具调用 {call_id}"
                ) from None
            except ValueError as error:
                raise HTTPException(status_code=409, detail=str(error)) from None

            if approved:
                execute_approved_call(
                    call, app.state.agent_services.permission, session.id
                )
            after = (call["decision"], call.get("outcome"), call.get("output"))
            if before != after:
                try:
                    sessions().save(session)
                except SessionError as error:
                    if approved:
                        raise SessionOutcomeUncertain(session.id) from error
                    raise

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
        return record_decision(session_id, call_id, True)

    @app.post("/api/sessions/{session_id}/approvals/{call_id}/reject")
    def reject_tool(session_id: int, call_id: str):
        return record_decision(session_id, call_id, False)

    @app.post("/api/sessions/{session_id}/resume")
    def resume(session_id: int):
        get_session(session_id)
        lock = runtime.session_lock(session_id)
        with lock:
            session = get_session(session_id)
            batch = session.pending_approval
            if batch is None:
                raise HTTPException(
                    status_code=409, detail="该会话没有可以继续的审批任务"
                )
            if not is_ready(batch):
                raise HTTPException(status_code=409, detail="仍有工具调用等待审批")
            run_token = runtime.reserve(session_id)

        def save_checkpoint() -> None:
            with lock:
                if session.pending_approval is batch:
                    session.pending_approval = None
                sessions().save(session)

        def save_approval(next_batch: dict) -> None:
            with lock:
                session.pending_approval = next_batch
                sessions().save(session)

        def sse():
            try:
                with lock:
                    was_committed = batch["outputs_committed"]
                    results = commit_outputs(
                        session.items,
                        batch,
                        app.state.agent_services.permission,
                        session.id,
                    )
                    if not was_committed:
                        sessions().save(session)

                for result in results:
                    yield _sse_event(result)

                for event in stream_events(
                    session.items,
                    app.state.agent_services,
                    max_turns=batch["remaining_turns"],
                    on_approval=save_approval,
                    on_checkpoint=save_checkpoint,
                    session_id=session.id,
                ):
                    if event["type"] in {"done", "max_turns"}:
                        with lock:
                            if session.pending_approval is batch:
                                session.pending_approval = None
                            sessions().save(session)
                    if event["type"] in {
                        "approval_required",
                        "done",
                        "max_turns",
                        "error",
                    }:
                        runtime.release(session_id, run_token)
                    yield _sse_event(event)
            except Exception as error:
                runtime.release(session_id, run_token)
                yield _sse_event(_error_event(error))

        return RunStreamingResponse(
            sse(), on_close=lambda: runtime.release(session_id, run_token)
        )

    return app


app = create_app()
