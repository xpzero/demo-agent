import json
import time
from collections.abc import Iterator

from tools import TOOLS, execute_tool
from tools.context import SessionContext

from .client import MODEL, client
from .metrics import ToolRecord, TurnRecorder, excerpt

MAX_RUN_SECONDS = 300


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("本轮回复超时，请重试")

# 工具 SCHEMA 是扁平格式，Chat Completions 请求需要多一层 function 外壳
CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            key: schema[key] for key in ("name", "description", "parameters")
        },
    }
    for schema in TOOLS
]

# stream_events 产出的事件类型：
#   {"type": "text_delta", "text": str}                文本增量
#   {"type": "tool_call", "id": str, "name": str, "args": dict} 模型发起一次调用
#   {"type": "tool_result", "id": str, "content": str} 工具执行结果
#   {"type": "done", "content": str}                   模型给出最终回复
#   {"type": "max_turns"}                              触发轮次上限
#   {"type": "error", "message": str}                  请求或流处理失败


def _merge_tool_call_delta(
    partial_tool_calls: dict[int, dict], tool_call_delta
) -> None:
    """把单个工具调用增量碎片并入对应 index 的累积记录（就地修改）。"""
    index = (
        tool_call_delta.index if tool_call_delta.index is not None else 0
    )
    partial_tool_call = partial_tool_calls.setdefault(
        index, {"id": "", "name": "", "arguments": ""}
    )
    if tool_call_delta.id:
        partial_tool_call["id"] = tool_call_delta.id
    if tool_call_delta.function is not None:
        if tool_call_delta.function.name:
            partial_tool_call["name"] = tool_call_delta.function.name
        if tool_call_delta.function.arguments:
            partial_tool_call["arguments"] += (
                tool_call_delta.function.arguments
            )


def _aggregate_stream(
    stream, text_parts: list[str], partial_tool_calls: dict[int, dict], deadline: float,
    current_turn=None,
) -> Iterator[dict]:
    """消费模型的 chunk 流：文本增量向外透传，工具调用增量按 index 聚齐。

    结果就地写入 text_parts 和 partial_tool_calls，不产生返回值。
    current_turn 存在时（P0-2 埋点），从最后一个 chunk 提取 usage。
    """
    # 工具调用参数按 chunk 增量到达，必须按 index 聚齐后再解析
    for chunk in stream:
        _check_deadline(deadline)
        if current_turn is not None:
            usage = _extract_usage(chunk)
            if usage:
                current_turn.usage = usage
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        # GLM 的深度思考走 reasoning_content，不属于用户可见文本，跳过
        if delta.content:
            text_parts.append(delta.content)
            yield {"type": "text_delta", "text": delta.content}
        for tool_call_delta in delta.tool_calls or []:
            _merge_tool_call_delta(partial_tool_calls, tool_call_delta)


def _parse_call_args(tool_calls: list[dict]) -> list[tuple[dict, dict]]:
    """把聚齐的调用记录解析成 (tool_call, args) 列表，参数必须是 JSON 对象。"""
    tool_calls_with_args = []
    for tool_call in tool_calls:
        args = json.loads(tool_call["arguments"] or "{}")
        if not isinstance(args, dict):
            raise TypeError(f"工具 {tool_call['name']} 的参数必须是 JSON 对象")
        tool_calls_with_args.append((tool_call, args))
    return tool_calls_with_args


def _build_assistant_message(text: str, tool_calls: list[dict]) -> dict:
    """构造携带本轮全部工具调用的 assistant 消息（调用请求，非结果）。"""
    return {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [
            {
                "id": tool_call["id"],
                "type": "function",
                "function": {
                    "name": tool_call["name"],
                    "arguments": tool_call["arguments"],
                },
            }
            for tool_call in tool_calls
        ],
    }


def _run_tool_calls(
    tool_calls_with_args: list[tuple[dict, dict]],
    items: list,
    deadline: float,
    context: SessionContext | None,
    current_turn=None,
) -> Iterator[dict]:
    """按模型给出的顺序逐个执行：结果写回 items，tool_call_id 原样复用。

    current_turn 存在时（P0-2 埋点），每个工具的名/参数副本/结果副本/
    耗时/成败记入该次模型请求的观测记录；为 None 时零开销。
    """
    for tool_call, args in tool_calls_with_args:
        _check_deadline(deadline)
        yield {
            "type": "tool_call",
            "id": tool_call["id"],
            "name": tool_call["name"],
            "args": args,
        }
        tool_started = time.monotonic()
        tool_output, tool_ok = execute_tool(tool_call["name"], args, context)
        _check_deadline(deadline)
        tool_elapsed_ms = (time.monotonic() - tool_started) * 1000
        if current_turn is not None:
            current_turn.tools.append(
                ToolRecord(
                    name=tool_call["name"],
                    args_excerpt=excerpt(args),
                    result_excerpt=excerpt(tool_output),
                    duration_ms=tool_elapsed_ms,
                    ok=tool_ok,
                )
            )
        items.append(
            {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": tool_output,
            }
        )
        yield {
            "type": "tool_result",
            "id": tool_call["id"],
            "content": tool_output,
            "elapsed": tool_elapsed_ms,
        }


def _extract_usage(chunk) -> dict | None:
    """从流式 chunk 提取 usage（OpenAI 兼容接口在最后一个 chunk 携带）。

    接口不返回时保持 None，由调用方按文本估算并标 estimated。
    """
    usage = getattr(chunk, "usage", None)
    if not usage:
        return None
    data = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            data[key] = value
    return data or None


def stream_events(
    items: list,
    max_turns: int = 10,
    context: SessionContext | None = None,
    recorder: TurnRecorder | None = None,
) -> Iterator[dict]:
    """agent loop 的核心：流式请求模型、执行工具，把过程产出为结构化事件。

    items 是 Chat Completions 的 messages 列表，会被原地追加。
    context 携带本轮会话信息，供 parse_attached_document 这类
    需要定位会话附件的工具使用，与事件流本身无关。
    recorder 携带内存账本（P0-2 埋点）：逐次模型请求的耗时与 usage、
    每个工具的名/耗时/成败记入其中，落库由调用方决定——loop 不碰库。
    不做任何打印——HTTP 服务是事件流的消费方，这里只负责「发生了什么」，
    怎么呈现由调用方决定。
    """
    deadline = time.monotonic() + MAX_RUN_SECONDS
    try:
        for _ in range(max_turns):
            _check_deadline(deadline)
            current_turn = recorder.start_turn() if recorder is not None else None
            if current_turn is not None:
                current_turn.items_snapshot = [
                    {**item} if isinstance(item, dict) else item
                    for item in items
                ]
            stream = client.chat.completions.create(
                model=MODEL,
                messages=items,
                tools=CHAT_TOOLS,
                stream=True,
                stream_options={"include_usage": True},
            )

            text_parts: list[str] = []
            partial_tool_calls: dict[int, dict] = {}
            started = time.monotonic()
            yield from _aggregate_stream(
                stream, text_parts, partial_tool_calls, deadline,
                current_turn=current_turn,
            )
            if current_turn is not None:
                current_turn.duration_ms = (time.monotonic() - started) * 1000

            if not partial_tool_calls:
                reply = "".join(text_parts)
                if current_turn is not None:
                    current_turn.reply_text = reply
                # 最终回复也要写回上下文，否则模型下一轮看不到自己说过什么
                if reply:
                    items.append({"role": "assistant", "content": reply})
                yield {"type": "done", "content": reply}
                return

            tool_calls = [
                partial_tool_calls[index]
                for index in sorted(partial_tool_calls)
            ]
            # 先确认所有参数都能解析，再追加 assistant 消息，
            # 避免留下只有调用、没有结果的半截历史。
            tool_calls_with_args = _parse_call_args(tool_calls)
            items.append(
                _build_assistant_message("".join(text_parts), tool_calls)
            )
            yield from _run_tool_calls(tool_calls_with_args, items, deadline, context, current_turn)

        yield {"type": "max_turns"}
    except Exception as error:
        yield {"type": "error", "message": f"{type(error).__name__}: {error}"}
