from dataclasses import dataclass

# /list 里用于辨认会话的摘要长度
SUMMARY_MAX_CHARS = 24

# 切换会话时回显上一轮对话的长度
EXCHANGE_MAX_CHARS = 40


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
            or not isinstance(value.get("name"), str)
            or not isinstance(value.get("args"), dict)
        ):
            raise ValueError("待审批工具调用缺少基本字段")

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
        calls.append(call)

    normalized = dict(data)
    normalized["calls"] = calls
    return normalized


@dataclass
class Session:
    id: int
    items: list
    pending_approval: dict | None = None

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
        data = {"id": self.id, "items": [_to_plain(item) for item in self.items]}
        if self.pending_approval is not None:
            data["pending_approval"] = self.pending_approval
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(
            id=int(data["id"]),
            items=list(data["items"]),
            pending_approval=_pending_approval(data.get("pending_approval")),
        )
