from .paths import resolve

SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取项目内某个文件的全部内容",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对项目根目录的路径，例如 tools/calculate.py",
                },
            },
            "required": ["path"],
        },
    },
}


def run(args: dict) -> str:
    return resolve(args["path"]).read_text(encoding="utf-8")
