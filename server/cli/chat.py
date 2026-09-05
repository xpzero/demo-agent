from agent import SYSTEM_PROMPT
from services import ServiceContainer
from sessions import Session, SessionError, SessionService, handle_command

from .render import render_events


def _reload_after_error(
    session_service: SessionService, session_id: int, error: SessionError
) -> Session | None:
    print(f"[会话操作失败] {error}")
    try:
        current = session_service.get(session_id)
    except SessionError as reload_error:
        print(f"[重新加载会话失败] {reload_error}")
        return None
    if current is None:
        print(f"[没有 {session_id} 号会话]")
    return current


def run_agent(
    user_input: str, services: ServiceContainer, max_turns: int = 10
) -> None:
    """单次任务：跑完一个输入的工具循环就结束"""
    items = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_input},
    ]
    render_events(items, services, max_turns)


def chat(services: ServiceContainer, session_service: SessionService) -> None:
    """多轮对话：各会话的 Items 相互隔离，每轮结束后持久化当前会话"""
    current = session_service.create(SYSTEM_PROMPT)

    print("Agent 已启动。/help 查看命令，quit 退出")

    while True:
        def checkpoint_current() -> None:
            session_service.save(current)

        def save_pending(batch: dict | None) -> None:
            current.pending_approval = batch
            session_service.save(current)

        if current.pending_approval is not None:
            try:
                render_events(
                    current.items,
                    services,
                    session_id=current.id,
                    pending_approval=current.pending_approval,
                    on_checkpoint=checkpoint_current,
                    on_pending=save_pending,
                )
            except SessionError as error:
                restored = _reload_after_error(session_service, current.id, error)
                if restored is None:
                    return
                current = restored
                continue
            restored = session_service.get(current.id)
            if restored is None:
                print(f"[没有 {current.id} 号会话]")
                return
            current = restored
            continue

        # 提示符带会话号，随时能看出自己在哪条线上
        user_input = input(f"[{current.id}] 你: ").strip()
        if user_input.lower() in ("quit", "exit", "q"):
            break

        # 斜杠命令由 sessions 包消费，不进入上下文
        try:
            result = handle_command(user_input, session_service, current, SYSTEM_PROMPT)
        except SessionError as error:
            restored = _reload_after_error(session_service, current.id, error)
            if restored is None:
                return
            current = restored
            continue
        current = result.current
        if result.handled:
            continue

        try:
            current.items.append({"role": "user", "content": user_input})
            session_service.save(current)
            render_events(
                current.items,
                services,
                session_id=current.id,
                on_checkpoint=checkpoint_current,
                on_pending=save_pending,
            )
        except SessionError as error:
            restored = _reload_after_error(session_service, current.id, error)
            if restored is None:
                return
            current = restored
            continue
        restored = session_service.get(current.id)
        if restored is None:
            print(f"[没有 {current.id} 号会话]")
            return
        current = restored
