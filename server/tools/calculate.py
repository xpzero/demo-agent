SCHEMA = {
    "type": "function",
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
    "strict": False,
}


def run(args: dict) -> str:
    return str(eval(args["expression"]))
