from dataclasses import dataclass

from .service import SessionService
from .session import Session

HELP = """可用命令：
  /new          新建并切换到新会话
  /list         列出全部会话
  /switch <id>  切换到指定会话
  /del <id>     删除指定会话
  /help         显示本说明
  quit          退出"""


@dataclass(frozen=True)
class CommandResult:
    handled: bool
    current: Session


def handle(
    user_input: str,
    service: SessionService,
    current: Session,
    system_prompt: str,
) -> CommandResult:
    """尝试消费 CLI 命令，并返回命令执行后的当前会话。"""
    if not user_input.startswith("/"):
        return CommandResult(False, current)

    parts = user_input.split()
    name, args = parts[0], parts[1:]

    if name == "/new":
        current = service.create(system_prompt)
        print(f"[已新建并切换到 {current.id} 号会话]")
    elif name == "/list":
        _print_listing(service, current)
    elif name == "/switch":
        current = _switch(service, current, args)
    elif name == "/del":
        _delete(service, current, args)
    elif name == "/help":
        print(HELP)
    else:
        print(f"[未知命令 {name}]\n{HELP}")

    return CommandResult(True, current)


def _print_listing(service: SessionService, current: Session) -> None:
    for session in service.list_sessions():
        mark = "*" if session.id == current.id else " "
        print(
            f"{mark} {session.id:>3}  {session.summary}  "
            f"({session.message_count} 个上下文项)"
        )


def _parse_id(args: list[str]) -> int | None:
    if len(args) != 1 or not args[0].isdigit():
        return None
    session_id = int(args[0])
    return session_id if session_id > 0 else None


def _switch(
    service: SessionService, current: Session, args: list[str]
) -> Session:
    session_id = _parse_id(args)
    if session_id is None:
        print("[用法：/switch <会话号>]")
        return current
    session = service.get(session_id)
    if session is None:
        print(f"[没有 {session_id} 号会话]")
        return current

    print(f"[已切换到 {session_id} 号会话]")

    # 回显上一轮，切回来时不必凭记忆想这条线聊到哪了
    exchange = session.last_exchange
    if exchange is None:
        print("  (还没聊过)")
        return session

    user_text, assistant_text = exchange
    print(f"  上次 你: {user_text}")
    print(f"     Agent: {assistant_text}")
    return session


def _delete(service: SessionService, current: Session, args: list[str]) -> None:
    session_id = _parse_id(args)
    if session_id is None:
        print("[用法：/del <会话号>]")
        return

    if session_id == current.id:
        print("[不能删除当前会话，请先 /switch 到别处]")
        return

    session = service.get(session_id)
    if session is None:
        print(f"[没有 {session_id} 号会话]")
        return

    deleted = service.delete(session_id, session.revision)
    print(f"[已删除 {session_id} 号会话]" if deleted else f"[没有 {session_id} 号会话]")
