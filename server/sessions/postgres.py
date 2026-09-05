import json

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from schema import SchemaError, assert_schema_current

from .service import (
    SessionDataError,
    SessionNotFound,
    SessionRevisionConflict,
    SessionStorageError,
    SessionSummary,
)
from .session import Session


def _contains_nul(value) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, list):
        return any(_contains_nul(entry) for entry in value)
    if isinstance(value, dict):
        return any(_contains_nul(key) or _contains_nul(entry) for key, entry in value.items())
    return False


def _plain_session(session: Session) -> tuple[list, dict | None]:
    data = session.to_dict()
    try:
        validated = Session.from_dict(data)
    except ValueError as error:
        raise SessionDataError(f"Session {session.id} 包含无效状态") from error
    items = validated.items
    pending = validated.pending_approval
    if _contains_nul({"items": items, "pending_approval": pending}):
        raise SessionDataError(f"Session {session.id} 包含 PostgreSQL JSONB 不支持的 NUL 字符")
    try:
        # PostgreSQL JSONB rejects NUL and non-JSON numeric values such as NaN.
        encoded = json.dumps(
            {"items": items, "pending_approval": pending},
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise SessionDataError(f"Session {session.id} 包含无法保存的内容") from error
    return items, pending


def _session_from_row(row) -> Session:
    try:
        return Session.from_dict(
            {
                "id": row[0],
                "items": row[1],
                "pending_approval": row[2],
                "revision": row[3],
            }
        )
    except (TypeError, ValueError) as error:
        raise SessionDataError(f"数据库中的 Session {row[0]} 格式无效") from error


class PostgresSessionService:
    def __init__(
        self,
        database_url: str,
        *,
        min_size: int = 1,
        max_size: int = 5,
        timeout: float = 5.0,
        check_schema: bool = True,
    ):
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            check=ConnectionPool.check_connection,
            open=False,
        )
        try:
            self._pool.open(wait=True, timeout=timeout)
            if check_schema:
                assert_schema_current(self._pool)
        except SchemaError as error:
            self._pool.close()
            raise SessionStorageError(f"无法初始化 Session 数据库：{error}") from error
        except (psycopg.Error, TimeoutError) as error:
            self._pool.close()
            raise SessionStorageError(
                f"无法连接 Session 数据库：{type(error).__name__}"
            ) from error

    def create(self, system_prompt: str) -> Session:
        items = [{"role": "system", "content": system_prompt}]
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    INSERT INTO public.sessions (items)
                    VALUES (%s)
                    RETURNING id, items, pending_approval, revision
                    """,
                    (Jsonb(items),),
                ).fetchone()
            return _session_from_row(row)
        except psycopg.Error as error:
            raise SessionStorageError("创建 Session 失败") from error

    def get(self, session_id: int) -> Session | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT id, items, pending_approval, revision
                    FROM public.sessions
                    WHERE id = %s
                    """,
                    (session_id,),
                ).fetchone()
            return None if row is None else _session_from_row(row)
        except psycopg.Error as error:
            raise SessionStorageError(f"读取 Session {session_id} 失败") from error

    def list_sessions(self) -> list[SessionSummary]:
        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, items, pending_approval, revision
                    FROM public.sessions
                    ORDER BY id
                    """
                ).fetchall()
            return [
                SessionSummary(
                    id=session.id,
                    summary=session.summary,
                    message_count=len(session.items),
                    revision=session.revision,
                )
                for session in (_session_from_row(row) for row in rows)
            ]
        except psycopg.Error as error:
            raise SessionStorageError("列出 Session 失败") from error

    def save(self, session: Session) -> None:
        items, pending = _plain_session(session)
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    UPDATE public.sessions
                    SET items = %s,
                        pending_approval = %s,
                        revision = revision + 1,
                        updated_at = clock_timestamp()
                    WHERE id = %s AND revision = %s
                    RETURNING revision
                    """,
                    (
                        Jsonb(items),
                        None if pending is None else Jsonb(pending),
                        session.id,
                        session.revision,
                    ),
                ).fetchone()
                if row is None:
                    exists = connection.execute(
                        "SELECT 1 FROM public.sessions WHERE id = %s", (session.id,)
                    ).fetchone()
                    if exists is None:
                        raise SessionNotFound(session.id)
                    raise SessionRevisionConflict(session.id)
            session.revision = row[0]
        except (SessionNotFound, SessionRevisionConflict):
            raise
        except psycopg.Error as error:
            raise SessionStorageError(f"保存 Session {session.id} 失败") from error

    def delete(self, session_id: int, expected_revision: int) -> bool:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    DELETE FROM public.sessions
                    WHERE id = %s AND revision = %s
                    RETURNING id
                    """,
                    (session_id, expected_revision),
                ).fetchone()
                if row is not None:
                    return True
                exists = connection.execute(
                    "SELECT 1 FROM public.sessions WHERE id = %s", (session_id,)
                ).fetchone()
                if exists is not None:
                    raise SessionRevisionConflict(session_id)
                return False
        except SessionRevisionConflict:
            raise
        except psycopg.Error as error:
            raise SessionStorageError(f"删除 Session {session_id} 失败") from error

    def close(self) -> None:
        self._pool.close()
