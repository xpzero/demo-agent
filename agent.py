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
    """流式：文本边收边打印，工具调用则需跨 chunk 拼接完整参数后才能执行。

    流式响应不返回完整对象，而是一串只带增量的 chunk，形如：

        delta.content = "北"                              # 文本的一个片段
        delta.tool_calls[0].function.arguments = '{"ci'   # 参数的一个片段

    所以文本要边收边拼，工具调用更要等参数拼完整才能 json.loads。
    """
    for _ in range(max_turns):
        stream = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            stream=True,
        )

        content = ""
        # 工具调用会被拆散在多个 chunk 里；部分网关对多个调用一律返回 index=0，
        # 因此按 id 归拢：id 相同即同一个调用，不依赖 index，也不怕片段交错送达
        calls: list[dict] = []

        for chunk in stream:
            # 每个 chunk 只携带增量，所以取 delta 而不是 message
            delta = chunk.choices[0].delta

            if delta.content:
                # 首个文本片段到达时才打印前缀，避免纯工具调用的轮次也输出 "Agent: "
                if not content:
                    print("Agent: ", end="", flush=True)
                print(delta.content, end="", flush=True)
                # 除了实时打印，还要留一份完整文本用于回填上下文
                content += delta.content

            # 为 None 说明这个 chunk 不含工具调用信息
            for tc in delta.tool_calls or []:
                # 出现没见过的 id 就是一个新调用（同一个工具被调多次时 id 也不同）
                if not calls or (tc.id and all(c["id"] != tc.id for c in calls)):
                    calls.append({"id": tc.id or "", "name": "", "arguments": ""})

                # 带 id 的片段归回它自己的调用，不带 id 的归到最近一个调用
                slot = next((c for c in calls if tc.id and c["id"] == tc.id), calls[-1])
                if tc.function and tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    # 参数是逐段送来的 JSON 字符串碎片，只能累加，不能覆盖
                    slot["arguments"] += tc.function.arguments

        if content:
            print()

        # 没有工具调用 → 模型给出了最终回复，与非流式一样要写回上下文
        if not calls:
            messages.append({"role": "assistant", "content": content})
            return

        # 流式拿不到现成的 message 对象，需按非流式的结构自己拼一条 assistant 消息，
        # 否则下一轮请求时模型不知道自己发起过哪些调用，后面的 tool 消息也无从对应
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
