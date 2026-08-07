"""Agent 内核：模型客户端与 agent loop，只产出事件，不负责呈现。"""

from .client import MODEL, SYSTEM_PROMPT, client
from .loop import stream_events

__all__ = ["MODEL", "SYSTEM_PROMPT", "client", "stream_events"]
