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


@dataclass
class Session:
    id: int
    items: list

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
        return {"id": self.id, "items": [_to_plain(item) for item in self.items]}

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(id=int(data["id"]), items=list(data["items"]))
