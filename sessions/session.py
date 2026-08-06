from dataclasses import dataclass

# /list 里用于辨认会话的摘要长度
SUMMARY_MAX_CHARS = 24

# 切换会话时回显上一轮对话的长度
EXCHANGE_MAX_CHARS = 40


def _field(message, name: str):
    """messages 里混着 dict 与 SDK 返回的对象，统一取字段"""
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def _to_plain(message) -> dict:
    """转成可 JSON 序列化的 dict；SDK 的 pydantic 对象不能直接 json.dumps"""
    if isinstance(message, dict):
        return message
    return message.model_dump(exclude_none=True)


def _clip(text: str, limit: int) -> str:
    """压掉换行等空白再截断，否则多行内容会打乱单行排版"""
    single_line = " ".join(text.split())
    return f"{single_line[:limit]}…" if len(single_line) > limit else single_line


@dataclass
class Session:
    id: int
    messages: list

    @property
    def summary(self) -> str:
        """取首句用户输入作为摘要"""
        for message in self.messages:
            if _field(message, "role") == "user":
                return _clip(_field(message, "content") or "", SUMMARY_MAX_CHARS)
        return "(空会话)"

    @property
    def last_exchange(self) -> tuple[str, str] | None:
        """最后一轮的用户提问与模型文本回复，切换会话时用来提示聊到哪了"""
        assistant_text = ""
        for message in reversed(self.messages):
            role = _field(message, "role")
            content = _field(message, "content")

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
        return not any(_field(m, "role") == "user" for m in self.messages)

    def to_dict(self) -> dict:
        return {"id": self.id, "messages": [_to_plain(m) for m in self.messages]}

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(id=int(data["id"]), messages=list(data["messages"]))
