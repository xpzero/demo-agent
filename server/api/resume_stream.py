"""只读恢复流：resume 三步裁决与 SSE 回放-实时交接。

场景 A/B（活流回放+实时跟随）、D（终态快照）、E（判死入口之一）。
判死收口逻辑在 reaper；本模块只负责传输。
"""

import asyncio
import json
import time

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from database import Database

from .active_runs import active_runs
from .reaper import finalize_dead, is_dead_residual
from .run_log import run_log

# 回放消费循环的轮询间隔与 SSE 保活
POLL_SECONDS = 0.05
HEARTBEAT_SECONDS = 15
# 终态事件：收到即关流（前端 terminal 判定同源）
TERMINAL_EVENTS = frozenset({"done", "error", "max_turns", "stopped"})


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def snapshot_response(database: Database, session_id: str) -> Response:
    """终态/判死快照：单帧历史 + 关流（前端走既有历史转换）。"""
    history = database.get_session_history(session_id)
    if history is None:
        raise HTTPException(404, detail={"code": "session_not_found", "message": "会话不存在"})
    return Response(
        content=_sse({"type": "resume_snapshot", "history": history}),
        media_type="text/event-stream",
    )


async def _consume(past: list[tuple[int, dict]], queue, request: Request):
    """先播 Run 快照（seq 1..N），再接实时队列（N+1 起）到终态关流。"""
    last_seq = 0
    last_sent = time.monotonic()
    for seq, event in past:
        yield _sse(event)
        last_seq = seq
    while True:
        if await request.is_disconnected():
            return
        try:
            event, seq = await asyncio.to_thread(queue.get, timeout=POLL_SECONDS)
        except Exception:  # queue.Empty 的超时路径
            event = None
            seq = 0
        if event is not None:
            if seq <= last_seq:
                continue
            yield _sse(event)
            last_sent = time.monotonic()
            if event.get("type") in TERMINAL_EVENTS:
                return
        now = time.monotonic()
        if now - last_sent >= HEARTBEAT_SECONDS:
            yield ": heartbeat\n\n"
            last_sent = now


def resume_response(database: Database, session_id: str, request: Request):
    """resume 三步裁决（缺口 B/D 内嵌）。"""
    message = database.get_unfinished_message(session_id)
    if message is None:
        return Response(status_code=204)
    if is_dead_residual(message):
        # 超期，或 Run 不在 = 进程重启残留（缺口 D，不等 deadline）
        finalize_dead(database, message)
        return snapshot_response(database, session_id)

    attached = active_runs.attach(message["id"])
    if attached is None:
        # attach 与 has 之间 Run 被 close（终态刚落）→ 走快照
        return snapshot_response(database, session_id)
    run_log.info("resume", "恢复接管直播", message_id=message["id"])
    past, queue = attached
    if not past and database.get_unfinished_message(session_id) is None:
        # 空快照重查（缺口 B）：已终态 → 快照；仍 unfinished = 刚预建，挂队列等
        active_runs.detach(message["id"], queue)
        return snapshot_response(database, session_id)

    message_id = message["id"]

    def detach():
        active_runs.detach(message_id, queue)

    return StreamingResponse(
        _consume(past, queue, request),
        media_type="text/event-stream",
        background=BackgroundTask(detach),
    )
