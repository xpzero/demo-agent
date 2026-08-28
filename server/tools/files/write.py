from .paths import ROOT, resolve

SCHEMA = {
    "type": "function",
    "name": "write_file",
    "description": "把内容写入项目内的文件，文件已存在时会覆盖原有内容",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对项目根目录的路径，例如 notes/todo.md",
            },
            "content": {
                "type": "string",
                "description": "要写入的完整文本内容",
            },
        },
        "required": ["path", "content"],
    },
    "strict": False,
}


def run(args: dict) -> str:
    target = resolve(args["path"])
    target.parent.mkdir(parents=True, exist_ok=True)

    content = args["content"]
    target.write_text(content, encoding="utf-8")

    # 回一条明确的结果，模型才知道写入成功以及落在了哪里
    return f"已写入 {target.relative_to(ROOT)}（{len(content)} 字符）"
