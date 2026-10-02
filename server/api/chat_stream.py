"""聊天执行的编排：后台执行线程 + SSE 订阅广播，两层分离。

routes.py 的 /api/chat 路由是薄壳（校验请求 → 调本模块 → 返回
StreamingResponse）；这里负责一轮 Chat 的完整生命周期：

- 发送即预建助手行 + 建 Run（先 Run 后 DB 行，缺口 D 顺序）
- **执行体在后台线程**（run_executor）：跑 agent loop、写 fragments、
  终态落库——与 SSE 连接无关，客户端断连/刷新不影响执行
- SSE 生成器（subscribe_stream）只是订阅者：回放快照 + 实时广播，
  断开仅退订；执行继续，下次 resume 再 attach
- done/error/max_turns/stop 各路径统一 finalize 终态单向收口
- 流结束（无论成败）后统一落埋点账本
"""

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from queue import Queue
from threading import Event, Thread

from agent import stream_events
from agent.context_budget import maybe_roll, needs_roll
from agent.loop import StopRequestedError
from agent.metrics import TurnRecorder, export_turns, summarize_meta
from database import Database
from tools.context import SessionContext

from .active_runs import active_runs
from .run_log import run_log

logger = logging.getLogger(__name__)

# text 攒批窗口：同一窗口的多个 delta 合成一个 fragment 级事件（缺口 A）
TEXT_FLUSH_SECONDS = 0.5
# SSE 保活：防代理掐静默连接
HEARTBEAT_SECONDS = 15


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _roll_session(database: Database, session_id: str, message_id: int) -> None:
    try:
        maybe_roll(database, session_id, message_id)
    except Exception as error:
        logger.warning("摘要失败，下轮重试：%s: %s", type(error).__name__, error)


class _FragmentSink:
    """每事件「先落库、后进 Run」；text 按节流窗口攒批。

    所有方法返回对外事件 dict（已落库、已广播），由调用方转 SSE 帧。
    """

    def __init__(self, database: Database, message_id: int):
        self.database = database
        self.message_id = message_id
        self.text_buffer: list[str] = []
        self.last_flush = time.monotonic()

    def _publish(self, type_: str, payload: dict, status: str | None = None) -> dict:
        seq = self.database.append_fragment(
            self.message_id, type_, payload, status=status,
        )
        active_runs.publish(self.message_id, payload, seq)
        return payload

    def flush_text(self, *, force: bool = False) -> list[dict]:
        """攒批非空且（force 或超窗）→ 一个 text fragment 级事件。"""
        if not self.text_buffer:
            return []
        if not force and time.monotonic() - self.last_flush < TEXT_FLUSH_SECONDS:
            return []
        text = "".join(self.text_buffer)
        self.text_buffer.clear()
        self.last_flush = time.monotonic()
        return [self._publish("text", {"type": "text_delta", "text": text})]

    def tool_call(self, event: dict) -> dict:
        return self._publish(
            "tool_call",
            {"type": "tool_call", "id": event["id"], "name": event["name"],
             "args": event["args"]},
            status="running",
        )

    def tool_result(self, event: dict) -> dict:
        return self._publish(
            "tool_result",
            {"type": "tool_result", "id": event["id"], "content": event["content"],
             "elapsed": event.get("elapsed")},
            status="finished",
        )

    def terminal(self, kind: str, **fields) -> dict:
        """终态 fragment + 广播；返回对外事件。"""
        payload = {"type": kind, **fields}
        seq = self.database.append_fragment(self.message_id, "terminal", payload)
        active_runs.publish(self.message_id, payload, seq)
        return payload

    def text_so_far(self) -> str:
        """收口路径全文以库为准。"""
        return self.database.assemble_text(self.message_id)


def run_executor(
    *,
    database: Database,
    session_id: str,
    user_message_id: int,
    assistant_id: int,
    items: list,
    file_root: Path,
    release_session,
    stop_signal: Event | None = None,
) -> None:
    """执行体：后台线程跑完一整轮（与连接无关）。

    每事件先落库后广播；各终态路径统一收口；结束后 Run 关闭。
    """
    recorder = TurnRecorder()
    context = SessionContext(
        session_id=session_id, database=database, file_root=file_root
    )
    sink = _FragmentSink(database, assistant_id)
    session_released = [False]

    def release_session_once() -> None:
        """锁只放一次：终态广播前释放，保证订阅者看到终态即可发新消息。"""
        if not session_released[0]:
            session_released[0] = True
            release_session()

    def close_out(kind: str, content: str | None = None, **fields) -> None:
        """统一收口：冲刷 → 回填（以库为准）→ 终态落库 → 放锁 → terminal 广播。"""
        sink.flush_text(force=True)
        if content is None:
            content = sink.text_so_far()
        database.finalize_assistant(assistant_id, kind, content)
        run_log.info(
            "chat_stream", f"终态 {kind}", message_id=assistant_id, chars=len(content or ""))
        release_session_once()
        sink.terminal(kind, message_id=assistant_id, **fields)

    try:
        active_runs.publish(user_message_id, {"type": "user_message", "message_id": user_message_id}, seq=0)
        terminated = False
        for event in stream_events(
            items, context=context, recorder=recorder, stop_signal=stop_signal,
        ):
            kind = event["type"]
            if kind == "text_delta":
                sink.text_buffer.append(event["text"])
                # 窗口到期即冲刷（纯文字流也会触发，事件产生即落库）
                sink.flush_text()
                continue
            # 交错保真：任何非 text 事件前强制冲刷，文字段边界由 fragment 承载
            sink.flush_text(force=True)
            if kind == "tool_call":
                sink.tool_call(event)
            elif kind == "tool_result":
                sink.tool_result(event)
            elif kind == "done":
                close_out("done", event["content"])
                terminated = True
                break
            elif kind == "error":
                close_out("error", message=event["message"])
                terminated = True
                break
            elif kind == "max_turns":
                close_out("max_turns", None)
                terminated = True
                break
            else:
                active_runs.publish(assistant_id, event, seq=0)
        if not terminated:
            close_out("error", message="本轮回复未完成，请重试")
    except StopRequestedError:
        close_out("stopped")
    except Exception as error:
        try:
            close_out(
                "error",
                message=f"回复中断：{type(error).__name__}: {error}",
            )
        except Exception:
            logger.exception("错误路径收口失败")
    finally:
        active_runs.close(assistant_id)
        try:
            database.record_agent_turns(
                message_id=user_message_id, turns=export_turns(recorder)
            )
            database.set_message_meta(assistant_id, summarize_meta(recorder))
        except Exception as error:
            logger.warning("埋点落库失败：%s: %s", type(error).__name__, error)
        release_session_once()
        try:
            if needs_roll(database, session_id, assistant_id):
                Thread(
                    target=_roll_session,
                    args=(database, session_id, assistant_id),
                    daemon=True,
                ).start()
        except Exception as error:
            logger.warning("检查摘要触发失败，下轮重试：%s: %s", type(error).__name__, error)


def start_run(
    *,
    database: Database,
    session_id: str,
    user_message_id: int,
    items: list,
    file_root: Path,
    release_session,
    stop_signal: Event | None = None,
) -> int:
    """发送入口：预建（Run→DB 行→bind）+ 启动执行线程；返回 assistant_id。"""
    placeholder = active_runs.reserve()
    assistant_id = database.create_pending_assistant(session_id, user_message_id)
    active_runs.bind(placeholder, assistant_id)
    run_log.info("chat_stream", "开始执行", message_id=assistant_id, session=session_id[:8])
    Thread(
        target=run_executor,
        kwargs=dict(
            database=database,
            session_id=session_id,
            user_message_id=user_message_id,
            assistant_id=assistant_id,
            items=items,
            file_root=file_root,
            release_session=release_session,
            stop_signal=stop_signal,
        ),
        daemon=True,
    ).start()
    return assistant_id


def subscribe_stream(
    *,
    assistant_id: int,
    user_message_id: int,
) -> Iterator[str]:
    """SSE 订阅层：回放快照 + 实时广播；断开只退订，不影响执行。"""
    attached = active_runs.attach(assistant_id)
    queue: Queue | None = None
    try:
        yield _sse({"type": "user_message", "message_id": user_message_id})
        if attached is not None:
            _, queue = attached
            past, _q = attached
            for _seq, event in past:
                yield _sse(event)
        else:
            # Run 已终态/不存在：不等待，交由历史接口与快照兜底
            return
        import time as _time
        last_beat = _time.monotonic()
        while True:
            try:
                event, _seq = queue.get(timeout=HEARTBEAT_SECONDS)
            except Exception:
                # 心跳保活
                yield ": keep-alive\n\n"
                last_beat = _time.monotonic()
                continue
            if event.get("type") in ("done", "error", "max_turns", "stopped", "interrupted"):
                yield _sse(event)
                return
            yield _sse(event)
            last_beat = _time.monotonic()
    finally:
        if queue is not None:
            active_runs.detach(assistant_id, queue)
