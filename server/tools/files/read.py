from services.permission import PermissionRequest

from .paths import ROOT, resolve

SCHEMA = {
    "type": "function",
    "name": "read_file",
    "description": "读取项目内某个文件的全部内容",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对项目根目录的路径，例如 tools/files/read.py",
            },
        },
        "required": ["path"],
    },
    "strict": False,
}


def permission_requests(args: dict) -> tuple[PermissionRequest, ...]:
    target = resolve(args["path"])
    return (PermissionRequest("read", target.relative_to(ROOT).as_posix()),)


def run(args: dict) -> str:
    return resolve(args["path"]).read_text(encoding="utf-8")
