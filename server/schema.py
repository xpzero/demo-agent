import hashlib
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool


MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_LOCK_ID = 2_026_090_401


class SchemaError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def load_migrations() -> list[Migration]:
    migrations = []
    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        version_text, _, _ = path.name.partition("_")
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=int(version_text),
                name=path.name,
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )

    versions = [migration.version for migration in migrations]
    if len(versions) != len(set(versions)):
        raise SchemaError("数据库 migration 版本重复")
    if not migrations:
        raise SchemaError(f"没有在 {MIGRATIONS_DIR} 中找到 migration")
    return migrations


def apply_migrations(database_url: str) -> list[str]:
    migrations = load_migrations()
    applied_now = []

    with psycopg.connect(database_url) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS public.schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        rows = connection.execute(
            "SELECT version, name, checksum FROM public.schema_migrations ORDER BY version"
        ).fetchall()
        applied = {row[0]: (row[1], row[2]) for row in rows}
        expected_versions = {migration.version for migration in migrations}
        unknown = sorted(set(applied) - expected_versions)
        if unknown:
            raise SchemaError(f"数据库包含当前代码无法识别的 migration：{unknown}")

        for migration in migrations:
            recorded = applied.get(migration.version)
            if recorded is not None:
                if recorded != (migration.name, migration.checksum):
                    raise SchemaError(
                        f"已执行的 migration {migration.version} 与当前文件不一致"
                    )
                continue

            connection.execute(migration.sql)
            connection.execute(
                """
                INSERT INTO public.schema_migrations (version, name, checksum)
                VALUES (%s, %s, %s)
                """,
                (migration.version, migration.name, migration.checksum),
            )
            applied_now.append(migration.name)

    return applied_now


def assert_connection_schema_current(connection) -> None:
    migrations = load_migrations()
    expected = {
        migration.version: (migration.name, migration.checksum)
        for migration in migrations
    }

    exists = connection.execute(
        "SELECT to_regclass('public.schema_migrations')"
    ).fetchone()[0]
    if exists is None:
        raise SchemaError("数据库尚未初始化，请先运行 uv run python migrate.py")

    rows = connection.execute(
        "SELECT version, name, checksum FROM public.schema_migrations ORDER BY version"
    ).fetchall()
    applied = {row[0]: (row[1], row[2]) for row in rows}

    if applied != expected:
        raise SchemaError("数据库 schema 与当前代码不一致，请运行 uv run python migrate.py")

    sessions_table = connection.execute(
        "SELECT to_regclass('public.sessions')"
    ).fetchone()[0]
    if sessions_table is None:
        raise SchemaError("数据库缺少 public.sessions 表，请运行 uv run python migrate.py")

    rows = connection.execute(
        """
        SELECT
            column_name,
            data_type,
            is_nullable,
            is_identity,
            identity_generation,
            column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'sessions'
        """
    ).fetchall()
    actual_columns = {row[0]: row[1:] for row in rows}
    required_columns = {
        "id": ("integer", "NO", "YES"),
        "items": ("jsonb", "NO", "NO"),
        "pending_approval": ("jsonb", "YES", "NO"),
        "revision": ("bigint", "NO", "NO"),
        "created_at": ("timestamp with time zone", "NO", "NO"),
        "updated_at": ("timestamp with time zone", "NO", "NO"),
    }
    if any(
        actual_columns.get(name, ())[:3] != details
        for name, details in required_columns.items()
    ):
        raise SchemaError("public.sessions 表结构与当前代码不一致")

    if actual_columns["id"][3] != "BY DEFAULT":
        raise SchemaError("public.sessions.id 必须使用 BY DEFAULT identity")
    for name in ("revision", "created_at", "updated_at"):
        if actual_columns[name][4] is None:
            raise SchemaError(f"public.sessions.{name} 缺少默认值")

    primary_key = connection.execute(
        """
        SELECT pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = 'public.sessions'::regclass AND contype = 'p'
        """
    ).fetchone()
    if primary_key is None or primary_key[0] != "PRIMARY KEY (id)":
        raise SchemaError("public.sessions.id 必须是主键")


def assert_schema_current(pool: ConnectionPool) -> None:
    with pool.connection() as connection:
        assert_connection_schema_current(connection)
