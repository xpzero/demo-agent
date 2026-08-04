# 工具实现

# 提供给模型的工具声明，需与下方 TOOL_HANDLERS 保持一致
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "计算一个数学表达式的结果",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "待计算的表达式，例如 (38 - 12) * 3",
                    },
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市今天的天气",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名，例如 北京",
                    },
                },
                "required": ["city"],
            },
        },
    },
]


def calculate(args: dict) -> str:
    return str(eval(args["expression"]))


def get_weather(args: dict) -> str:
    return f"{args['city']}今天晴，最高气温38℃"


# 工具名 → 实现，新增工具时需同步补充上面的 TOOLS 声明
TOOL_HANDLERS = {
    "calculate": calculate,
    "get_weather": get_weather,
}


def execute_tool(name: str, args: dict) -> str:
    """按名字分发到具体工具，并把异常统一转成文本结果。

    args 由模型生成，属于不可信输入：字段缺失、表达式非法都有可能。
    这里兜住所有异常并把错误信息回传给模型，它就有机会自行纠正后重试，
    而不是让整个 agent 循环直接崩掉。
    """
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"未知工具：{name}"

    try:
        return handler(args)
    except Exception as e:
        return f"{name} 执行出错：{e}"
