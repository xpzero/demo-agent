"""运行埋点：记录一次 Chat 内逐次模型请求与工具执行的观测数据。

只管记，不管存——recorder 是内存账本，落库由 api.py 在 SSE 结束时
统一执行（loop 不知数据库存在）。测试时把它换成内存假账本即可。
"""

from dataclasses import dataclass, field
from typing import Optional

from .context_budget import estimate_tokens

# 截断副本上限：与 LangSmith / LangFuse 等观测平台一致的取舍，
# 账本要的是「足以判断这次执行正常吗」，全量留在原始来源
EXCERPT_LIMIT = 500


def excerpt(value, limit: int = EXCERPT_LIMIT) -> str:
    """把任意值转成不超过 limit 字符的截断副本。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value[:limit]


@dataclass
class ToolRecord:
    """一次工具执行的观测记录（tool_runs 表的一行前身）。"""

    name: str
    args_excerpt: str
    result_excerpt: str
    duration_ms: float
    ok: bool


@dataclass
class TurnRecord:
    """一次模型请求的观测记录（agent_turns 表的一行前身）。

    usage 是流式最后一个 chunk 携带的用量字典；接口不返回时为 None，
    由 api.py 侧按文本长度估算并标 estimated。items_snapshot 是该次
    请求发出时的 messages 快照（估算 prompt 用量的依据）。
    """

    started_monotonic: Optional[float] = None
    duration_ms: Optional[float] = None
    usage: Optional[dict] = None
    items_snapshot: Optional[list] = None
    reply_text: Optional[str] = None
    tools: list[ToolRecord] = field(default_factory=list)
    ok: bool = True


class TurnRecorder:
    """一次 Chat 的内存账本：turns 按发生顺序累积。"""

    def __init__(self):
        self.turns: list[TurnRecord] = []

    def start_turn(self) -> TurnRecord:
        record = TurnRecord()
        self.turns.append(record)
        return record

    def track_turn(self):
        """上下文管理器形态：进入即登记，异常也不丢账。"""
        import time
        from contextlib import contextmanager

        @contextmanager
        def _track():
            record = self.start_turn()
            record.started_monotonic = time.monotonic()
            try:
                yield record
            finally:
                record.duration_ms = (
                    time.monotonic() - record.started_monotonic
                ) * 1000

        return _track()

    @property
    def total_usage(self) -> Optional[dict]:
        """把各 turn 的 usage 求和；全部缺失时为 None。"""
        totals: dict[str, int] = {}
        for record in self.turns:
            if record.usage:
                for key, value in record.usage.items():
                    if isinstance(value, int):
                        totals[key] = totals.get(key, 0) + value
        return totals or None


def export_turns(recorder: TurnRecorder) -> list[dict]:
    """把内存账本导出为落库行（agent 层数据结构 → database 参数）。

    有实测 usage 用实测；没有则按该次请求的 items 快照估算 prompt、
    按回复文本估算 completion——同一行里估算值与实测值分列存放，
    事后可用实测校准估算。
    """
    turns = []
    for record in recorder.turns:
        usage = record.usage or {}
        estimated_prompt = None
        if not usage and record.items_snapshot is not None:
            estimated_prompt = sum(
                estimate_tokens(message.get("content") or "")
                for message in record.items_snapshot
                if isinstance(message, dict)
            )
        estimated_completion = None
        if not usage and record.reply_text is not None:
            estimated_completion = estimate_tokens(record.reply_text)
        turns.append(
            {
                "duration_ms": record.duration_ms,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "estimated_prompt_tokens": (
                    usage.get("prompt_tokens") or estimated_prompt
                ),
                "estimated_completion_tokens": (
                    usage.get("completion_tokens") or estimated_completion
                ),
                "tools": [
                    {
                        "name": tool.name,
                        "args_excerpt": tool.args_excerpt,
                        "result_excerpt": tool.result_excerpt,
                        "duration_ms": tool.duration_ms,
                        "ok": tool.ok,
                    }
                    for tool in record.tools
                ],
            }
        )
    return turns


def summarize_meta(recorder: TurnRecorder) -> dict:
    """助手消息的汇总挂牌：生成该回答的成本，done 落库时顺手写入。"""
    total = recorder.total_usage or {}
    estimated = not bool(total)
    estimated_prompt = 0
    if estimated:
        for record in recorder.turns:
            if record.items_snapshot is not None:
                for message in record.items_snapshot:
                    if isinstance(message, dict):
                        estimated_prompt += estimate_tokens(
                            message.get("content") or ""
                        )
    return {
        "turns": len(recorder.turns),
        "prompt_tokens": total.get("prompt_tokens"),
        "completion_tokens": total.get("completion_tokens"),
        "estimated": estimated,
        "estimated_prompt_tokens": total.get("prompt_tokens") or estimated_prompt,
    }
