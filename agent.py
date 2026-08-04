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


def run_tool_loop_stream(messages: list, max_turns: int = 10) -> None:
    """流式：文本边收边打印，工具调用则需跨 chunk 拼接完整参数后才能执行"""
    for _ in range(max_turns):
        stream = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            stream=True,
        )

        content = ""
        # 工具调用会被拆散在多个 chunk 里；部分网关对多个调用一律返回 index=0，
        # 因此以「出现新的 id」作为新调用的开始，后续无 id 的 chunk 追加到最近一个调用
        calls: list[dict] = []

        for chunk in stream:
            delta = chunk.choices[0].delta

            if delta.content:
                if not content:
                    print("Agent: ", end="", flush=True)
                print(delta.content, end="", flush=True)
                content += delta.content

            for tc in delta.tool_calls or []:
                if not calls or (tc.id and tc.id != calls[-1]["id"]):
                    calls.append({"id": tc.id or "", "name": "", "arguments": ""})

                slot = calls[-1]
                if tc.function and tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    slot["arguments"] += tc.function.arguments

        if content:
            print()

        # 没有工具调用 → 任务完成
        if not calls:
            return

        # 流式拿不到现成的 message 对象，需要自己拼一条 assistant 消息回填上下文
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

        execute_tool_calls(
            messages,
            [(slot["id"], slot["name"], slot["arguments"]) for slot in calls],
        )

    print("[达到最大轮次，停止]")


def run_agent(user_input: str, max_turns: int = 10, stream: bool = False) -> None:
    """单次任务：跑完一个输入的工具循环就结束"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_input},
    ]
    loop = run_tool_loop_stream if stream else run_tool_loop
    loop(messages, max_turns)


def chat(stream: bool = False) -> None:
    """多轮对话：复用同一份 messages 持续接收用户输入"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    loop = run_tool_loop_stream if stream else run_tool_loop

    print("Agent 已启动，输入 quit 退出")

    while True:
        user_input = input("你: ")
        if user_input.strip().lower() in ("quit", "exit", "q"):
            break

        messages.append({"role": "user", "content": user_input})
        loop(messages)
