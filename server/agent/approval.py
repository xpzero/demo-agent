from collections.abc import Iterable

from services.permission import (
    PermissionCheckContext,
    PermissionDecision,
    PermissionRequest,
    PermissionService,
)
from tools import (
    describe_tool_permissions,
    execute_approved_tool,
    execute_tool,
    preview_tool,
)


def _context(
    call_id: str, tool_name: str, session_id: int | None
) -> PermissionCheckContext:
    return PermissionCheckContext(
        call_id=call_id,
        tool_name=tool_name,
        session_id=session_id,
    )


def _request_data(request: PermissionRequest) -> dict:
    return {"permission": request.permission, "target": request.target}


def _check_call(
    call_id: str,
    tool_name: str,
    args: dict,
    permission_service: PermissionService,
    session_id: int | None,
) -> tuple[tuple[PermissionRequest, ...], PermissionDecision]:
    requests = describe_tool_permissions(tool_name, args)
    decision = permission_service.check(
        requests, _context(call_id, tool_name, session_id)
    )
    if decision.action not in {"allow", "ask", "deny"}:
        raise ValueError(f"权限服务返回了无效结果：{decision.action}")
    return requests, decision


def prepare_batch(
    parsed_calls: Iterable[tuple[object, dict]],
    remaining_turns: int,
    permission_service: PermissionService,
    session_id: int | None = None,
) -> dict:
    """执行已允许的工具，并为需要确认的调用建立审批批次。"""
    calls = []
    for call, args in parsed_calls:
        pending = {
            "id": call.call_id,
            "name": call.name,
            "args": args,
            "permission": None,
            "decision": None,
            "outcome": None,
            "output": None,
            "preview": None,
            "guard": None,
        }

        try:
            requests, authorization = _check_call(
                call.call_id, call.name, args, permission_service, session_id
            )
            pending["permission"] = {
                "action": authorization.action,
                "requests": [_request_data(request) for request in requests],
                "reason": authorization.reason,
            }
        except Exception as error:
            pending["permission"] = {
                "action": "deny",
                "requests": [],
                "reason": f"权限检查失败：{type(error).__name__}: {error}",
            }
            pending["decision"] = None
            pending["outcome"] = "denied"
            pending["output"] = (
                f"{call.name} 权限检查失败，工具未执行："
                f"{type(error).__name__}: {error}"
            )
            calls.append(pending)
            continue

        if authorization.action == "allow":
            pending["output"] = execute_tool(call.name, args)
            pending["outcome"] = "completed"
        elif authorization.action == "ask":
            try:
                preview = preview_tool(call.name, args)
                if preview is not None:
                    pending["guard"] = preview.pop("_guard", None)
                pending["preview"] = preview
            except Exception as error:
                pending["outcome"] = "failed"
                pending["output"] = (
                    f"{call.name} 无法进入审批：{type(error).__name__}: {error}"
                )
        else:
            pending["outcome"] = "denied"
            detail = authorization.reason or "当前权限规则禁止该操作"
            pending["output"] = f"权限规则拒绝执行工具 {call.name}：{detail}；工具未执行。"

        calls.append(pending)

    return {
        "schema_version": 2,
        "remaining_turns": remaining_turns,
        "outputs_committed": False,
        "calls": calls,
    }


def pending_call_ids(batch: dict) -> list[str]:
    return [
        call["id"]
        for call in batch["calls"]
        if call["permission"]["action"] == "ask"
        and call["decision"] is None
        and call.get("outcome") is None
    ]


def decide(batch: dict, call_id: str, approved: bool) -> dict:
    """记录一次审批决定；相同决定可重试，相反决定会被拒绝。"""
    call = next((entry for entry in batch["calls"] if entry["id"] == call_id), None)
    if call is None or call["permission"]["action"] != "ask":
        raise KeyError(call_id)

    decision = "approved" if approved else "rejected"
    if call["decision"] == decision:
        return call
    if call.get("outcome") is not None:
        raise KeyError(call_id)
    if call["decision"] is not None:
        raise ValueError(f"工具调用 {call_id} 已经被{_decision_label(call['decision'])}")

    call["decision"] = decision
    if not approved:
        call["output"] = f"用户拒绝执行工具 {call['name']}；工具未执行。"
        call["outcome"] = "rejected"
    return call


def is_ready(batch: dict) -> bool:
    return not pending_call_ids(batch)


def execute_approved_call(
    call: dict,
    permission_service: PermissionService,
    session_id: int | None = None,
) -> str:
    if call["decision"] != "approved":
        raise ValueError(f"工具调用 {call['id']} 尚未获批")
    if call["output"] is None:
        saved_requests = call["permission"]["requests"]
        try:
            requests, authorization = _check_call(
                call["id"],
                call["name"],
                call["args"],
                permission_service,
                session_id,
            )
            if [_request_data(request) for request in requests] != saved_requests:
                raise ValueError("工具权限请求在审批后发生变化")
            if authorization.action == "deny":
                detail = authorization.reason or "当前权限规则禁止该操作"
                call["output"] = (
                    f"权限规则拒绝执行工具 {call['name']}：{detail}；工具未执行。"
                )
                call["outcome"] = "denied"
            else:
                call["output"] = execute_approved_tool(
                    call["name"], call["args"], call.get("guard")
                )
                call["outcome"] = "completed"
        except Exception as error:
            call["output"] = (
                f"{call['name']} 重新检查权限失败，工具未执行："
                f"{type(error).__name__}: {error}"
            )
            call["outcome"] = "failed"
    return call["output"]


def commit_outputs(
    items: list,
    batch: dict,
    permission_service: PermissionService,
    session_id: int | None = None,
) -> list[dict]:
    """只执行已批准调用一次，并按原调用顺序提交所有 function_call_output。"""
    if not is_ready(batch):
        raise ValueError("仍有工具调用等待审批")

    for call in batch["calls"]:
        if call["output"] is None:
            execute_approved_call(call, permission_service, session_id)

    if not batch["outputs_committed"]:
        items.extend(
            {
                "type": "function_call_output",
                "call_id": call["id"],
                "output": call["output"],
            }
            for call in batch["calls"]
        )
        batch["outputs_committed"] = True

    return [
        {
            "type": "tool_result",
            "id": call["id"],
            "content": call["output"],
            "outcome": call.get("outcome") or "completed",
        }
        for call in batch["calls"]
    ]


def _decision_label(decision: str) -> str:
    return "批准" if decision == "approved" else "拒绝"
