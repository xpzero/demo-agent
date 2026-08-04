import json

from dotenv import load_dotenv
from openai import OpenAI

from tools import TOOLS, execute_tool

# 自动读取.env文件
load_dotenv()

# 自动从环境变量中读取OPENAI_API_KEY和OPENAI_BASE_URL
client = OpenAI()

MODEL = "gpt-5.6-luna"


def run_agent(user_input: str, max_turns: int = 10):
    messages = [
        {"role": "system", "content": "你是一个有用的助手，可以调用工具来帮助用户。"},
        {"role": "user", "content": user_input},
    ]

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


def main():
    run_agent("北京今天天气怎么样？顺便帮我算一下 (38 - 12) * 3")


if __name__ == "__main__":
    main()
