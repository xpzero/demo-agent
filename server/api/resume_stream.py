"""只读恢复传输：重放与事实跟随不启动模型或重新提交操作。"""

import asyncio
import re
import time
from threading import Event, Thread

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from .event_buffer import buffers, sse

ACTIVE = frozenset({"running", "finishing"})
POLL_SECONDS = 1
HEARTBEAT_SECONDS = 15


def session_history(database, session_id):
    history = database.get_session_history(session_id)
    if history is None:
        raise HTTPException(404, detail={"code": "session_not_found", "message": "会话不存在"})
    history["can_send_message"] = database.get_active_run(session_id) is None
    progress = database.get_session_progress(session_id)
    if progress is not None:
        # Public progress retains message ownership and operation facts, but
        # internal execution identifiers stay inside the database layer.
        for run in progress["runs"]:
            run.pop("id", None)
        for task in progress["tasks"]:
            for operation in task["operations"]:
                operation.pop("run_id", None)
        history["progress"] = progress
    return history


def watch_terminal(database, run_id, buffer):
    """Expire without future HTTP traffic, including deferred finishing Runs."""
    def watch():
        try:
            while True:
                run = database.get_run(run_id)
                if run is None:
                    buffers.discard(database, run_id)
                    return
                if run["status"] not in ACTIVE:
                    buffers.terminal(database, run_id, run["finished_at"])
                    return
                Event().wait(POLL_SECONDS)
        except Exception:
            # No usable database means no safe replay arbitration. Existing
            # subscribers still hold the object and will surface the DB error.
            buffers.discard(database, run_id)
    Thread(target=watch, name=f"resume-terminal-{run_id}", daemon=True).start()


def parse_cursor(value):
    if value is None:
        return None
    if re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", value) is None or len(value) > 100:
        raise HTTPException(400, detail={"code": "invalid_event_id", "message": "恢复游标无效"})
    message_id, sequence = value.split(":")
    return int(message_id), int(sequence)


def resume_response(database, session_id, request: Request, last_event_id):
    # Shares the short acceptance/registration critical section with POST.
    with buffers.condition:
        return _resume_response(database, session_id, request, last_event_id)


def _resume_response(database, session_id, request: Request, last_event_id):
    cursor = parse_cursor(last_event_id)
    # Arbitration precedes the snapshot. Only finishing is eligible for the
    # original-deadline check; a live producer's running Run is never reclaimed.
    run = database.get_active_run(session_id)
    if run is not None and run["status"] == "finishing":
        database.reconcile_orphan_run(run["id"])
    run = database.get_active_run(session_id) or database.get_latest_run(session_id)
    history = session_history(database, session_id)
    if run is None:
        return Response(status_code=204)
    buffer = buffers.get(database, run["id"])
    messages = history["messages"]
    incomplete = (bool(messages) and (messages[-1]["role"] == "user"
                  or messages[-1]["status"] != "finished"))
    incomplete = incomplete or any(task["status"] != "completed"
                                  for task in history.get("progress", {}).get("tasks", []))
    if run["status"] not in ACTIVE and not incomplete:
        return Response(status_code=204)
    if buffer is None:
        # A shared observation-only buffer also bounds snapshot subscriptions.
        # Its presence never promises exact replay of a restarted execution.
        buffer, created = buffers.get_or_register_observer(database, run["id"], run["user_message_id"])
        if created:
            watch_terminal(database, run["id"], buffer)
    try:
        valid_cursor, after = buffer.subscribe(cursor)
    except OverflowError as error:
        raise HTTPException(429, detail={"code": "resume_capacity", "message": str(error)}) from error
    released = False

    def release():
        nonlocal released
        if not released:
            released = True
            buffer.unsubscribe()

    def snapshot(replay):
        return {"type": "resume_snapshot", "history": session_history(database, session_id),
                "user_message_id": run["user_message_id"], "replay": replay}

    async def consume():
        nonlocal after
        last_snapshot = None
        replaying = True
        last_check = 0
        last_sent = time.monotonic()
        try:
            if run["status"] not in ACTIVE:
                # Already ended executions expose persisted facts. A retained
                # buffer is only needed by subscriptions established while live.
                yield sse(await asyncio.to_thread(snapshot, False))
                yield sse({"type": "stream_end"})
                return
            exact, _, _ = buffer.read(after)
            last_snapshot = await asyncio.to_thread(snapshot, exact)
            if not valid_cursor:
                yield sse(last_snapshot)
                replaying = exact
            while True:
                if await request.is_disconnected():
                    return
                exact, events, producer_done = buffer.read(after)
                if not exact and replaying:
                    replaying = False
                    last_snapshot = await asyncio.to_thread(snapshot, False)
                    yield sse(last_snapshot)
                    last_sent = time.monotonic()
                if exact:
                    for sequence, frame in events:
                        after = sequence
                        yield frame
                        last_sent = time.monotonic()
                now = time.monotonic()
                if now - last_check >= POLL_SECONDS:
                    current = await asyncio.to_thread(database.get_run, run["id"])
                    if current is None:
                        raise RuntimeError("恢复执行记录不存在")
                    terminal = current["status"] not in ACTIVE
                    if not exact or (producer_done and not terminal):
                        fact = await asyncio.to_thread(snapshot, False)
                        if fact["history"] != last_snapshot["history"]:
                            last_snapshot = fact
                            yield sse(fact)
                            last_sent = time.monotonic()
                    if terminal and producer_done:
                        # Terminal persistence can precede the producer's final
                        # append/metrics cleanup. Drain again *after* observing
                        # producer_done so a done/error is never skipped.
                        exact, tail, _ = buffer.read(after)
                        if exact:
                            for sequence, frame in tail:
                                after = sequence
                                yield frame
                        elif replaying:
                            yield sse(await asyncio.to_thread(snapshot, False))
                        active = await asyncio.to_thread(database.get_active_run, session_id)
                        if active is not None and active["id"] == run["id"]:
                            raise RuntimeError("终态执行仍占用会话")
                        buffers.terminal(database, run["id"], current["finished_at"])
                        yield sse({"type": "stream_end"})
                        return
                    last_check = now
                if now - last_sent >= HEARTBEAT_SECONDS:
                    yield ": heartbeat\n\n"
                    last_sent = now
                await asyncio.sleep(0.05)
        finally:
            release()

    return StreamingResponse(consume(), media_type="text/event-stream",
                             background=BackgroundTask(release))
