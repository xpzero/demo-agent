from collections.abc import Callable

from agent import stream_events
from agent.approval import commit_outputs, decide, pending_call_ids
from services import ServiceContainer


def _print_pending(batch: dict) -> None:
    for call in batch["calls"]:
        if call["permission"]["action"] != "ask" or call.get("outcome") is not None:
            continue
        print(f"  [待审批工具] {call['name']}({call['args']})")
        preview = call.get("preview")
        if preview and preview.get("type") == "code_diff":
            print(
                f"  [拟议改动] {preview['path']} "
                f"+{preview['additions']} -{preview['deletions']}"
            )


def render_events(
    items: list,
    services: ServiceContainer,
    max_turns: int = 10,
    session_id: int | None = None,
    pending_approval: dict | None = None,
    on_checkpoint: Callable[[], None] | None = None,
    on_pending: Callable[[dict | None], None] | None = None,
) -> None:
    """把事件流呈现到终端：文本逐字打印，工具调用与结果按行展示"""
    remaining_turns = (
        pending_approval["remaining_turns"]
        if pending_approval is not None
        else max_turns
    )
    pending = pending_approval
    if pending is not None:
        print("[恢复上次未完成的工具审批]")
        _print_pending(pending)

    while True:
        printing_text = False

        if pending is None:
            def approval_checkpoint(batch: dict) -> None:
                nonlocal pending
                if on_pending is not None:
                    on_pending(batch)
                pending = batch

            for event in stream_events(
                items,
                services,
                remaining_turns,
                on_approval=approval_checkpoint,
                on_checkpoint=on_checkpoint,
                session_id=session_id,
            ):
                if event["type"] == "text_delta":
                    # 首个文本片段到达时才打印前缀，避免纯工具调用也输出前缀。
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
                    if on_pending is not None:
                        on_pending(None)
                    elif on_checkpoint is not None:
                        on_checkpoint()
                    if printing_text:
                        print()
                elif event["type"] == "max_turns":
                    if on_pending is not None:
                        on_pending(None)
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
        if on_pending is not None:
            on_pending(None)
        elif on_checkpoint is not None:
            on_checkpoint()
        for result in results:
            if result["id"] in decided_ids:
                print(f"  [返回结果] {result['content']}")
        remaining_turns = pending["remaining_turns"]
        pending = None
