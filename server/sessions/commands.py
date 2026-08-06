from .manager import SessionManager

HELP = """可用命令：
  /new          新建并切换到新会话
  /list         列出全部会话
  /switch <id>  切换到指定会话
  /del <id>     删除指定会话
  /help         显示本说明
  quit          退出"""


def handle(user_input: str, manager: SessionManager) -> bool:
    """尝试把输入当命令处理；返回 True 表示这次输入已被消费，不该发给模型"""
    if not user_input.startswith("/"):
        return False

    parts = user_input.split()
    name, args = parts[0], parts[1:]

    if name == "/new":
        print(f"[已新建并切换到 {manager.new().id} 号会话]")
    elif name == "/list":
        _print_listing(manager)
    elif name == "/switch":
        _switch(manager, args)
    elif name == "/del":
        _delete(manager, args)
    elif name == "/help":
        print(HELP)
    else:
        print(f"[未知命令 {name}]\n{HELP}")

    return True


def _print_listing(manager: SessionManager) -> None:
    for is_current, session in manager.listing():
        mark = "*" if is_current else " "
        print(f"{mark} {session.id:>3}  {session.summary}  ({len(session.messages)} 条消息)")


def _parse_id(args: list[str]) -> int | None:
    if len(args) != 1 or not args[0].lstrip("-").isdigit():
        return None
    return int(args[0])


def _switch(manager: SessionManager, args: list[str]) -> None:
    session_id = _parse_id(args)
    if session_id is None:
        print("[用法：/switch <会话号>]")
        return
    if not manager.switch(session_id):
        print(f"[没有 {session_id} 号会话]")
        return

    print(f"[已切换到 {session_id} 号会话]")

    # 回显上一轮，切回来时不必凭记忆想这条线聊到哪了
    exchange = manager.current.last_exchange
    if exchange is None:
        print("  (还没聊过)")
        return

    user_text, assistant_text = exchange
    print(f"  上次 你: {user_text}")
    print(f"     Agent: {assistant_text}")


def _delete(manager: SessionManager, args: list[str]) -> None:
    session_id = _parse_id(args)
    if session_id is None:
        print("[用法：/del <会话号>]")
        return

    error = manager.delete(session_id)
    print(f"[{error}]" if error else f"[已删除 {session_id} 号会话]")
