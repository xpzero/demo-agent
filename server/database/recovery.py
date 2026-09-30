"""Durable goal, approved step, and business evidence storage contracts."""
import json
import time

from .connection import StoreError
from recovery.adapter import BusinessAdapter, Observation, FINAL


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class RecoveryStoreMixin:
    def adopt_goal(self, task_id: int, goal: str, constraints: str, *, expected_version: int,
                   run_id: int, message_id: int) -> int:
        """Adopt a complete new goal, retaining the previous complete version."""
        if not goal.strip() or not isinstance(constraints, str):
            raise StoreError("invalid_goal", "目标不能为空")
        with self.transaction() as db:
            row = db.execute("""SELECT tasks.* FROM tasks JOIN run_tasks ON run_tasks.task_id=tasks.id
                                WHERE tasks.id=? AND run_tasks.run_id=?""", (task_id, run_id)).fetchone()
            message = db.execute("SELECT 1 FROM runs WHERE id=? AND user_message_id=?", (run_id, message_id)).fetchone()
            if row is None or message is None or row["goal_version"] != expected_version:
                raise StoreError("task_changed", "任务目标或来源已变化", 409)
            history = json.loads(row["goal_history"])
            history.append({"version": expected_version, "name": row["name"],
                            "constraints": row["constraints_text"], "run_id": run_id,
                            "message_id": message_id})
            db.execute("""UPDATE tasks SET name=?, constraints_text=?, goal_version=?, goal_history=?,
                          status='active', pending_modification=NULL, updated_at=? WHERE id=?""",
                       (goal, constraints, expected_version + 1, _json(history), time.time(), task_id))
            return expected_version + 1

    def save_goal_modification(self, task_id: int, run_id: int, message_id: int, text: str) -> None:
        with self.transaction() as db:
            row = db.execute("""SELECT tasks.id FROM tasks JOIN run_tasks ON tasks.id=run_tasks.task_id
                                JOIN runs ON runs.id=run_tasks.run_id
                                WHERE tasks.id=? AND runs.id=? AND runs.user_message_id=?""",
                             (task_id, run_id, message_id)).fetchone()
            if row is None:
                raise StoreError("invalid_task", "修改来源不属于任务", 409)
            db.execute("UPDATE tasks SET pending_modification=?, status='waiting_input', updated_at=? WHERE id=?",
                       (_json({"text": text, "run_id": run_id, "message_id": message_id}), time.time(), task_id))

    def add_clarification(self, task_id: int, run_id: int, message_id: int, question: str) -> int:
        if not question.strip():
            raise StoreError("invalid_question", "澄清问题不能为空")
        with self.transaction() as db:
            row = db.execute("""SELECT tasks.* FROM tasks JOIN run_tasks ON tasks.id=run_tasks.task_id
                                JOIN runs ON runs.id=run_tasks.run_id
                                WHERE tasks.id=? AND runs.id=? AND runs.user_message_id=?""",
                             (task_id, run_id, message_id)).fetchone()
            if row is None:
                raise StoreError("invalid_task", "问题来源不属于任务", 409)
            questions = json.loads(row["pending_clarifications"])
            questions.append({"question": question, "run_id": run_id, "message_id": message_id,
                              "goal_version": row["goal_version"], "state": "pending", "answer_message_id": None})
            db.execute("UPDATE tasks SET pending_clarifications=?, status='waiting_input', updated_at=? WHERE id=?",
                       (_json(questions), time.time(), task_id))
            return len(questions) - 1

    def resolve_clarification(self, task_id: int, index: int, answer_message_id: int,
                              *, state: str = "answered") -> None:
        if state not in ("answered", "superseded"):
            raise StoreError("invalid_question_state", "无效的澄清状态")
        with self.transaction() as db:
            row = db.execute("SELECT pending_clarifications, session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise StoreError("invalid_task", "任务不存在", 404)
            questions = json.loads(row["pending_clarifications"])
            if not 0 <= index < len(questions) or questions[index]["state"] != "pending":
                raise StoreError("question_changed", "问题已更新", 409)
            if db.execute("SELECT 1 FROM chat_messages WHERE id=? AND session_id=? AND role='user'",
                          (answer_message_id, row["session_id"])).fetchone() is None:
                raise StoreError("invalid_answer", "回答不属于当前会话", 409)
            questions[index].update(state=state, answer_message_id=answer_message_id)
            db.execute("UPDATE tasks SET pending_clarifications=?, updated_at=? WHERE id=?",
                       (_json(questions), time.time(), task_id))

    def approve_step(self, task_id: int, step_id: str, tool: str, args: dict, *,
                     depends_on: tuple[int, ...] = (), run_id: int, goal_version: int,
                     adapter: BusinessAdapter) -> None:
        if not step_id or not tool or not isinstance(args, dict) or not isinstance(depends_on, tuple):
            raise StoreError("invalid_step", "无效的步骤")
        args_json = _json(args)
        if not adapter.approve(task_id, goal_version, step_id, tool, args, depends_on):
            raise StoreError("step_not_approved", "业务适配层未批准步骤", 409)
        with self.transaction() as db:
            task = db.execute("""SELECT tasks.goal_version, tasks.status FROM tasks JOIN run_tasks
                                ON tasks.id=run_tasks.task_id WHERE tasks.id=? AND run_tasks.run_id=?""",
                              (task_id, run_id)).fetchone()
            if task is None or task["goal_version"] != goal_version or task["status"] not in ("active", "running", "pending"):
                raise StoreError("task_changed", "任务不可审批", 409)
            if db.execute("SELECT 1 FROM planned_steps WHERE task_id=? AND step_id=?", (task_id, step_id)).fetchone():
                raise StoreError("step_exists", "步骤已记录", 409)
            for dep in depends_on:
                if db.execute("SELECT 1 FROM operations WHERE id=? AND task_id=?", (dep, task_id)).fetchone() is None:
                    raise StoreError("invalid_dependency", "依赖操作不属于任务", 409)
            db.execute("""INSERT INTO planned_steps
                          (task_id,step_id,goal_version,tool,args_json,depends_on,approved,approved_run_id,created_at)
                          VALUES (?,?,?,?,?,?,1,?,?)""",
                       (task_id, step_id, goal_version, tool, args_json, _json(depends_on), run_id, time.time()))

    def list_planned_steps(self, task_id: int) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM planned_steps WHERE task_id=? ORDER BY created_at,step_id", (task_id,))]

    def begin_approved_step(self, run_id: int, task_id: int, step_id: str, tool_call_id: str,
                            name: str, args: dict, *, goal_version: int) -> int:
        """Atomic stop/goal/evidence check and durable maybe-submitted registration.

        Only trusted orchestration may approve a step using adapter clearance;
        a model suggestion alone must never call approve_step.
        """
        with self.transaction() as db:
            now = time.time()
            run = db.execute("""SELECT runs.session_id FROM runs JOIN chat_sessions
                                ON chat_sessions.active_run_id=runs.id WHERE runs.id=?
                                AND runs.status='running' AND runs.stop_requested=0
                                AND chat_sessions.active_run_id=? AND runs.deadline_at>?""",
                             (run_id, run_id, now)).fetchone()
            task = db.execute("""SELECT tasks.* FROM tasks JOIN run_tasks ON tasks.id=run_tasks.task_id
                                WHERE tasks.id=? AND run_tasks.run_id=?""", (task_id, run_id)).fetchone()
            step = db.execute("SELECT * FROM planned_steps WHERE task_id=? AND step_id=?", (task_id, step_id)).fetchone()
            if run is None or task is None or task["session_id"] != run["session_id"] or task["status"] != "active" or task["goal_version"] != goal_version or step is None or not step["approved"] or step["goal_version"] != goal_version or not tool_call_id:
                raise StoreError("step_not_dispatchable", "步骤或运行已变化", 409)
            try:
                same_args = isinstance(args, dict) and _json(args) == step["args_json"]
            except (TypeError, ValueError):
                same_args = False
            if name != step["tool"] or not same_args:
                raise StoreError("step_mismatch", "工具调用与批准步骤不符", 409)
            if db.execute("SELECT 1 FROM operations WHERE task_id=? AND step_id=?", (task_id, step_id)).fetchone() or db.execute("SELECT 1 FROM operations WHERE run_id=? AND tool_call_id=?", (run_id, tool_call_id)).fetchone():
                raise StoreError("operation_already_attempted", "步骤已尝试，需核实结果", 409)
            # Independent steps also need explicit adapter approval; unresolved facts
            # cannot be treated as proof that independent execution is safe.
            if db.execute("""SELECT 1 FROM operations WHERE task_id=? AND
                             (conflict=1 OR status IN ('running','unknown','maybe_submitted','processing','unconfirmed'))""",
                          (task_id,)).fetchone():
                raise StoreError("operation_needs_reconciliation", "先核实未确定的操作结果", 409)
            for dep in json.loads(step["depends_on"]):
                evidence = db.execute("SELECT status, conflict, confirmed_result FROM operations WHERE id=? AND task_id=?",
                                      (dep, task_id)).fetchone()
                if evidence is None or evidence["status"] != "succeeded" or evidence["conflict"]:
                    raise StoreError("dependency_not_confirmed", "依赖结果未可靠确认", 409)
            op = db.execute("""INSERT INTO operations (task_id,run_id,name,status,content,tool_call_id,
                                 goal_version,step_id,submitted_at,created_at,updated_at)
                                 VALUES (?,?,?,'maybe_submitted',?,?,?,?,?,?,?)""",
                            (task_id, run_id, step["tool"], step["args_json"], tool_call_id,
                             goal_version, step_id, now, now, now)).lastrowid
            db.execute("UPDATE runs SET operation_count=operation_count+1 WHERE id=?", (run_id,))
            return op

    def list_operation_observations(self, operation_id: int) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM operation_observations WHERE operation_id=? ORDER BY id", (operation_id,))]

    def record_confirmed_feedback(self, operation_id: int, observation: Observation, *,
                                  source: str, observed_at: float, business_operation_id: str | None = None) -> dict:
        if not isinstance(observation, Observation) or not source or not isinstance(observed_at, (int, float)):
            raise StoreError("invalid_observation", "缺少业务证据")
        with self.transaction() as db:
            op = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if op is None:
                raise StoreError("operation_not_found", "操作不存在", 404)
            if op["business_operation_id"] and business_operation_id and op["business_operation_id"] != business_operation_id:
                raise StoreError("business_id_changed", "业务操作标识冲突", 409)
            now = time.time()
            db.execute("""INSERT INTO operation_observations
                          (operation_id,status,result,source,observed_at,recorded_at,business_operation_id,detail)
                          VALUES (?,?,?,?,?,?,?,?)""",
                       (operation_id, observation.status, observation.result, source, observed_at, now,
                        business_operation_id, observation.detail))
            previous = op["status"]
            conflict = bool(op["conflict"]) or (previous in FINAL and observation.status in FINAL and
                        (previous != observation.status or op["confirmed_result"] != observation.result))
            if previous in FINAL or conflict:
                status, result = previous, op["confirmed_result"]
            elif observation.status in FINAL:
                status, result = observation.status, observation.result
            elif previous in ("maybe_submitted", "running", "unknown", "processing", "unconfirmed"):
                status, result = observation.status, None
            else:
                raise StoreError("operation_not_submitted", "操作尚未提交", 409)
            db.execute("""UPDATE operations SET status=?, confirmed_result=?, conflict=?,
                          business_operation_id=COALESCE(business_operation_id,?),
                          unconfirmed_reason=?, version=version+1, updated_at=? WHERE id=?""",
                       (status, result, int(conflict), business_operation_id,
                        observation.detail if status not in FINAL else None, now, operation_id))
            unresolved = db.execute("""SELECT 1 FROM operations WHERE task_id=? AND
                                      (conflict=1 OR status IN ('running','unknown','maybe_submitted','processing','unconfirmed'))
                                      LIMIT 1""", (op["task_id"],)).fetchone()
            if unresolved:
                db.execute("UPDATE tasks SET status='waiting_business', updated_at=? WHERE id=?", (now, op["task_id"]))
            else:
                db.execute("""UPDATE tasks SET status='active', updated_at=? WHERE id=?
                              AND status='waiting_business' AND pending_modification IS NULL""", (now, op["task_id"]))
            return {"status": status, "confirmed_result": result, "conflict": conflict}

    def defer_uncertain_run(self, run_id: int, reason: str) -> dict | bool:
        """Keep Session occupied until original deadline; never infer business failure."""
        with self.transaction() as db:
            run = db.execute("""SELECT runs.* FROM runs JOIN chat_sessions ON chat_sessions.active_run_id=runs.id
                                WHERE runs.id=? AND runs.status IN ('running','finishing')""", (run_id,)).fetchone()
            if run is None:
                return False
            pending = db.execute("""SELECT 1 FROM operations WHERE run_id=? AND status IN
                                  ('running','unknown','maybe_submitted','processing','unconfirmed') LIMIT 1""",
                                 (run_id,)).fetchone()
            if pending is None:
                return False
            now = time.time()
            db.execute("""UPDATE operations SET status='unknown', reliable=0, version=version+1, updated_at=?
                          WHERE run_id=? AND status='running'""", (now, run_id))
            db.execute("""UPDATE runs SET status='finishing', reason=COALESCE(reason,?), updated_at=?
                          WHERE id=? AND status='running'""", (reason, now, run_id))
            return {"status": "finishing", "next_check_at": run["deadline_at"] or run["created_at"] + 300}
