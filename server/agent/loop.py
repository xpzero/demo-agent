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
#   {"type": "error", "message": str}                       请求或流处理失败


def stream_events(items: list, max_turns: int = 10) -> Iterator[dict]:
    """agent loop 的核心：流式请求模型、执行工具，把过程产出为结构化事件。

    不做任何打印——CLI 与 HTTP 服务是两种不同的消费方式，
    这里只负责「发生了什么」，怎么呈现由调用方决定。items 会被原地追加。
    """
    try:
        for _ in range(max_turns):
            stream = client.responses.create(
                model=MODEL,
                input=items,
                tools=TOOLS,
                stream=True,
                store=False,
                include=["reasoning.encrypted_content"],
            )

            response = None

            for event in stream:
                if event.type == "response.output_text.delta":
                    yield {"type": "text_delta", "text": event.delta}
                elif event.type == "response.completed":
                    response = event.response
                elif event.type == "response.failed":
                    error = event.response.error
                    detail = error.message if error else "未知错误"
                    raise RuntimeError(f"Responses API 请求失败：{detail}")
                elif event.type == "response.incomplete":
                    details = event.response.incomplete_details
                    reason = details.reason if details else "未知原因"
                    raise RuntimeError(f"Responses API 响应不完整：{reason}")
                elif event.type == "error":
                    raise RuntimeError(f"Responses API 流错误：{event.message}")

            if response is None:
                raise RuntimeError("Responses API 流结束时未收到 completed 事件")

            calls = [item for item in response.output if item.type == "function_call"]
            parsed_calls = [(call, json.loads(call.arguments)) for call in calls]

            # 完整保留 Responses 的所有 output Items，包括 reasoning，
            # 工具结果回灌后模型才能继续上一轮的推理与调用。
            items.extend(response.output)

            if not calls:
                yield {"type": "done", "content": response.output_text}
                return

            for call, args in parsed_calls:
                yield {
                    "type": "tool_call",
                    "id": call.call_id,
                    "name": call.name,
                    "args": args,
                }

                result = execute_tool(call.name, args)
                yield {"type": "tool_result", "id": call.call_id, "content": result}

                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": result,
                    }
                )

        yield {"type": "max_turns"}
    except Exception as error:
        yield {"type": "error", "message": f"{type(error).__name__}: {error}"}
