"""Versioned, transactional migrations for the SQLite store."""

from .connection import SCHEMA
from .metrics import METRICS_SCHEMA

RUN_SCHEMA = """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    user_message_id INTEGER NOT NULL REFERENCES chat_messages(id),
    assistant_message_id INTEGER REFERENCES chat_messages(id),
    status TEXT NOT NULL CHECK (status IN
        ('running', 'finishing', 'completed', 'failed', 'stopped', 'timed_out', 'limit_reached')),
    stop_requested INTEGER NOT NULL DEFAULT 0 CHECK (stop_requested IN (0, 1)),
    deadline_at REAL,
    turn_count INTEGER NOT NULL DEFAULT 0,
    logical_count INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    operation_count INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX idx_runs_session ON runs(session_id, id);
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    name TEXT NOT NULL,
    goal_version INTEGER NOT NULL DEFAULT 1,
    goal_history TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX idx_tasks_session ON tasks(session_id, id);
CREATE TABLE run_tasks (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (run_id, task_id)
);
CREATE TABLE operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    name TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    evidence TEXT,
    feedback TEXT,
    reliable INTEGER NOT NULL DEFAULT 0 CHECK (reliable IN (0, 1)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX idx_operations_task ON operations(task_id, id);
CREATE INDEX idx_operations_run ON operations(run_id, id);
"""


def _script(connection, script):
    # sqlite3.executescript commits pending transactions, so execute each DDL
    # statement under the migration's BEGIN IMMEDIATE instead.
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def migrate(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > 6:
        raise RuntimeError(f"Unsupported database version: {version}")
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if version < 1:
            _script(connection, SCHEMA)
            _script(connection, METRICS_SCHEMA)
            session_columns = {r["name"] for r in connection.execute("PRAGMA table_info(chat_sessions)")}
            for column, type_ in (("summary", "TEXT"), ("summary_upto_message_id", "INTEGER")):
                if column not in session_columns:
                    connection.execute(f"ALTER TABLE chat_sessions ADD COLUMN {column} {type_}")
            message_columns = {r["name"] for r in connection.execute("PRAGMA table_info(chat_messages)")}
            if "meta" not in message_columns:
                connection.execute("ALTER TABLE chat_messages ADD COLUMN meta TEXT")
            connection.execute("PRAGMA user_version = 1")
        if version < 2:
            connection.execute("""CREATE TABLE chat_messages_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
                parent_id INTEGER REFERENCES chat_messages(id),
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                status TEXT NOT NULL CHECK (status IN ('finished', 'failed', 'incomplete')),
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                meta TEXT
            )""")
            connection.execute("""INSERT INTO chat_messages_new
                (id, session_id, parent_id, role, status, content, created_at, meta)
                SELECT id, session_id, parent_id, role, status, content, created_at, meta
                FROM chat_messages""")
            connection.execute("DROP TABLE chat_messages")
            connection.execute("ALTER TABLE chat_messages_new RENAME TO chat_messages")
            connection.execute("CREATE INDEX idx_messages_session ON chat_messages(session_id)")
            connection.execute("CREATE INDEX idx_messages_parent ON chat_messages(parent_id)")
            connection.execute("ALTER TABLE chat_sessions ADD COLUMN active_run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL")
            _script(connection, RUN_SCHEMA)
            connection.execute("PRAGMA user_version = 2")
        if version < 3:
            connection.execute("ALTER TABLE operations ADD COLUMN tool_call_id TEXT")
            connection.execute("CREATE UNIQUE INDEX idx_operations_call ON operations(run_id, tool_call_id)")
            connection.execute("PRAGMA user_version = 3")
        if version < 4:
            # Older operation records predate per-operation goal tracking. Keep
            # their original facts while new attempts capture the actual version.
            connection.execute("ALTER TABLE operations ADD COLUMN goal_version INTEGER")
            connection.execute("PRAGMA user_version = 4")
        if version < 5:
            for statement in (
                "ALTER TABLE tasks ADD COLUMN constraints_text TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE tasks ADD COLUMN pending_clarifications TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE tasks ADD COLUMN pending_modification TEXT",
                "ALTER TABLE operations ADD COLUMN step_id TEXT",
                "ALTER TABLE operations ADD COLUMN business_operation_id TEXT",
                "ALTER TABLE operations ADD COLUMN conflict INTEGER NOT NULL DEFAULT 0 CHECK (conflict IN (0, 1))",
                "ALTER TABLE operations ADD COLUMN confirmed_result TEXT",
                "ALTER TABLE operations ADD COLUMN unconfirmed_reason TEXT",
                "ALTER TABLE operations ADD COLUMN submitted_at REAL",
            ):
                connection.execute(statement)
            _script(connection, """
                CREATE TABLE planned_steps (
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    step_id TEXT NOT NULL,
                    goal_version INTEGER NOT NULL,
                    tool TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    depends_on TEXT NOT NULL DEFAULT '[]',
                    approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
                    approved_run_id INTEGER REFERENCES runs(id),
                    created_at REAL NOT NULL,
                    PRIMARY KEY (task_id, step_id)
                );
                CREATE TABLE operation_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id INTEGER NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    result TEXT,
                    source TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    recorded_at REAL NOT NULL,
                    business_operation_id TEXT,
                    detail TEXT
                );
                CREATE INDEX idx_observations_operation ON operation_observations(operation_id, id);
            """)
            connection.execute("PRAGMA user_version = 5")
        # Column completion is idempotent and also repairs databases already
        # stamped with this version but created before the columns existed.
        if True:
            # Databases created or migrated before the final shape may
            # lack these columns; add them without touching stored facts.
            run_columns = {r["name"] for r in connection.execute("PRAGMA table_info(runs)")}
            for column in ("logical_count", "attempt_count"):
                if column not in run_columns:
                    connection.execute(
                        f"ALTER TABLE runs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
            operation_columns = {r["name"] for r in connection.execute("PRAGMA table_info(operations)")}
            for column, type_ in (("feedback", "TEXT"), ("reliable", "INTEGER NOT NULL DEFAULT 0")):
                if column not in operation_columns:
                    connection.execute(f"ALTER TABLE operations ADD COLUMN {column} {type_}")
            task_columns = {r["name"] for r in connection.execute("PRAGMA table_info(tasks)")}
            for column, type_ in (("goal_version", "INTEGER NOT NULL DEFAULT 1"),
                                  ("goal_history", "TEXT NOT NULL DEFAULT '[]'")):
                if column not in task_columns:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} {type_}")
            connection.execute("PRAGMA user_version = 6")
        errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if errors:
            raise RuntimeError(f"Migration broke foreign keys: {errors}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
