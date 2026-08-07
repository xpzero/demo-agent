import json
from collections.abc import Iterator

from tools import TOOLS, execute_tool

from .client import MODEL, client

# stream_events 产出的事件类型：
#   {"type": "text_delta", "text": str}                     文本增量
#   {"type": "tool_call", "id": str, "name": str, "args": dict}    模型发起一次调用
#   {"type": "tool_result", "id": str, "content": str}      工具执行结果
#   {"type": "done", "content": str}                        模型给出最终回复
#   {"type": "max_turns"}                                   触发轮次上限


def stream_events(messages: list, max_turns: int = 10) -> Iterator[dict]:
    """agent loop 的核心：流式请求模型、执行工具，把过程产出为结构化事件。

    不做任何打印——CLI 与 HTTP 服务是两种不同的消费方式，
    这里只负责「发生了什么」，怎么呈现由调用方决定。messages 会被原地追加。
    """
    for _ in range(max_turns):
        stream = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            stream=True,
        )

        content = ""
        # 按 index 归拢分片，与官方 SDK 一致：index 是 tool_calls 数组的下标，
        # 官方视其为必填并据此定位（openai/lib/streaming/_deltas.py 缺失即报错）。
        # 用 dict 而非 list，是为了容忍多个调用的分片交错到达
        calls: dict[int, dict] = {}

        for chunk in stream:
            # 每个 chunk 只携带增量，所以取 delta 而不是 message
            delta = chunk.choices[0].delta

            if delta.content:
                content += delta.content
                yield {"type": "text_delta", "text": delta.content}

            # 为 None 说明这个 chunk 不含工具调用信息
            for tc in delta.tool_calls or []:
                slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                # id 与 name 只在首个分片出现，出现即取
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    # 参数是逐段送来的 JSON 字符串碎片，只能累加，不能覆盖
                    slot["arguments"] += tc.function.arguments

        # 没有工具调用 → 模型给出了最终回复，写回上下文后结束
        if not calls:
            messages.append({"role": "assistant", "content": content})
            yield {"type": "done", "content": content}
            return

        # dict 不保证遍历顺序反映 index 大小，按 index 排序还原调用顺序
        ordered = [calls[index] for index in sorted(calls)]

        # 流式拿不到现成的 message 对象，需按非流式的结构自己拼一条 assistant 消息，
        # 否则下一轮请求时模型不知道自己发起过哪些调用，后面的 tool 消息也无从对应
        messages.append(
            {
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": slot["id"],
                        "type": "function",
                        "function": {"name": slot["name"], "arguments": slot["arguments"]},
                    }
                    for slot in ordered
                ],
            }
        )

        for slot in ordered:
            args = json.loads(slot["arguments"])
            yield {"type": "tool_call", "id": slot["id"], "name": slot["name"], "args": args}

            result = execute_tool(slot["name"], args)
            yield {"type": "tool_result", "id": slot["id"], "content": result}

            messages.append(
                {"role": "tool", "tool_call_id": slot["id"], "content": result}
            )

    yield {"type": "max_turns"}
