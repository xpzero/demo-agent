"""Durable run, task, operation and streaming assistant state."""

import time
import json

from .connection import StoreError
from .sessions import normalize_session_id


class RunStoreMixin:
    def create_run_turn(self, session_id, parent_message_id, content, file_ids, deadline_at=None) -> tuple[int, int]:
        session_id = normalize_session_id(session_id)
        with self.transaction() as connection:
            active = connection.execute(
                "SELECT active_run_id FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if active and active["active_run_id"] is not None:
                raise StoreError("active_run_exists", "会话已有运行中的任务", 409)
            message_id = self._create_user_turn(
                connection, session_id, parent_message_id, content, file_ids
            )
            now = time.time()
            # The Run's total budget begins at acceptance, including model setup.
            if deadline_at is None:
                deadline_at = now + 300
            run_id = connection.execute(
                """INSERT INTO runs (session_id, user_message_id, status, deadline_at, created_at, updated_at)
                   VALUES (?, ?, 'running', ?, ?, ?)""",
                (session_id, message_id, deadline_at, now, now),
            ).lastrowid
            connection.execute(
                "UPDATE chat_sessions SET active_run_id = ? WHERE id = ?",
                (run_id, session_id),
            )
            return message_id, run_id

    def get_run(self, run_id: int) -> dict | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return dict(row) if row else None

    def get_active_run(self, session_id: str) -> dict | None:
        session_id = normalize_session_id(session_id)
        with self.connection() as connection:
            row = connection.execute(
                """SELECT runs.* FROM chat_sessions JOIN runs
                   ON runs.id = chat_sessions.active_run_id WHERE chat_sessions.id = ?""",
                (session_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_latest_run(self, session_id: str) -> dict | None:
        session_id = normalize_session_id(session_id)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_session_status(self, session_id: str) -> dict | None:
        """A single lightweight read, without loading message history."""
        session_id = normalize_session_id(session_id)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT active_run_id FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            processing = row["active_run_id"] is not None
            return {"processing": processing, "can_send_message": not processing}

    def request_stop(self, session_id: str) -> dict:
        session_id = normalize_session_id(session_id)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT active_run_id FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise StoreError("session_not_found", "会话不存在", 404)
            if row["active_run_id"] is None:
                return {"result": "no_active_request", "run_id": None}
            run_id = row["active_run_id"]
            now = time.time()
            connection.execute(
                """UPDATE runs SET stop_requested = 1,
                   status = CASE WHEN status = 'running' THEN 'finishing' ELSE status END,
                   reason = CASE WHEN status = 'running' THEN 'user_stop' ELSE reason END,
                   deadline_at = CASE WHEN status != 'running' THEN deadline_at
                       WHEN deadline_at IS NULL THEN ?
                       ELSE MIN(deadline_at, ?) END,
                   updated_at = ?
                   WHERE id = ? AND status IN ('running', 'finishing') AND stop_requested = 0""",
                (now + 120, now + 120, now, run_id),
            )
            return {"result": "processing", "run_id": run_id}

    def finish_run(self, run_id: int, status: str, reason: str | None = None, *, assistant_status: str | None = None, assistant_content: str | None = None) -> bool:
        if status not in ("completed", "stopped", "failed", "timed_out", "limit_reached"):
            raise StoreError("invalid_run_status", "无效的结束状态")
        if assistant_status is not None and assistant_status not in ("finished", "failed", "incomplete"):
            raise StoreError("invalid_message_status", "无效的消息结束状态")
        with self.transaction() as connection:
            now = time.time()
            if connection.execute(
                """SELECT 1 FROM operations WHERE run_id = ? AND status IN
                   ('running','unknown','maybe_submitted','processing','unconfirmed') LIMIT 1""",
                (run_id,),
            ).fetchone():
                run_deadline = connection.execute("SELECT deadline_at, created_at FROM runs WHERE id=?", (run_id,)).fetchone()
                if status != "timed_out" or (run_deadline and (run_deadline["deadline_at"] or run_deadline["created_at"] + 300) > now):
                    raise StoreError("operation_needs_reconciliation", "业务结果未确认，运行需继续收尾", 409)
            if assistant_status is not None or assistant_content is not None:
                run = connection.execute("SELECT assistant_message_id, stop_requested, status FROM runs WHERE id = ?", (run_id,)).fetchone()
                if run is None or run["assistant_message_id"] is None:
                    raise StoreError("message_not_found", "运行没有助手消息", 404)
                if run["status"] not in ("running", "finishing"):
                    raise StoreError("run_not_active", "运行已结束", 409)
                target_status = ("incomplete" if status == "completed" and run["stop_requested"]
                                 else assistant_status or ("finished" if status == "completed" else "incomplete"))
                updated = connection.execute(
                    """UPDATE chat_messages SET status = ?, content = COALESCE(?, content)
                       WHERE id = ? AND status = 'incomplete'""",
                    (target_status, assistant_content, run["assistant_message_id"]),
                )
                if not updated.rowcount:
                    raise StoreError("message_already_finalized", "助手消息已结束", 409)
            result = connection.execute(
                """UPDATE runs SET status = CASE WHEN ? = 'completed' AND stop_requested = 1
                       THEN 'stopped' ELSE ? END,
                       reason = ?, updated_at = ?, finished_at = ?
                   WHERE id = ? AND status IN ('running', 'finishing')""",
                (status, status, reason, now, now, run_id),
            )
            if result.rowcount:
                connection.execute(
                    """UPDATE operations SET status = 'unknown', reliable = 0,
                       updated_at = ?, version = version + 1
                       WHERE run_id = ? AND status = 'running'""",
                    (now, run_id),
                )
                connection.execute(
                    """UPDATE chat_sessions SET active_run_id = NULL, updated_at = ?
                       WHERE active_run_id = ?""",
                    (now, run_id),
                )
            return result.rowcount == 1

    def upsert_partial_assistant(
        self, run_id: int, session_id: str, parent_message_id: int,
        content: str, status: str = "incomplete",
    ) -> int:
        if status != "incomplete":
            raise StoreError("invalid_message_status", "流式消息状态必须为 incomplete")
        session_id = normalize_session_id(session_id)
        with self.transaction() as connection:
            run = connection.execute(
                """SELECT runs.*, chat_sessions.active_run_id,
                          chat_sessions.current_message_id
                   FROM runs JOIN chat_sessions ON chat_sessions.id = runs.session_id
                   WHERE runs.id = ? AND runs.session_id = ?""",
                (run_id, session_id),
            ).fetchone()
            if run is None:
                raise StoreError("run_not_found", "运行不存在", 404)
            if run["active_run_id"] != run_id or run["status"] not in ("running", "finishing"):
                raise StoreError("run_not_active", "运行已结束", 409)
            if run["user_message_id"] != parent_message_id:
                raise StoreError("invalid_parent_message", "父消息不属于当前运行", 409)
            if run["assistant_message_id"] is not None:
                updated = connection.execute(
                    """UPDATE chat_messages SET content = ?
                       WHERE id = ? AND status = 'incomplete'""",
                    (content, run["assistant_message_id"]),
                )
                if not updated.rowcount:
                    raise StoreError("message_already_finalized", "助手消息已结束", 409)
                return run["assistant_message_id"]
            if run["current_message_id"] != parent_message_id:
                raise StoreError("stale_parent_message", "会话已更新", 409)
            now = time.time()
            message_id = connection.execute(
                """INSERT INTO chat_messages
                   (session_id, parent_id, role, status, content, created_at)
                   VALUES (?, ?, 'assistant', 'incomplete', ?, ?)""",
                (session_id, parent_message_id, content, now),
            ).lastrowid
            connection.execute("UPDATE runs SET assistant_message_id = ?, updated_at = ? WHERE id = ?",
                               (message_id, now, run_id))
            connection.execute(
                "UPDATE chat_sessions SET current_message_id = ?, updated_at = ? WHERE id = ?",
                (message_id, now, session_id),
            )
            return message_id

    def finalize_assistant_message(
        self, run_id: int, status: str = "finished", content: str | None = None
    ) -> int:
        if status not in ("finished", "failed"):
            raise StoreError("invalid_message_status", "无效的消息结束状态")
        with self.transaction() as connection:
            run = connection.execute("SELECT assistant_message_id FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None or run["assistant_message_id"] is None:
                raise StoreError("message_not_found", "运行没有助手消息", 404)
            message_id = run["assistant_message_id"]
            result = connection.execute(
                """UPDATE chat_messages SET status = ?, content = COALESCE(?, content)
                   WHERE id = ? AND status = 'incomplete'""",
                (status, content, message_id),
            )
            if not result.rowcount:
                raise StoreError("message_already_finalized", "助手消息已结束", 409)
            return message_id

    def create_task(self, run_id: int, name: str, status: str = "pending") -> int:
        with self.transaction() as connection:
            run = connection.execute("SELECT session_id FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise StoreError("run_not_found", "运行不存在", 404)
            now = time.time()
            task_id = connection.execute(
                "INSERT INTO tasks (session_id, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (run["session_id"], name, status, now, now),
            ).lastrowid
            connection.execute("INSERT INTO run_tasks (run_id, task_id) VALUES (?, ?)", (run_id, task_id))
            return task_id

    def attach_task(self, run_id: int, task_id: int) -> None:
        with self.transaction() as connection:
            run = connection.execute("SELECT session_id FROM runs WHERE id = ?", (run_id,)).fetchone()
            task = connection.execute("SELECT session_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if run is None or task is None or run["session_id"] != task["session_id"]:
                raise StoreError("invalid_task", "任务与运行不属于同一会话", 409)
            connection.execute("INSERT OR IGNORE INTO run_tasks (run_id, task_id) VALUES (?, ?)", (run_id, task_id))

    def record_model_request(self, run_id: int, attempts: int = 0) -> int:
        """Compatibility helper; prefer record_model_attempt before each send."""
        if attempts < 0:
            raise StoreError("invalid_attempts", "重试次数不能为负数")
        self.record_model_attempt(run_id, True)
        for _ in range(attempts):
            self.record_model_attempt(run_id, False)
        return self.get_run(run_id)["attempt_count"]

    def record_model_attempt(self, run_id: int, new_logical: bool, *, max_logical: int = 10, max_attempts: int = 30) -> dict:
        """Persist attempt counts before each external request."""
        with self.transaction() as connection:
            now = time.time()
            result = connection.execute(
                """UPDATE runs SET logical_count = logical_count + ?, turn_count = turn_count + ?,
                   attempt_count = attempt_count + 1, updated_at = ?
                   WHERE id = ? AND status = 'running' AND stop_requested = 0
                   AND (deadline_at IS NULL OR deadline_at > ?)
                   AND logical_count + ? <= ? AND attempt_count < ?
                   AND (? = 1 OR logical_count > 0)""",
                (int(new_logical), int(new_logical), now, run_id, now,
                 int(new_logical), max_logical, max_attempts, int(new_logical)),
            )
            if result.rowcount != 1:
                raise StoreError("run_not_active", "运行已结束或已超时", 409)
            row = connection.execute("SELECT logical_count, attempt_count FROM runs WHERE id = ?", (run_id,)).fetchone()
            return dict(row)

    def enter_finishing(self, run_id: int, reason: str | None = None, finishing_deadline_at: float | None = None) -> bool:
        with self.transaction() as connection:
            result = connection.execute(
                """UPDATE runs SET status = 'finishing', reason = COALESCE(?, reason),
                   deadline_at = CASE
                       WHEN ? IS NULL THEN deadline_at
                       WHEN deadline_at IS NULL THEN ?
                       ELSE MIN(deadline_at, ?) END, updated_at = ?
                   WHERE id = ? AND status = 'running'""",
                (reason, finishing_deadline_at, finishing_deadline_at,
                 finishing_deadline_at, time.time(), run_id),
            )
            return result.rowcount == 1

    def _reconcile_interrupted(self, connection, run, now, *, startup=False):
        """One transaction protects the Run decision and its Session slot."""
        run_id = run["id"]
        deadline = run["deadline_at"]
        if deadline is None:
            deadline = run["created_at"] + 300
            connection.execute("UPDATE runs SET deadline_at = ? WHERE id = ?", (deadline, run_id))
        if startup:
            # Entering the submit stage means the call may have left this process.
            # Keep existing feedback/evidence and never relabel pending steps.
            connection.execute(
                """UPDATE operations SET status = 'unknown', updated_at = ?,
                   version = version + 1 WHERE run_id = ? AND status = 'running'""",
                (now, run_id),
            )
            if run["status"] == "running":
                connection.execute(
                    """UPDATE runs SET status = 'finishing',
                       reason = COALESCE(reason, 'worker_lost_on_restart'),
                       updated_at = ? WHERE id = ? AND status = 'running'""",
                    (now, run_id),
                )
        if deadline <= now:
            # Timeout is a conclusion about this Run, never about business outcome.
            connection.execute(
                """UPDATE runs SET status = 'timed_out',
                   reason = COALESCE(reason, 'worker_lost_on_restart'),
                   updated_at = ?, finished_at = ?
                   WHERE id = ? AND status = 'finishing'""",
                (now, now, run_id),
            )
            connection.execute(
                """UPDATE chat_sessions SET active_run_id = NULL, updated_at = ?
                   WHERE active_run_id = ?""",
                (now, run_id),
            )
            return "timed_out"
        return "pending"

    def recover_orphan_runs(self) -> int:
        """One-time startup claim; keeps the original Run, budget and facts.

        Parent must call list_orphan_runs and schedule reconcile_orphan_run for
        each deadline. Do not call this startup claim on newly created live Runs.
        """
        with self.transaction() as connection:
            now = time.time()
            rows = connection.execute(
                """SELECT runs.* FROM runs JOIN chat_sessions
                   ON chat_sessions.active_run_id = runs.id
                   WHERE runs.status IN ('running', 'finishing')"""
            ).fetchall()
            changed = 0
            for run in rows:
                outcome = self._reconcile_interrupted(connection, run, now, startup=True)
                if run["status"] == "running" or outcome == "timed_out":
                    changed += 1
            return changed

    def list_orphan_runs(self) -> list[dict]:
        """Pending restart work and persisted operation facts for the parent.

        The parent may query the original operation through a business adapter;
        no submitted operation may be redispatched solely from this snapshot.
        """
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT runs.* FROM runs JOIN chat_sessions
                   ON chat_sessions.active_run_id = runs.id
                   WHERE runs.status = 'finishing'"""
            ).fetchall()
            return [
                {**dict(run), "operations": [dict(op) for op in connection.execute(
                    "SELECT * FROM operations WHERE run_id = ? ORDER BY id", (run["id"],)
                )]}
                for run in rows
            ]

    def reconcile_orphan_run(self, run_id: int) -> dict:
        """Recheck one orphan against its original deadline, without dispatch.

        Returns pending plus next_check_at while waiting, or timed_out after
        atomic release. Parent can save reliable adapter feedback before calling
        this; missing or unqueryable results remain unknown at timeout.
        """
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT runs.* FROM runs JOIN chat_sessions
                   ON chat_sessions.active_run_id = runs.id
                   WHERE runs.id = ? AND runs.status = 'finishing'""",
                (run_id,),
            ).fetchone()
            if row is None:
                existing = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
                if existing is None:
                    raise StoreError("run_not_found", "运行不存在", 404)
                return {"status": existing["status"], "next_check_at": None}
            outcome = self._reconcile_interrupted(connection, row, time.time())
            deadline = row["deadline_at"] if row["deadline_at"] is not None else row["created_at"] + 300
            return {"status": outcome, "next_check_at": deadline if outcome == "pending" else None}

    def list_run_operations(self, run_id: int) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM operations WHERE run_id = ? ORDER BY id", (run_id,)
            )]

    def revise_task_goal(self, task_id: int, name: str, expected_version: int) -> bool:
        with self.transaction() as connection:
            row = connection.execute("SELECT name, goal_history FROM tasks WHERE id = ? AND goal_version = ?", (task_id, expected_version)).fetchone()
            if row is None:
                return False
            history = json.loads(row["goal_history"])
            history.append({"version": expected_version, "name": row["name"]})
            return connection.execute(
                """UPDATE tasks SET name = ?, goal_version = goal_version + 1,
                   goal_history = ?, updated_at = ? WHERE id = ? AND goal_version = ?""",
                (name, json.dumps(history, ensure_ascii=False), time.time(), task_id, expected_version),
            ).rowcount == 1

    def record_operation_feedback(self, operation_id: int, expected_version: int, feedback: str, evidence: str, reliable: bool) -> bool:
        with self.transaction() as connection:
            return connection.execute(
                """UPDATE operations SET feedback = ?, evidence = ?, reliable = ?,
                   version = version + 1, updated_at = ? WHERE id = ? AND version = ?""",
                (feedback, evidence, int(reliable), time.time(), operation_id, expected_version),
            ).rowcount == 1

    def update_task(self, task_id: int, status: str) -> bool:
        with self.transaction() as connection:
            return connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status, time.time(), task_id),
            ).rowcount == 1

    def list_session_tasks(self, session_id: str) -> list[dict]:
        """Return only this Session's persisted task candidates."""
        session_id = normalize_session_id(session_id)
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM tasks WHERE session_id = ? ORDER BY id", (session_id,)
            )]

    def list_tasks(self, run_id: int) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT tasks.* FROM tasks JOIN run_tasks ON run_tasks.task_id = tasks.id WHERE run_tasks.run_id = ? ORDER BY tasks.id", (run_id,)
            )]

    def create_operation(self, task_id: int, name: str, status: str = "pending", content: str = "", *, run_id: int | None = None, evidence: str | None = None) -> int:
        with self.transaction() as connection:
            if run_id is None:
                links = connection.execute("SELECT run_id FROM run_tasks WHERE task_id = ? ORDER BY run_id DESC", (task_id,)).fetchall()
                if len(links) != 1:
                    raise StoreError("run_required", "请指定操作所属运行", 409)
                run_id = links[0]["run_id"]
            elif connection.execute("SELECT 1 FROM run_tasks WHERE run_id = ? AND task_id = ?", (run_id, task_id)).fetchone() is None:
                raise StoreError("invalid_task", "任务未关联此运行", 409)
            task = connection.execute("SELECT goal_version FROM tasks WHERE id = ?", (task_id,)).fetchone()
            now = time.time()
            operation_id = connection.execute(
                """INSERT INTO operations (task_id, run_id, name, status, content, evidence, goal_version, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, run_id, name, status, content, evidence, task["goal_version"], now, now),
            ).lastrowid
            connection.execute("UPDATE runs SET operation_count = operation_count + 1 WHERE id = ?", (run_id,))
            return operation_id

    def update_operation(self, operation_id: int, status: str, content: str) -> bool:
        with self.transaction() as connection:
            return connection.execute(
                "UPDATE operations SET status = ?, content = ?, updated_at = ? WHERE id = ?",
                (status, content, time.time(), operation_id),
            ).rowcount == 1

    def list_operations(self, task_id: int) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM operations WHERE task_id = ? ORDER BY id", (task_id,)
            )]

    def begin_tool_operation(self, run_id: int, tool_call_id: str, name: str, args: dict,
                             *, task_id: int | None = None, goal_version: int | None = None) -> tuple[int, int, int]:
        """Commit the attempt before dispatch, under the same lock as stop and goal edits."""
        with self.transaction() as connection:
            now = time.time()
            run = connection.execute(
                "SELECT session_id, user_message_id FROM runs WHERE id = ? AND status = 'running' "
                "AND stop_requested = 0 AND (deadline_at IS NULL OR deadline_at > ?)",
                (run_id, now),
            ).fetchone()
            if run is None:
                raise StoreError("run_not_active", "运行已结束或已停止", 409)
            if not tool_call_id:
                raise StoreError("invalid_tool_call", "工具调用缺少 ID", 409)
            if connection.execute("SELECT 1 FROM operations WHERE run_id = ? AND tool_call_id = ?",
                                  (run_id, tool_call_id)).fetchone():
                raise StoreError("operation_already_attempted", "工具调用已尝试，需核实结果", 409)
            links = connection.execute(
                "SELECT tasks.id, tasks.goal_version, tasks.status FROM tasks JOIN run_tasks "
                "ON run_tasks.task_id = tasks.id WHERE run_tasks.run_id = ?", (run_id,)
            ).fetchall()
            if task_id is None and links:
                raise StoreError("task_changed", "运行关联的任务已变化，需重新判断", 409)
            if task_id is None and not links:
                if connection.execute(
                    """SELECT 1 FROM operations JOIN tasks ON tasks.id = operations.task_id
                       WHERE tasks.session_id = ? AND operations.status IN ('unknown', 'running')""",
                    (run["session_id"],),
                ).fetchone():
                    raise StoreError("operation_needs_reconciliation", "先核实未确定的操作结果", 409)
                request = connection.execute("SELECT content FROM chat_messages WHERE id = ?",
                                             (run["user_message_id"],)).fetchone()
                task_id = connection.execute(
                    "INSERT INTO tasks (session_id, name, status, created_at, updated_at) "
                    "VALUES (?, ?, 'running', ?, ?)",
                    (run["session_id"], request["content"], now, now),
                ).lastrowid
                connection.execute("INSERT INTO run_tasks (run_id, task_id) VALUES (?, ?)", (run_id, task_id))
                version = 1
            else:
                if len(links) != 1 or (task_id is not None and links[0]["id"] != task_id):
                    raise StoreError("ambiguous_task", "无法确定当前任务", 409)
                task_id = links[0]["id"]
                version = links[0]["goal_version"]
                if links[0]["status"] not in ("pending", "running", "active") or goal_version != version:
                    raise StoreError("task_changed", "任务目标或状态已变化，需重新判断", 409)
            if connection.execute(
                "SELECT 1 FROM operations WHERE task_id = ? AND status IN ('unknown', 'running')",
                (task_id,),
            ).fetchone():
                raise StoreError("operation_needs_reconciliation", "先核实未确定的操作结果", 409)
            operation_id = connection.execute(
                """INSERT INTO operations (task_id, run_id, name, status, content, tool_call_id,
                   goal_version, created_at, updated_at) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)""",
                (task_id, run_id, name, json.dumps(args, ensure_ascii=False), tool_call_id, version, now, now),
            ).lastrowid
            connection.execute("UPDATE runs SET operation_count = operation_count + 1 WHERE id = ?", (run_id,))
            return operation_id, task_id, version

    def finish_tool_operation(self, operation_id: int, output: str, ok: bool, *,
                              business: bool = False) -> None:
        """Record a returned tool call.

        Local tools completing inside this process have definite results.
        Adapter-managed business tools only acknowledge transport; their
        business outcome stays unknown until reliable adapter evidence.
        """
        status, reliable = ("unknown", 0) if business else ("succeeded" if ok else "failed", 1)
        with self.transaction() as connection:
            result = connection.execute(
                """UPDATE operations SET status = ?, content = ?, reliable = ?,
                   version = version + 1, updated_at = ? WHERE id = ? AND status = 'running'""",
                (status, output, reliable, time.time(), operation_id),
            )
            if result.rowcount != 1:
                raise StoreError("operation_not_running", "操作状态已变化", 409)
