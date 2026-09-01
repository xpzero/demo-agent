from pathlib import Path

# 文件工具的根目录固定为项目目录，模型只能在此范围内读写
# 层级：paths.py → files/ → tools/ → 项目根
ROOT = Path(__file__).resolve().parents[2]

# 禁止访问的路径：.env 含 API key，读出来会被带进模型上下文；.git 是仓库元数据
BLOCKED = {".env", ".git", ".sessions"}


def resolve(path: str) -> Path:
    """把模型给的路径解析为项目内的绝对路径，越界或命中黑名单则拒绝"""
    target = (ROOT / path).resolve()

    if not target.is_relative_to(ROOT):
        raise ValueError(f"路径越界，只能访问项目目录内的文件：{path}")

    relative = target.relative_to(ROOT)
    if relative.parts and relative.parts[0] in BLOCKED:
        raise ValueError(f"该路径禁止访问：{path}")

    return target
