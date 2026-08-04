# 工具实现

# 提供给模型的工具声明，需与 execute_tool 中的分支保持一致
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


def execute_tool(name: str, args: dict) -> str:
    if name == "calculate":
        try:
            return str(eval(args["expression"]))
        except Exception as e:
            return f"计算出错：{e}"
    elif name == "get_weather":
        city = args["city"]
        return f"{city}今天晴，最高气温38℃"
    return f"未知工具：{name}"
