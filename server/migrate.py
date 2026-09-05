import os

import psycopg
from dotenv import load_dotenv

from schema import SchemaError, apply_migrations


def main() -> None:
    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("缺少 DATABASE_URL")

    try:
        applied = apply_migrations(database_url)
    except SchemaError as error:
        raise SystemExit(str(error)) from error
    except psycopg.Error as error:
        raise SystemExit(f"数据库 migration 失败：{type(error).__name__}") from error

    if applied:
        for name in applied:
            print(f"[已执行 migration] {name}")
    else:
        print("[数据库 schema 已是最新版本]")


if __name__ == "__main__":
    main()
