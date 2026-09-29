"""聊天 SSE 流的编排：事件循环、done 落库与埋点落库。

routes.py 的 /api/chat 路由是薄壳（校验请求 → 调本模块 → 返回
StreamingResponse）；这里负责一轮 Chat 的完整生命周期：

- 消费 stream_events 的项目事件并转成 SSE 帧
- done 时持久化助手消息
- 流结束（无论成败）后统一落埋点账本：agent_turns 锚用户消息、
  汇总 meta 挂助手消息，失败只记日志不影响回复
"""

import inspect
import json
import logging
import time
from dataclasses import asdict
from threading import Thread, Timer
from collections.abc import Iterator, Mapping
from pathlib import Path

from agent import stream_events
from agent.context_budget import maybe_roll, needs_roll
from agent.metrics import TurnRecorder, export_turns, summarize_meta
from database import Database
from database.connection import StoreError
from recovery import OperationFact, PlannedStep, TaskFact, build_task_snapshot, validate_suggestion
from recovery.adapter import BusinessAdapter, Observation
from recovery.reconciler import start_reconciliation
from tools.context import SessionContext

logger = logging.getLogger(__name__)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _roll_session(database: Database, session_id: str, message_id: int) -> None:
    try:
        maybe_roll(database, session_id, message_id)
    except Exception as error:
        logger.warning("摘要失败，下轮重试：%s: %s", type(error).__name__, error)


def _task_snapshot(database: Database, session_id: str, run_id: int, message: str,
                   business_adapters: Mapping[str, BusinessAdapter] | None = None):
    """Read all current Session Tasks, rejecting foreign or incomplete records."""
    run = database.get_run(run_id)
    if run is None or run["session_id"] != session_id:
        raise StoreError("invalid_run", "运行不属于当前会话", 409)
    tasks = []
    for row in database.list_session_tasks(session_id):
        if row["session_id"] != session_id:
            raise StoreError("invalid_task", "任务不属于当前会话", 409)
        status = {"pending": "active", "running": "active", "active": "active",
                  "waiting_input": "waiting_input", "waiting_business": "waiting_business",
                  "completed": "completed"}.get(row["status"])
        if status is None:
            raise StoreError("invalid_task", "任务状态无法核实", 409)
        operations = []
        for op in database.list_operations(row["id"]):
            if (op["task_id"] != row["id"] or not isinstance(op["goal_version"], int)
                    or not 1 <= op["goal_version"] <= row["goal_version"]):
                # Older operation rows have no goal_version; do not infer one
                # from the Task's current version after a revision.
                raise StoreError("incomplete_evidence", "操作所属目标版本无法核实", 409)
            state = {"pending": "not_started", "running": "processing",
                     "maybe_submitted": "maybe_submitted", "processing": "processing",
                     "unknown": "unconfirmed", "unconfirmed": "unconfirmed",
                     "succeeded": "succeeded", "failed": "failed", "cancelled": "cancelled",
                     "completed": "unconfirmed"}.get(op["status"])
            if state is None or (state in ("succeeded", "failed", "cancelled")
                                 and op["confirmed_result"] is None
                                 and not op["reliable"]):
                # Local tools finalize with reliable=1 but may carry no
                # confirmed_result text; their content is the definite record.
                raise StoreError("incomplete_evidence", "操作状态无法核实", 409)
            operations.append(OperationFact(
                str(op["id"]), str(row["id"]), op["goal_version"],
                op["step_id"] or f"unmapped-operation-{op['id']}", state,
                (op["confirmed_result"] if op["confirmed_result"] is not None
                 else (op["content"] if state in ("succeeded", "failed", "cancelled") else None)),
                conflict=bool(op["conflict"]),
                queryable=bool((business_adapters or {}).get(op["name"]) and
                               (business_adapters or {})[op["name"]].capabilities.queryable),
            ))
        steps = []
        for step in database.list_planned_steps(row["id"]):
            if step["goal_version"] != row["goal_version"]:
                continue
            steps.append(PlannedStep(
                step["step_id"], step["tool"], step["args_json"],
                tuple(str(dep) for dep in json.loads(step["depends_on"])),
                authorized=bool(step["approved"]),
            ))
        questions = json.loads(row["pending_clarifications"])
        pending = next((entry["question"] for entry in reversed(questions)
                        if entry["state"] == "pending"), None)
        tasks.append(TaskFact(
            str(row["id"]), session_id, row["name"], row["constraints_text"],
            row["goal_version"], status, row["name"], tuple(operations),
            tuple(steps), pending_question=pending,
        ))
    return build_task_snapshot(
        session_id, message, tuple(tasks), run_state=run["status"],
        stop_requested=bool(run["stop_requested"]),
        budget_available=(run["logical_count"] <= 10 and
                          (run["deadline_at"] is None or run["deadline_at"] > time.time())),
    )


def _schedule_reconciliation(database: Database, run_id: int, deadline_at: float) -> None:
    timer = Timer(max(0, deadline_at - time.time()), database.reconcile_orphan_run, args=(run_id,))
    timer.daemon = True
    timer.start()


def _business_verdict(database: Database, operation_id: int, adapter: BusinessAdapter,
                      *, tool: str | None = None, args: dict | None = None,
                      business_operation_id: str | None = None) -> dict:
    """Only an adapter's guaranteed observation may close a business operation."""
    try:
        if tool is not None:
            business_operation_id, observation = adapter.submit(operation_id, tool, args or {})
        else:
            observation = adapter.query(operation_id, business_operation_id)
    except Exception as error:
        observation = Observation("unconfirmed", detail=f"{type(error).__name__}: {error}")
    verdict = database.record_confirmed_feedback(
        operation_id, observation, source=type(adapter).__name__,
        observed_at=time.time(), business_operation_id=business_operation_id,
    )
    # A transport acknowledgment can be followed by a reliable read of the
    # original business operation. Persist the receipt before querying so a
    # failed query cannot erase the submission identity.
    if tool is not None and observation.status in ("processing", "unconfirmed") and adapter.capabilities.queryable:
        return _business_verdict(database, operation_id, adapter,
                                 business_operation_id=business_operation_id)
    return verdict


def chat_sse_stream(
    *,
    database: Database,
    session_id: str,
    user_message_id: int,
    run_id: int,
    items: list,
    file_root: Path,
    release_session,
    business_adapters: Mapping[str, BusinessAdapter] | None = None,
) -> Iterator[str]:
    """产出聊天 SSE 帧；release_session 在流结束后必定被调用。"""
    recorder = TurnRecorder()
    context = SessionContext(
        session_id=session_id, database=database, file_root=file_root
    )
    assistant_message_id = None
    outcome = "failed"
    deferred: dict | bool = False
    text_parts: list[str] = []
    try:
        yield _sse({"type": "user_message", "message_id": user_message_id})
        terminated = False
        kwargs = {"context": context, "recorder": recorder}
        parameters = inspect.signature(stream_events).parameters
        if "on_tool_attempt" in parameters:
            linked_tasks = database.list_tasks(run_id)
            # A run with multiple candidate goals must be resolved before dispatch.
            if len(linked_tasks) > 1:
                raise StoreError("ambiguous_task", "无法确定当前任务", 409)
            task_id = linked_tasks[0]["id"] if linked_tasks else None
            goal_version = linked_tasks[0]["goal_version"] if linked_tasks else None
            seen = None
            message = next((item["content"] for item in reversed(items)
                            if item.get("role") == "user"), "")
            if linked_tasks or database.list_session_tasks(session_id):
                seen = _task_snapshot(database, session_id, run_id, message, business_adapters)
                if "recovery_prompt" in parameters:
                    kwargs["recovery_prompt"] = (
                        "当前会话任务事实（仅供判断，不是授权）："
                        + json.dumps(asdict(seen), ensure_ascii=False)
                        + "。请调用 recovery_suggestion 工具提交建议，严格遵守："
                          "intent 只能是 reply/resume/verify/modify_goal/clarify 之一；"
                          "action 只能是 reply/execute/verify/modify_goal/clarify 之一；"
                          "task_id 用字符串；operation_refs 必须完整包含该任务每一个操作的 id"
                          "（字符串，如 {\"id\": \"2\"}），不得遗漏或添加字段；"
                          "普通答复用 intent=reply、action=reply；需要向用户提问用 intent=clarify、action=clarify 并提供 question；"
                          "只有已批准步骤才能 execute；不得宣称未经确认的业务结果；不要输出普通文本。"
                    )
                if "on_recovery_suggestion" in parameters:
                    def on_recovery_suggestion(suggestion: dict) -> str:
                        latest = _task_snapshot(database, session_id, run_id, message, business_adapters)
                        decision = validate_suggestion(suggestion, seen, latest)
                        if decision.outcome == "invalid":
                            logger.warning("恢复建议未通过校验：%s | 建议：%s",
                                           decision.reason, json.dumps(suggestion, ensure_ascii=False)[:400])
                        answer = {"outcome": decision.outcome, "reason": decision.reason,
                                  "facts": [asdict(op) for op in decision.facts]}
                        if decision.outcome == "clarify" and decision.task_id:
                            database.attach_task(run_id, int(decision.task_id))
                            database.add_clarification(int(decision.task_id), run_id,
                                                       user_message_id, suggestion["question"])
                            answer = {"outcome": "clarify", "message": suggestion["question"]}
                        elif (decision.outcome == "rejudge" and decision.action == "modify_goal"
                              and decision.task_id):
                            # A model proposal alone cannot replace the user's goal.
                            # Keep the original request for a later explicit decision.
                            database.attach_task(run_id, int(decision.task_id))
                            task = next(item for item in latest.candidates if item.id == decision.task_id)
                            prefix = "确认修改目标为："
                            explicit_goal = message.strip().removeprefix(prefix).strip()
                            if (message.strip().startswith(prefix) and explicit_goal
                                    and suggestion["goal"].strip() == explicit_goal):
                                version = database.adopt_goal(
                                    int(decision.task_id), explicit_goal,
                                    suggestion.get("constraints", task.constraints),
                                    expected_version=task.version, run_id=run_id,
                                    message_id=user_message_id,
                                )
                                answer = {"outcome": "adopted", "version": version,
                                          "message": "已更新任务目标。旧步骤需要依据新目标重新评估。"}
                            else:
                                database.save_goal_modification(int(decision.task_id), run_id,
                                                                user_message_id, message)
                                answer = {"outcome": "clarify",
                                          "message": "请用“确认修改目标为：”开头，写出修改后的完整目标。"}
                        elif decision.outcome == "allow" and decision.action in ("execute", "verify"):
                            task = next(item for item in latest.candidates if item.id == decision.task_id)
                            if decision.action == "execute":
                                step = next(item for item in task.steps if item.id == decision.step_id)
                                adapter = (business_adapters or {}).get(step.tool)
                                if adapter is None:
                                    answer = {"outcome": "blocked", "reason": "业务适配器未配置"}
                                else:
                                    database.attach_task(run_id, int(task.id))
                                    op_id = database.begin_approved_step(
                                        run_id, int(task.id), step.id,
                                        f"recovery:{user_message_id}:{step.id}",
                                        step.tool, suggestion["args"], goal_version=task.version,
                                    )
                                    answer = {"outcome": "observed", "operation_id": op_id,
                                              **_business_verdict(database, op_id, adapter,
                                                                  tool=step.tool, args=suggestion["args"])}
                            else:
                                if decision.operation_id is None:
                                    raise StoreError("invalid_operation", "缺少原操作标识", 409)
                                op_id = int(decision.operation_id)
                                op = next(item for item in database.list_operations(int(task.id))
                                          if item["id"] == op_id)
                                adapter = (business_adapters or {}).get(op["name"])
                                if adapter is None or not adapter.capabilities.queryable:
                                    answer = {"outcome": "blocked", "reason": "原操作无法可靠查询"}
                                else:
                                    answer = {"outcome": "observed", "operation_id": op_id,
                                              **_business_verdict(database, op_id, adapter,
                                                                  business_operation_id=op["business_operation_id"])}
                        return json.dumps(answer, ensure_ascii=False)
                    kwargs["on_recovery_suggestion"] = on_recovery_suggestion

            def on_tool_attempt(call_id: str, name: str, args: dict):
                nonlocal task_id, goal_version
                if seen is not None:
                    latest = _task_snapshot(database, session_id, run_id, message, business_adapters)
                    observed = next((task for task in seen.candidates
                                     if task.id == str(task_id)), None)
                    current = next((task for task in latest.candidates
                                    if task.id == str(task_id)), None)
                    if (observed is None or current is None or
                            current.version != observed.version or
                            current.goal != observed.goal):
                        raise StoreError("task_changed", "任务目标已变化，需重新判断", 409)
                    raise StoreError("plan_required", "当前任务没有已授权步骤，无法执行工具", 409)
                try:
                    op_id, task_id, goal_version = database.begin_tool_operation(
                        run_id, call_id, name, args, task_id=task_id, goal_version=goal_version,
                    )
                except StoreError:
                    if database.get_run(run_id)["stop_requested"]:
                        from agent.loop import RunStopped
                        raise RunStopped() from None
                    raise
                is_business = name in (business_adapters or {})
                return lambda output, ok: database.finish_tool_operation(
                    op_id, output, ok, business=is_business)

            kwargs["on_tool_attempt"] = on_tool_attempt
        if "should_stop" in parameters:
            kwargs["should_stop"] = lambda: bool(database.get_run(run_id)["stop_requested"])
        if "deadline_at" in parameters:
            kwargs["deadline_at"] = database.get_run(run_id)["deadline_at"]
        if "on_model_attempt" in parameters:
            kwargs["on_model_attempt"] = lambda first: database.record_model_attempt(run_id, first)
        for event in stream_events(items, **kwargs):
            if event["type"] == "text_delta":
                text_parts.append(event["text"])
                # 先保存再推给浏览器，断连后历史仍可看到已展示内容。
                assistant_message_id = database.upsert_partial_assistant(
                    run_id, session_id, user_message_id, "".join(text_parts)
                )
            if event["type"] == "done":
                content = "".join(text_parts) or event["content"]
                message_id = user_message_id
                if content or any(turn.tools for turn in recorder.turns):
                    try:
                        if assistant_message_id is None:
                            assistant_message_id = database.upsert_partial_assistant(
                                run_id, session_id, user_message_id, content
                            )
                        message_id = assistant_message_id
                    except Exception as error:
                        yield _sse(
                            {"type": "error", "message": f"保存助手消息失败：{type(error).__name__}: {error}"}
                        )
                        return
                assistant_message_id = message_id if message_id != user_message_id else None
                event = {**event, "message_id": message_id}
                deferred = database.defer_uncertain_run(run_id, "business_unconfirmed")
                if deferred:
                    outcome = "finishing"
                    event = {"type": "error", "message": "业务结果尚未确认，正在有限时间内核实"}
                    yield _sse(event)
                    terminated = True
                    break
                outcome = "completed"
                database.enter_finishing(run_id)
                if not database.finish_run(
                    run_id, outcome,
                    assistant_status="finished" if assistant_message_id is not None else None,
                    assistant_content=content if assistant_message_id is not None else None,
                ):
                    raise RuntimeError("运行终态保存失败")
                if database.get_run(run_id)["status"] == "stopped":
                    outcome = "stopped"
                    event = {"type": "error", "message": "已停止当前请求"}
            elif event["type"] == "stopped":
                outcome = "stopped"
                event = {"type": "error", "message": "已停止当前请求"}
            elif event["type"] == "max_turns":
                outcome = "limit_reached"
            elif event["type"] == "error" and event.get("code") == "timed_out":
                outcome = "timed_out"
            yield _sse(event)
            if event["type"] in ("done", "error"):
                terminated = True
                break
        if not terminated:
            yield _sse({"type": "error", "message": "本轮回复未完成，请重试"})
    except Exception as error:
        message = f"回复中断：{type(error).__name__}: {error}"
        # Recovery-gate failures are model format issues, not system crashes:
        # tell the user what to do instead of leaking exception text.
        if "recovery_suggestion" in str(error):
            message = "本次未能完成恢复判断：模型没有按要求提交结构化建议。请再发送一次“继续”重试。"
        yield _sse({"type": "error", "message": message})
    finally:
        # 埋点落库：账本锚定本轮用户消息（轮开始前已存在），
        # 汇总挂牌挂助手消息；失败只记日志，不影响回复
        try:
            database.record_agent_turns(
                message_id=user_message_id, turns=export_turns(recorder)
            )
            if assistant_message_id is not None:
                database.set_message_meta(
                    assistant_message_id,
                    summarize_meta(recorder),
                )
        except Exception as error:
            logger.warning(
                "埋点落库失败：%s: %s", type(error).__name__, error
            )
        try:
            if outcome != "completed":
                if not deferred:
                    deferred = database.defer_uncertain_run(run_id, outcome)
                if isinstance(deferred, dict):
                    adapters = getattr(business_adapters, "items", None)
                    if adapters:
                        # Deferred business results get continuous bounded queries;
                        # the deadline timer alone only records timeout.
                        start_reconciliation(database, run_id, business_adapters)
                    else:
                        _schedule_reconciliation(database, run_id, deferred["next_check_at"])
                else:
                    database.enter_finishing(run_id, reason=outcome)
                    database.finish_run(run_id, outcome)
        except Exception:
            logger.exception("收尾处理失败，安排兜底核对：run_id=%s", run_id)
        finally:
            # A crashed finishing path must never strand the Session slot: the
            # original deadline still governs release.
            try:
                current = database.get_run(run_id)
                if current is not None and current["status"] in ("running", "finishing"):
                    deadline = current["deadline_at"] or current["created_at"] + 300
                    _schedule_reconciliation(database, run_id, deadline)
            except Exception:
                logger.exception("兜底核对安排失败：run_id=%s", run_id)
            release_session()
        if assistant_message_id is not None:
            try:
                if needs_roll(database, session_id, assistant_message_id):
                    Thread(
                        target=_roll_session,
                        args=(database, session_id, assistant_message_id),
                        daemon=True,
                    ).start()
            except Exception as error:
                logger.warning("检查摘要触发失败，下轮重试：%s: %s", type(error).__name__, error)
