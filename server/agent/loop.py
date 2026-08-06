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
        # 工具调用会被拆散在多个 chunk 里；部分网关对多个调用一律返回 index=0，
        # 因此按 id 归拢：id 相同即同一个调用，不依赖 index，也不怕片段交错送达
        calls: list[dict] = []

        for chunk in stream:
            # 每个 chunk 只携带增量，所以取 delta 而不是 message
            delta = chunk.choices[0].delta

            if delta.content:
                content += delta.content
                yield {"type": "text_delta", "text": delta.content}

            # 为 None 说明这个 chunk 不含工具调用信息
            for tc in delta.tool_calls or []:
                # 出现没见过的 id 就是一个新调用（同一个工具被调多次时 id 也不同）
                if not calls or (tc.id and all(c["id"] != tc.id for c in calls)):
                    calls.append({"id": tc.id or "", "name": "", "arguments": ""})

                # 带 id 的片段归回它自己的调用，不带 id 的归到最近一个调用
                slot = next((c for c in calls if tc.id and c["id"] == tc.id), calls[-1])
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
                    for slot in calls
                ],
            }
        )

        for slot in calls:
            args = json.loads(slot["arguments"])
            yield {"type": "tool_call", "id": slot["id"], "name": slot["name"], "args": args}

            result = execute_tool(slot["name"], args)
            yield {"type": "tool_result", "id": slot["id"], "content": result}

            messages.append(
                {"role": "tool", "tool_call_id": slot["id"], "content": result}
            )

    yield {"type": "max_turns"}


def run_tool_loop_stream(messages: list, max_turns: int = 10) -> None:
    """CLI 消费事件流：文本逐字打印，工具调用与结果按行展示"""
    printing_text = False

    for event in stream_events(messages, max_turns):
        if event["type"] == "text_delta":
            # 首个文本片段到达时才打印前缀，避免纯工具调用的轮次也输出 "Agent: "
            if not printing_text:
                print("Agent: ", end="", flush=True)
                printing_text = True
            print(event["text"], end="", flush=True)
        elif event["type"] == "tool_call":
            if printing_text:
                print()
                printing_text = False
            print(f"  [工具调用] {event['name']}({event['args']})")
        elif event["type"] == "tool_result":
            print(f"  [返回结果] {event['content']}")
        elif event["type"] == "done":
            if printing_text:
                print()
        elif event["type"] == "max_turns":
            print("[达到最大轮次，停止]")


def execute_tool_calls(messages: list, tool_calls: list) -> None:
    """执行模型请求的工具，并把结果以 role=tool 消息追加回 messages。

    tool_calls 统一为 (调用 id, 工具名, 参数 JSON 字符串) 三元组，
    以此屏蔽流式与非流式两种返回结构的差异。
    """
    for call_id, name, arguments in tool_calls:
        args = json.loads(arguments)
        print(f"  [工具调用] {name}({args})")

        result = execute_tool(name, args)
        print(f"  [返回结果] {result}")

        messages.append({"role": "tool", "tool_call_id": call_id, "content": result})


def run_tool_loop(messages: list, max_turns: int = 10) -> None:
    """非流式：等模型完整返回后再处理，直到它给出最终文本回复"""
    for _ in range(max_turns):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
        )

        message = response.choices[0].message
        messages.append(message)

        if not message.tool_calls:
            print(f"Agent: {message.content}")
            return

        execute_tool_calls(
            messages,
            [(tc.id, tc.function.name, tc.function.arguments) for tc in message.tool_calls],
        )

    print("[达到最大轮次，停止]")
