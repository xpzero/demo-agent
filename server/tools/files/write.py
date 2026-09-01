from difflib import ndiff
from hashlib import sha256
from pathlib import Path
from threading import Lock

from services.permission import PermissionRequest

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

_lock_guard = Lock()
_target_locks: dict[str, Lock] = {}


def _content_hash(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def _target_lock(target: Path) -> Lock:
    key = str(target)
    with _lock_guard:
        return _target_locks.setdefault(key, Lock())


def permission_requests(args: dict) -> tuple[PermissionRequest, ...]:
    target = resolve(args["path"])
    return (PermissionRequest("write", target.relative_to(ROOT).as_posix()),)


def preview(args: dict) -> dict:
    """生成完整内容覆盖前的逐行 Diff，不修改目标文件。"""
    target = resolve(args["path"])
    content = args["content"]
    if not isinstance(content, str):
        raise TypeError("content 必须是字符串")

    existed = target.exists()
    old_content = target.read_text(encoding="utf-8") if existed else ""
    lines = []
    additions = 0
    deletions = 0

    for line in ndiff(old_content.splitlines(), content.splitlines()):
        marker, text = line[:2], line[2:]
        if marker == "? ":
            continue
        if marker == "+ ":
            kind = "added"
            additions += 1
        elif marker == "- ":
            kind = "removed"
            deletions += 1
        else:
            kind = "context"
        lines.append({"kind": kind, "text": text})

    return {
        "type": "code_diff",
        "path": str(target.relative_to(ROOT)),
        "additions": additions,
        "deletions": deletions,
        "lines": lines,
        "_guard": {
            "canonical_path": str(target),
            "existed": existed,
            "content_hash": _content_hash(old_content),
        },
    }


def run_approved(args: dict, guard: dict | None) -> str:
    """确认目标与预览时一致后写入，避免批准过期的 Diff。"""
    if guard is None:
        raise ValueError("缺少文件预览校验信息")

    approved_target = Path(guard["canonical_path"])
    with _target_lock(approved_target):
        target = resolve(args["path"])
        if str(target) != guard["canonical_path"]:
            raise ValueError("文件路径在审批后发生变化，请重新发起修改")

        existed = target.exists()
        content = target.read_text(encoding="utf-8") if existed else ""
        if existed != guard["existed"] or _content_hash(content) != guard["content_hash"]:
            raise ValueError("文件内容在审批后发生变化，请重新查看 Diff")

        return _write(target, args["content"])


def _write(target: Path, content) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(content, str):
        raise TypeError("content 必须是字符串")
    target.write_text(content, encoding="utf-8")
    return f"已写入 {target.relative_to(ROOT)}（{len(content)} 字符）"


def run(args: dict) -> str:
    target = resolve(args["path"])
    return _write(target, args["content"])
