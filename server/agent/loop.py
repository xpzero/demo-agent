import json
import time
from collections.abc import Iterator

from tools import TOOLS, execute_tool

from .client import MODEL, client

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
    stream, text_parts: list[str], partial_tool_calls: dict[int, dict], deadline: float
) -> Iterator[dict]:
    """消费模型的 chunk 流：文本增量向外透传，工具调用增量按 index 聚齐。

    结果就地写入 text_parts 和 partial_tool_calls，不产生返回值。
    """
    # 工具调用参数按 chunk 增量到达，必须按 index 聚齐后再解析
    for chunk in stream:
        _check_deadline(deadline)
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
    tool_calls_with_args: list[tuple[dict, dict]], items: list, deadline: float
) -> Iterator[dict]:
    """按模型给出的顺序逐个执行：结果写回 items，tool_call_id 原样复用。"""
    for tool_call, args in tool_calls_with_args:
        _check_deadline(deadline)
        yield {
            "type": "tool_call",
            "id": tool_call["id"],
            "name": tool_call["name"],
            "args": args,
        }
        tool_output = execute_tool(tool_call["name"], args)
        _check_deadline(deadline)
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
        }


def stream_events(items: list, max_turns: int = 10) -> Iterator[dict]:
    """agent loop 的核心：流式请求模型、执行工具，把过程产出为结构化事件。

    items 是 Chat Completions 的 messages 列表，会被原地追加。
    不做任何打印——HTTP 服务是事件流的消费方，这里只负责「发生了什么」，
    怎么呈现由调用方决定。
    """
    deadline = time.monotonic() + MAX_RUN_SECONDS
    try:
        for _ in range(max_turns):
            _check_deadline(deadline)
            stream = client.chat.completions.create(
                model=MODEL,
                messages=items,
                tools=CHAT_TOOLS,
                stream=True,
            )

            text_parts: list[str] = []
            partial_tool_calls: dict[int, dict] = {}
            yield from _aggregate_stream(stream, text_parts, partial_tool_calls, deadline)

            if not partial_tool_calls:
                reply = "".join(text_parts)
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
            yield from _run_tool_calls(tool_calls_with_args, items, deadline)

        yield {"type": "max_turns"}
    except Exception as error:
        yield {"type": "error", "message": f"{type(error).__name__}: {error}"}
