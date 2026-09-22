"""上传文件垃圾回收：仅处理受控根目录下的服务端 file_id 目录。"""

import json
import logging
import re
import time
from pathlib import Path

from database import DATABASE_PATH, Database

from .storage import FILE_ROOT

logger = logging.getLogger(__name__)
FILE_ID_PATTERN = re.compile(r"^file_[0-9a-f]{32}$")
UNREFERENCED_TTL_SECONDS = 24 * 60 * 60
TEMPORARY_TTL_SECONDS = 60 * 60


def _valid_directory(root: Path, directory: Path) -> bool:
    return (
        directory.parent.resolve() == root.resolve()
        and FILE_ID_PATTERN.fullmatch(directory.name) is not None
        and not directory.is_symlink()
        and directory.is_dir()
    )


def _remove_flat_directory(root: Path, directory: Path) -> bool:
    """删除服务生成的扁平目录；遇到真实子目录时拒绝递归删除。"""
    if not directory.exists():
        return False
    if not _valid_directory(root, directory):
        raise ValueError(f"拒绝删除不受控目录: {directory}")
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            raise ValueError(f"拒绝递归删除意外子目录: {child}")
        child.unlink(missing_ok=True)
    directory.rmdir()
    return True


def cleanup_stale_temporary_files(
    *, root: Path, now: float, ttl_seconds: int = TEMPORARY_TTL_SECONDS
) -> int:
    removed = 0
    if not root.exists():
        return removed
    cutoff = now - ttl_seconds
    for directory in root.iterdir():
        if not _valid_directory(root, directory):
            continue
        for child in directory.iterdir():
            if not child.name.endswith((".uploading", ".parsing")):
                continue
            if child.stat(follow_symlinks=False).st_mtime >= cutoff:
                continue
            child.unlink(missing_ok=True)
            removed += 1
    return removed


def cleanup_expired_unreferenced_files(
    *,
    database: Database,
    root: Path,
    now: float,
    ttl_seconds: int = UNREFERENCED_TTL_SECONDS,
) -> int:
    removed = 0
    before = now - ttl_seconds
    for record in database.list_expired_unreferenced_files(before):
        file_id = record["id"]
        if FILE_ID_PATTERN.fullmatch(file_id) is None:
            logger.error("跳过非法 file_id 的清理: %s", file_id)
            continue
        if not database.delete_file_if_unreferenced(file_id):
            continue
        try:
            _remove_flat_directory(root, root / file_id)
        except Exception:
            logger.exception("文件记录已删除，但磁盘目录清理失败: %s", file_id)
        removed += 1
    return removed


def cleanup_orphan_file_directories(
    *,
    database: Database,
    root: Path,
    now: float,
    ttl_seconds: int = UNREFERENCED_TTL_SECONDS,
) -> int:
    removed = 0
    if not root.exists():
        return removed
    known_ids = database.all_file_ids()
    cutoff = now - ttl_seconds
    for directory in root.iterdir():
        if not _valid_directory(root, directory):
            continue
        if directory.name in known_ids:
            continue
        if directory.stat().st_mtime >= cutoff:
            continue
        _remove_flat_directory(root, directory)
        removed += 1
    return removed


def cleanup_files(
    *,
    database: Database,
    root: Path = FILE_ROOT,
    now: float | None = None,
) -> dict:
    current_time = time.time() if now is None else now
    result = {
        "temporary_files_removed": 0,
        "unreferenced_files_removed": 0,
        "orphan_directories_removed": 0,
        "errors": [],
    }
    operations = (
        (
            "temporary_files_removed",
            lambda: cleanup_stale_temporary_files(root=root, now=current_time),
        ),
        (
            "unreferenced_files_removed",
            lambda: cleanup_expired_unreferenced_files(
                database=database, root=root, now=current_time
            ),
        ),
        (
            "orphan_directories_removed",
            lambda: cleanup_orphan_file_directories(
                database=database, root=root, now=current_time
            ),
        ),
    )
    for key, operation in operations:
        try:
            result[key] = operation()
        except Exception as error:
            logger.exception("文件清理步骤失败: %s", key)
            result["errors"].append(f"{key}: {type(error).__name__}: {error}")
    return result


def main() -> None:
    database = Database(DATABASE_PATH)
    database.initialize()
    print(json.dumps(cleanup_files(database=database), ensure_ascii=False))


if __name__ == "__main__":
    main()
