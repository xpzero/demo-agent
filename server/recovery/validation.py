"""Recovery decisions over normalized, authoritative records; no I/O or dispatch.

The caller supplies records read from the *current Session*. Operation statuses
are evidence classified by the business adapter, never inferred from model text,
Run termination, a transport timeout or a tool_call_id. A snapshot is a model
input, not a lock: build another snapshot after the model returns and check it
before scheduling. The parent must repeat the check atomically with dispatch.
"""

from dataclasses import dataclass
import json
from typing import Any


_FINAL = frozenset({"succeeded", "failed", "cancelled"})
_UNKNOWN = frozenset({"maybe_submitted", "processing", "unconfirmed"})
_STATUSES = _FINAL | _UNKNOWN | {"not_started"}
_ACTIONS = frozenset({"reply", "clarify", "verify", "execute", "modify_goal"})


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class OperationFact:
    id: str  # stable Agent operation ID, not a model tool_call_id
    task_id: str
    goal_version: int
    step_id: str
    status: str
    result: str | None = None  # only reliable confirmed results; never a model claim
    conflict: bool = False
    queryable: bool = False  # adapter can query the original operation


@dataclass(frozen=True)
class PlannedStep:
    id: str
    tool: str
    args_json: str  # canonical JSON of adapter-approved arguments
    depends_on: tuple[str, ...] = ()  # stable Operation IDs
    authorized: bool = False  # explicit adapter clearance for this step


@dataclass(frozen=True)
class TaskFact:
    id: str
    session_id: str
    goal: str
    constraints: str
    version: int
    status: str
    original_request: str
    operations: tuple[OperationFact, ...] = ()
    steps: tuple[PlannedStep, ...] = ()
    pending_question: str | None = None


@dataclass(frozen=True)
class RecoverySnapshot:
    session_id: str
    new_message: str
    candidates: tuple[TaskFact, ...]
    run_state: str  # 'running' only; stop_requested/finishing/terminal block actions
    stop_requested: bool
    budget_available: bool


@dataclass(frozen=True)
class Validation:
    outcome: str  # allow, clarify, rejudge, skip, blocked, invalid
    reason: str
    action: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    operation_id: str | None = None
    facts: tuple[OperationFact, ...] = ()  # parent renders critical results from these


def build_task_snapshot(
    session_id: str, new_message: str, candidates: tuple[TaskFact, ...],
    *, run_state: str, stop_requested: bool, budget_available: bool,
) -> RecoverySnapshot:
    """Check completeness and ownership of records before offering facts to a model.

    Caller must include *all* plausible current-Session tasks and all operations
    and planned steps for each task. Missing essential facts must fail closed.
    """
    if not session_id or not isinstance(new_message, str) or not isinstance(candidates, tuple):
        raise ValueError("invalid snapshot input")
    if run_state not in {"running", "finishing", "completed", "stopped", "failed", "timed_out", "limit_reached"}:
        raise ValueError("invalid run state")
    if type(stop_requested) is not bool or type(budget_available) is not bool:
        raise ValueError("invalid run controls")
    seen_tasks: set[str] = set()
    for task in candidates:
        if not isinstance(task, TaskFact) or not task.id or task.id in seen_tasks or task.session_id != session_id:
            raise ValueError("invalid task candidate")
        seen_tasks.add(task.id)
        if type(task.version) is not int or task.version < 1 or not task.goal or not task.original_request:
            raise ValueError("incomplete task goal")
        if task.status not in {"active", "waiting_input", "waiting_business", "completed"}:
            raise ValueError("invalid task status")
        if not isinstance(task.operations, tuple) or not isinstance(task.steps, tuple):
            raise ValueError("incomplete task records")
        operations: dict[str, OperationFact] = {}
        for op in task.operations:
            if (not isinstance(op, OperationFact) or not op.id or op.id in operations
                    or op.task_id != task.id or type(op.goal_version) is not int
                    or not 1 <= op.goal_version <= task.version or not op.step_id
                    or op.status not in _STATUSES or type(op.conflict) is not bool
                    or type(op.queryable) is not bool
                    or (op.status in _FINAL) != (op.result is not None)
                    or (op.result is not None and not isinstance(op.result, str))):
                raise ValueError("invalid operation evidence")
            operations[op.id] = op
        seen_steps: set[str] = set()
        for step in task.steps:
            if not isinstance(step, PlannedStep) or not step.id or step.id in seen_steps or not step.tool:
                raise ValueError("invalid planned step")
            seen_steps.add(step.id)
            try:
                if not isinstance(step.args_json, str) or _json(json.loads(step.args_json)) != step.args_json:
                    raise ValueError("noncanonical step arguments")
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid step arguments") from exc
            if (not isinstance(step.depends_on, tuple) or type(step.authorized) is not bool
                    or any(dep not in operations for dep in step.depends_on)):
                raise ValueError("invalid step dependencies")
    return RecoverySnapshot(session_id, new_message, candidates, run_state, stop_requested, budget_available)


def validate_suggestion(suggestion: Any, seen: RecoverySnapshot, latest: RecoverySnapshot) -> Validation:
    """Validate structured model output against freshly loaded facts.

    Shape: {intent, task_id, task_version, action, operation_refs,
    step_id?, tool?, args?, question?, goal?}. Each operation_ref is
    {id, status, result, conflict}. No returned value schedules an action.
    'allow' is only a candidate for the parent's atomic dispatch check.
    """
    def decision(outcome: str, reason: str, **kw: Any) -> Validation:
        return Validation(outcome, reason, **kw)

    if not isinstance(suggestion, dict) or not isinstance(seen, RecoverySnapshot) or not isinstance(latest, RecoverySnapshot):
        return decision("invalid", "malformed suggestion or snapshot")
    if seen.session_id != latest.session_id or seen.new_message != latest.new_message:
        return decision("invalid", "snapshot identity changed")
    if latest.run_state != "running" or latest.stop_requested or not latest.budget_available:
        return decision("blocked", "run stopped or budget exhausted")
    intent = suggestion.get("intent")
    action = suggestion.get("action")
    if not isinstance(intent, str) or not isinstance(action, str) or intent not in {"reply", "complete_reply", "verify", "resume", "modify_goal", "clarify"} or action not in _ACTIONS:
        return decision("invalid", "missing or invalid intent/action")
    allowed = {
        "reply": {"reply"}, "complete_reply": {"reply"}, "verify": {"verify"},
        "resume": {"execute", "verify", "reply"}, "modify_goal": {"modify_goal", "clarify"},
        "clarify": {"clarify"},
    }
    if action not in allowed[intent]:
        return decision("invalid", "intent/action mismatch")
    seen_tasks = {task.id: task for task in seen.candidates}
    current_tasks = {task.id: task for task in latest.candidates}
    task_id = suggestion.get("task_id")
    if task_id is None and action == "clarify" and (len(seen_tasks) != 1 or len(current_tasks) != 1):
        if isinstance(suggestion.get("question"), str) and suggestion["question"].strip():
            return decision("clarify", "ambiguous task candidates", action=action)
        return decision("invalid", "missing clarification question")
    if not isinstance(task_id, str) or task_id not in seen_tasks or task_id not in current_tasks:
        return decision("invalid", "task outside current Session candidates")
    if len(seen_tasks) != 1 or len(current_tasks) != 1:
        return decision("clarify", "ambiguous task candidates")
    old, task = seen_tasks[task_id], current_tasks[task_id]
    version = suggestion.get("task_version")
    if type(version) is not int or version != old.version:
        return decision("invalid", "missing or invalid observed goal version")
    if task.version != old.version or task.goal != old.goal or task.constraints != old.constraints:
        return decision("rejudge", "goal changed; reassess full goal and operations")
    if task.status != old.status or task.pending_question != old.pending_question:
        return decision("rejudge", "task state changed")
    if action == "execute" and task.status != "active":
        return decision("blocked", "task is not active")
    if not isinstance(suggestion.get("operation_refs"), list):
        return decision("invalid", "missing operation references")
    old_ops = {op.id: op for op in old.operations}
    ops = {op.id: op for op in task.operations}
    refs: set[str] = set()
    for ref in suggestion["operation_refs"]:
        # Reference validation is identification-only: the backend's stored
        # facts are authoritative; the model's claimed status/result never
        # overrides them (design: 模型建议不构成业务批准). Mismatches are ignored.
        if not isinstance(ref, dict) or "id" not in ref:
            return decision("invalid", "invalid operation reference")
        op_id = ref["id"]
        if isinstance(op_id, int):
            op_id = str(op_id)
        if not isinstance(op_id, str) or op_id in refs or op_id not in old_ops or op_id not in ops:
            return decision("invalid", "unknown or duplicate operation reference")
        refs.add(op_id)
    # Even unreferenced facts can affect the next choice.
    if old_ops.keys() != ops.keys() or any(old_ops[k] != ops[k] for k in old_ops):
        if action == "verify":
            target = suggestion.get("operation_id")
            if isinstance(target, str) and target in ops and target in old_ops and ops[target].status == "succeeded" and not ops[target].conflict:
                return decision("skip", "verification already succeeded", task_id=task_id, facts=(ops[target],))
        return decision("rejudge", "operation facts changed")
    if any(op.conflict for op in ops.values()):
        return decision("blocked", "conflicting business evidence requires adapter resolution")
    if action == "clarify":
        if not isinstance(suggestion.get("question"), str) or not suggestion["question"].strip():
            return decision("invalid", "missing clarification question")
        return decision("clarify", "ask user", action=action, task_id=task_id)
    if action == "modify_goal":
        if not isinstance(suggestion.get("goal"), str) or not suggestion["goal"].strip():
            return decision("invalid", "missing full proposed goal")
        return decision("rejudge", "goal change requires adoption and new version before actions",
                        action="modify_goal", task_id=task_id)
    if action == "verify":
        op_id = suggestion.get("operation_id")
        if not isinstance(op_id, str) or op_id not in refs or op_id not in ops:
            return decision("invalid", "verification must cite original operation")
        op = ops[op_id]
        if op.status not in _UNKNOWN or not op.queryable:
            return decision("blocked", "original operation is not queryable and unknown")
        return decision("allow", "query original operation only", action=action, task_id=task_id, operation_id=op_id, facts=(op,))
    if action == "reply":
        if set(ops) != refs:
            return decision("invalid", "reply must cite all task operation facts")
        return decision("allow", "render business results from facts only", action=action, task_id=task_id, facts=tuple(ops.values()))
    step_id = suggestion.get("step_id")
    steps = {step.id: step for step in task.steps}
    if not isinstance(step_id, str) or step_id not in steps or step_id not in {step.id for step in old.steps}:
        return decision("invalid", "unknown planned step")
    if old.steps != task.steps:
        return decision("rejudge", "planned steps changed")
    step = steps[step_id]
    try:
        args = _json(suggestion["args"])
    except (KeyError, TypeError, ValueError):
        return decision("invalid", "missing or invalid tool arguments")
    if suggestion.get("tool") != step.tool or args != step.args_json:
        return decision("invalid", "action differs from adapter-approved plan")
    if not step.authorized:
        return decision("blocked", "adapter has not cleared step")
    if any(op.step_id == step_id and op.status != "not_started" for op in ops.values()):
        return decision("blocked", "step already submitted or completed; never replay")
    if any(dep not in refs or ops[dep].status != "succeeded" for dep in step.depends_on):
        return decision("blocked", "dependency lacks reliable successful result")
    if any(op.status in _UNKNOWN for op in ops.values()):
        return decision("blocked", "unknown result requires adapter conflict clearance")
    if set(ops) != refs:
        return decision("invalid", "execution must cite all task operation facts")
    return decision("allow", "candidate for atomic dispatch check", action=action, task_id=task_id, step_id=step_id, facts=tuple(ops.values()))
