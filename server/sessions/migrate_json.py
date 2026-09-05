import argparse
import json
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from schema import SchemaError, assert_connection_schema_current

from .postgres import _plain_session
from .session import Session


def _reject_json_constant(value: str):
    raise ValueError(f"不支持的 JSON 常量：{value}")


def load_sessions(source: Path) -> list[Session]:
    if not source.is_dir():
        raise ValueError(f"Session 目录不存在：{source}")

    sessions = []
    seen_ids = set()
    for path in sorted(source.glob("*.json")):
        if not path.stem.isdigit() or int(path.stem) <= 0:
            raise ValueError(f"Session 文件名必须是正整数：{path.name}")
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
            )
            session = Session.from_dict(payload)
            _plain_session(session)
        except Exception as error:
            raise ValueError(f"Session 文件无效：{path.name}：{error}") from error
        if session.id != int(path.stem):
            raise ValueError(f"Session 文件名与内容 id 不一致：{path.name}")
        if session.id in seen_ids:
            raise ValueError(f"Session id 重复：{session.id}")
        seen_ids.add(session.id)
        sessions.append(session)
    return sessions


def import_sessions(database_url: str, sessions: list[Session]) -> None:
    try:
        with psycopg.connect(database_url) as connection:
            assert_connection_schema_current(connection)
            connection.execute("LOCK TABLE public.sessions IN ACCESS EXCLUSIVE MODE")

            count = connection.execute(
                "SELECT count(*) FROM public.sessions"
            ).fetchone()[0]
            if count:
                raise ValueError("目标 sessions 表已有数据，请使用空表执行导入")

            for session in sessions:
                data = session.to_dict()
                connection.execute(
                    """
                    INSERT INTO public.sessions (id, items, pending_approval, revision)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        session.id,
                        Jsonb(data["items"]),
                        None
                        if session.pending_approval is None
                        else Jsonb(session.pending_approval),
                        session.revision,
                    ),
                )

            if sessions:
                max_id = max(session.id for session in sessions)
                connection.execute(
                    """
                    SELECT setval(
                        pg_get_serial_sequence('public.sessions', 'id'),
                        GREATEST(%s, (SELECT last_value FROM public.sessions_id_seq)),
                        true
                    )
                    """,
                    (max_id,),
                )
    except (psycopg.Error, SchemaError) as error:
        raise ValueError("导入 Session 到 PostgreSQL 失败") from error


def main() -> None:
    parser = argparse.ArgumentParser(description="把旧 Session JSON 导入 PostgreSQL")
    parser.add_argument("--source", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    try:
        sessions = load_sessions(args.source)
    except ValueError as error:
        raise SystemExit(str(error)) from error

    print(f"[Session JSON 校验通过] {len(sessions)} 个文件")
    if args.dry_run:
        return

    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("缺少 DATABASE_URL")
    try:
        import_sessions(database_url, sessions)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"[Session 导入完成] {len(sessions)} 条")


if __name__ == "__main__":
    main()
