import json
from dataclasses import dataclass

# /list 里用于辨认会话的摘要长度
SUMMARY_MAX_CHARS = 24

# 切换会话时回显上一轮对话的长度
EXCHANGE_MAX_CHARS = 40
MAX_SESSION_ID = 2_147_483_647
MAX_REVISION = 9_223_372_036_854_775_807


def _field(item, name: str):
    """Items 里混着 dict 与 SDK 返回的对象，统一取字段"""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _to_plain(item) -> dict:
    """转成可 JSON 序列化的 dict；SDK 的 pydantic 对象不能直接 json.dumps"""
    if isinstance(item, dict):
        return item
    return item.model_dump(exclude_none=True)


def _text(item) -> str:
    """同时读取简单消息字符串与 Responses message 的 content parts。"""
    content = _field(item, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_field(part, "text") or "" for part in content)
    return ""


def _clip(text: str, limit: int) -> str:
    """压掉换行等空白再截断，否则多行内容会打乱单行排版"""
    single_line = " ".join(text.split())
    return f"{single_line[:limit]}…" if len(single_line) > limit else single_line


def _pending_approval(data: object) -> dict | None:
    if data is None:
        return None
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        raise ValueError("不支持的待审批状态版本")
    if (
        not isinstance(data.get("remaining_turns"), int)
        or isinstance(data.get("remaining_turns"), bool)
        or data["remaining_turns"] < 0
        or not isinstance(data.get("outputs_committed"), bool)
    ):
        raise ValueError("待审批状态包含无效运行信息")
    if not isinstance(data.get("calls"), list):
        raise ValueError("待审批状态缺少 calls")

    calls = []
    call_ids = set()
    for value in data["calls"]:
        if not isinstance(value, dict):
            raise ValueError("待审批工具调用必须是对象")
        permission = value.get("permission")
        if not isinstance(permission, dict) or permission.get("action") not in {
            "allow",
            "ask",
            "deny",
        }:
            raise ValueError("待审批工具调用缺少有效权限结果")
        requests = permission.get("requests")
        if not isinstance(requests, list) or any(
            not isinstance(request, dict)
            or not isinstance(request.get("permission"), str)
            or not request["permission"]
            or not isinstance(request.get("target"), str)
            for request in requests
        ):
            raise ValueError("待审批工具调用包含无效权限请求")
        if (
            not isinstance(value.get("id"), str)
            or not value["id"]
            or not isinstance(value.get("name"), str)
            or not value["name"]
            or not isinstance(value.get("args"), dict)
        ):
            raise ValueError("待审批工具调用缺少基本字段")
        if value["id"] in call_ids:
            raise ValueError("待审批工具调用 id 重复")
        call_ids.add(value["id"])
        if value.get("output") is not None and not isinstance(value["output"], str):
            raise ValueError("待审批工具调用包含无效输出")

        call = dict(value)
        if call.get("decision") == "denied":
            call["decision"] = None
            call["outcome"] = "denied"
        else:
            call.setdefault(
                "outcome",
                "rejected"
                if call.get("decision") == "rejected"
                else "completed"
                if call.get("output") is not None
                else None,
            )
        if call.get("decision") not in {None, "approved", "rejected"}:
            raise ValueError("待审批工具调用包含无效用户决定")
        if call.get("outcome") not in {
            None,
            "completed",
            "rejected",
            "denied",
            "failed",
        }:
            raise ValueError("待审批工具调用包含无效执行结果")

        action = call["permission"]["action"]
        decision = call.get("decision")
        outcome = call.get("outcome")
        output = call.get("output")
        if action == "allow" and (
            decision is not None or outcome != "completed" or not isinstance(output, str)
        ):
            raise ValueError("自动允许的工具调用缺少完成结果")
        if action == "deny" and (
            decision is not None or outcome != "denied" or not isinstance(output, str)
        ):
            raise ValueError("权限拒绝的工具调用缺少拒绝结果")
        if action == "ask":
            waiting = decision is None and outcome is None and output is None
            preview_failed = (
                decision is None
                and outcome == "failed"
                and isinstance(output, str)
            )
            approved_finished = (
                decision == "approved"
                and outcome in {"completed", "denied", "failed"}
                and isinstance(output, str)
            )
            rejected = (
                decision == "rejected"
                and outcome == "rejected"
                and isinstance(output, str)
            )
            if not (waiting or preview_failed or approved_finished or rejected):
                raise ValueError("待审批工具调用的决定与执行结果不一致")
        calls.append(call)

    normalized = dict(data)
    normalized["calls"] = calls
    return normalized


@dataclass
class Session:
    id: int
    items: list
    pending_approval: dict | None = None
    revision: int = 0

    @property
    def summary(self) -> str:
        """取首句用户输入作为摘要"""
        for item in self.items:
            if _field(item, "role") == "user":
                return _clip(_text(item), SUMMARY_MAX_CHARS)
        return "(空会话)"

    @property
    def last_exchange(self) -> tuple[str, str] | None:
        """最后一轮的用户提问与模型文本回复，切换会话时用来提示聊到哪了"""
        assistant_text = ""
        for item in reversed(self.items):
            role = _field(item, "role")
            content = _text(item)

            if role == "assistant" and content and not assistant_text:
                assistant_text = content
            if role == "user":
                return (
                    _clip(content or "", EXCHANGE_MAX_CHARS),
                    _clip(assistant_text, EXCHANGE_MAX_CHARS),
                )

        return None

    @property
    def is_empty(self) -> bool:
        """只有 system 消息、还没聊过的会话"""
        return not any(_field(item, "role") == "user" for item in self.items)

    def to_dict(self) -> dict:
        data = {
            "id": self.id,
            "items": [_to_plain(item) for item in self.items],
            "revision": self.revision,
        }
        if self.pending_approval is not None:
            data["pending_approval"] = self.pending_approval
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        if not isinstance(data, dict):
            raise ValueError("Session 必须是对象")

        session_id = data.get("id")
        if (
            not isinstance(session_id, int)
            or isinstance(session_id, bool)
            or session_id <= 0
            or session_id > MAX_SESSION_ID
        ):
            raise ValueError("Session id 必须是正整数")

        items = data.get("items")
        if not isinstance(items, list):
            raise ValueError("Session items 必须是数组")

        revision = data.get("revision", 0)
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
            or revision > MAX_REVISION
        ):
            raise ValueError("Session revision 必须是非负整数")

        pending_approval = _pending_approval(data.get("pending_approval"))
        if pending_approval is not None:
            if not pending_approval["calls"] or not any(
                call["permission"]["action"] == "ask"
                for call in pending_approval["calls"]
            ):
                raise ValueError("待审批状态必须包含需要用户决定的工具调用")

            function_calls: dict[str, list[object]] = {}
            for item in items:
                if _field(item, "type") == "function_call":
                    function_calls.setdefault(_field(item, "call_id"), []).append(item)

            for call in pending_approval["calls"]:
                matches = function_calls.get(call["id"], [])
                if len(matches) != 1 or _field(matches[0], "name") != call["name"]:
                    raise ValueError("待审批工具调用与 Items 中的 function_call 不匹配")
                arguments = _field(matches[0], "arguments")
                try:
                    parsed_arguments = json.loads(arguments)
                except (TypeError, ValueError) as error:
                    raise ValueError("Items 中的工具参数无效") from error
                if parsed_arguments != call["args"]:
                    raise ValueError("待审批工具参数与 Items 中的 function_call 不匹配")

        if pending_approval is not None:
            committed: dict[str, list[object]] = {}
            for item in items:
                if _field(item, "type") == "function_call_output":
                    committed.setdefault(_field(item, "call_id"), []).append(item)

            for call in pending_approval["calls"]:
                outputs = committed.get(call["id"], [])
                if pending_approval["outputs_committed"]:
                    if len(outputs) != 1 or _field(outputs[0], "output") != call["output"]:
                        raise ValueError(
                            "待审批状态标记已提交，但 Items 缺少匹配的工具结果"
                        )
                elif outputs:
                    raise ValueError("待审批状态尚未提交，但 Items 已包含工具结果")

        return cls(
            id=session_id,
            items=list(items),
            pending_approval=pending_approval,
            revision=revision,
        )
