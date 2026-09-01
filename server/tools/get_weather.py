from services.permission import PermissionRequest


SCHEMA = {
    "type": "function",
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
    "strict": False,
}


def permission_requests(args: dict) -> tuple[PermissionRequest, ...]:
    return (PermissionRequest("get_weather", str(args.get("city", "*"))),)


def run(args: dict) -> str:
    return f"{args['city']}今天晴，最高气温38℃"
