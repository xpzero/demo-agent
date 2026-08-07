from agent import stream_events


def render_events(messages: list, max_turns: int = 10) -> None:
    """把事件流呈现到终端：文本逐字打印，工具调用与结果按行展示"""
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
