import json

from dotenv import load_dotenv
from openai import OpenAI

from tools import TOOLS, execute_tool

# 自动读取.env文件
load_dotenv()

# 自动从环境变量中读取OPENAI_API_KEY和OPENAI_BASE_URL
client = OpenAI()

MODEL = "gpt-5.6-luna"

SYSTEM_PROMPT = "你是一个有用的助手，可以调用工具来帮助用户。"


def run_tool_loop(messages: list, max_turns: int = 10) -> None:
    """反复请求模型并执行它要求的工具，直到模型给出最终文本回复。

    单轮任务和多轮对话都要走这套流程，因此抽出来共用；messages 会被原地追加。
    """
    for _ in range(max_turns):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
        )

        message = response.choices[0].message

        # 把助手回复加入上下文
        messages.append(message)

        # 没有工具调用 → 任务完成
        if not message.tool_calls:
            print(f"Agent: {message.content}")
            return

        # 有工具调用 → 逐个执行
        for tool_call in message.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)
            print(f"  [工具调用] {name}({args})")

            result = execute_tool(name, args)
            print(f"  [返回结果] {result}")

            # 把工具结果喂回去
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
            )

    print("[达到最大轮次，停止]")


def run_agent(user_input: str, max_turns: int = 10) -> None:
    """单次任务：跑完一个输入的工具循环就结束"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_input},
    ]
    run_tool_loop(messages, max_turns)


def chat() -> None:
    """多轮对话：复用同一份 messages 持续接收用户输入"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    print("Agent 已启动，输入 quit 退出")

    while True:
        user_input = input("你: ")
        if user_input.strip().lower() in ("quit", "exit", "q"):
            break

        messages.append({"role": "user", "content": user_input})
        run_tool_loop(messages)
