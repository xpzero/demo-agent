from agent import SYSTEM_PROMPT
from sessions import SessionManager, handle_command

from .render import render_events


def run_agent(user_input: str, max_turns: int = 10) -> None:
    """单次任务：跑完一个输入的工具循环就结束"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_input},
    ]
    render_events(messages, max_turns)


def chat() -> None:
    """多轮对话：各会话的 messages 相互隔离，每轮结束后持久化当前会话"""
    manager = SessionManager(SYSTEM_PROMPT)

    print("Agent 已启动。/help 查看命令，quit 退出")

    while True:
        # 提示符带会话号，随时能看出自己在哪条线上
        user_input = input(f"[{manager.current.id}] 你: ").strip()
        if user_input.lower() in ("quit", "exit", "q"):
            break

        # 斜杠命令由 sessions 包消费，不进入上下文
        if handle_command(user_input, manager):
            continue

        messages = manager.current.messages
        messages.append({"role": "user", "content": user_input})
        render_events(messages)
        manager.save_current()
