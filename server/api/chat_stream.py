"""聊天 SSE 流的编排：事件循环、done 落库与埋点落库。

routes.py 的 /api/chat 路由是薄壳（校验请求 → 调本模块 → 返回
StreamingResponse）；这里负责一轮 Chat 的完整生命周期：

- 消费 stream_events 的项目事件并转成 SSE 帧
- done 时持久化助手消息
- 流结束（无论成败）后统一落埋点账本：agent_turns 锚用户消息、
  汇总 meta 挂助手消息，失败只记日志不影响回复
"""

import json
import logging
from collections.abc import Iterator
from pathlib import Path

from agent import stream_events
from agent.metrics import TurnRecorder, export_turns, summarize_meta
from database import Database
from tools.context import SessionContext

logger = logging.getLogger(__name__)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def chat_sse_stream(
    *,
    database: Database,
    session_id: str,
    user_message_id: int,
    items: list,
    file_root: Path,
    release_session,
) -> Iterator[str]:
    """产出聊天 SSE 帧；release_session 在流结束后必定被调用。"""
    recorder = TurnRecorder()
    context = SessionContext(
        session_id=session_id, database=database, file_root=file_root
    )
    assistant_message_id = None
    try:
        yield _sse({"type": "user_message", "message_id": user_message_id})
        terminated = False
        for event in stream_events(items, context=context, recorder=recorder):
            if event["type"] == "done":
                content = event["content"]
                message_id = user_message_id
                if content or any(turn.tools for turn in recorder.turns):
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
                assistant_message_id = message_id if message_id != user_message_id else None
                event = {**event, "message_id": message_id}
            yield _sse(event)
            if event["type"] in ("done", "error"):
                terminated = True
                break
        if not terminated:
            yield _sse({"type": "error", "message": "本轮回复未完成，请重试"})
    except Exception as error:
        yield _sse(
            {"type": "error", "message": f"回复中断：{type(error).__name__}: {error}"}
        )
    finally:
        # 埋点落库：账本锚定本轮用户消息（轮开始前已存在），
        # 汇总挂牌挂助手消息；失败只记日志，不影响回复
        try:
            database.record_agent_turns(
                message_id=user_message_id, turns=export_turns(recorder)
            )
            if assistant_message_id is not None:
                database.set_message_meta(
                    assistant_message_id,
                    summarize_meta(recorder),
                )
        except Exception as error:
            logger.warning(
                "埋点落库失败：%s: %s", type(error).__name__, error
            )
        release_session()
