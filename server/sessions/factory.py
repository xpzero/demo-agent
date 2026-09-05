import os

from dotenv import load_dotenv

from .postgres import PostgresSessionService
from .service import SessionStorageError


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise SessionStorageError(f"{name} 必须是正整数") from error
    if value <= 0:
        raise SessionStorageError(f"{name} 必须是正整数")
    return value


def create_default_session_service() -> PostgresSessionService:
    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SessionStorageError("缺少 DATABASE_URL")

    min_size = _positive_int("DB_POOL_MIN_SIZE", 1)
    max_size = _positive_int("DB_POOL_MAX_SIZE", 5)
    timeout = _positive_int("DB_POOL_TIMEOUT_SECONDS", 5)
    if min_size > max_size:
        raise SessionStorageError("DB_POOL_MIN_SIZE 不能大于 DB_POOL_MAX_SIZE")

    return PostgresSessionService(
        database_url,
        min_size=min_size,
        max_size=max_size,
        timeout=float(timeout),
    )
