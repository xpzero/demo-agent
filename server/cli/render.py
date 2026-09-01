from agent import stream_events
from agent.approval import commit_outputs, decide, pending_call_ids
from services import ServiceContainer


def render_events(
    items: list,
    services: ServiceContainer,
    max_turns: int = 10,
    session_id: int | None = None,
) -> None:
    """把事件流呈现到终端：文本逐字打印，工具调用与结果按行展示"""
    remaining_turns = max_turns

    while True:
        printing_text = False
        pending = None

        def checkpoint(batch: dict) -> None:
            nonlocal pending
            pending = batch

        for event in stream_events(
            items,
            services,
            remaining_turns,
            on_approval=checkpoint,
            session_id=session_id,
        ):
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
                preview = event.get("preview")
                if preview and preview.get("type") == "code_diff":
                    print(
                        f"  [拟议改动] {preview['path']} "
                        f"+{preview['additions']} -{preview['deletions']}"
                    )
            elif event["type"] == "tool_result":
                print(f"  [返回结果] {event['content']}")
            elif event["type"] == "done":
                if printing_text:
                    print()
            elif event["type"] == "max_turns":
                print("[达到最大轮次，停止]")
            elif event["type"] == "error":
                print(f"[请求失败] {event['message']}")

        if pending is None:
            return

        decided_ids = pending_call_ids(pending)
        for call_id in decided_ids:
            call = next(entry for entry in pending["calls"] if entry["id"] == call_id)
            answer = input(f"  允许执行 {call['name']}？[y/N] ").strip().lower()
            decide(pending, call_id, answer in {"y", "yes"})

        results = commit_outputs(
            items, pending, services.permission, session_id
        )
        for result in results:
            if result["id"] in decided_ids:
                print(f"  [返回结果] {result['content']}")
        remaining_turns = pending["remaining_turns"]
