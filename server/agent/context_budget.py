"""会话上下文：保留原始消息链，用滚动摘要代表较早的消息。"""

import os

from .client import MODEL, client

CONTEXT_BUDGET = 24_000
DEFAULT_ROLL_TRIGGER = 12_000
RECENT_BUDGET = 8_000
ABSORB_BUDGET = 8_000
SUMMARY_MAX_TOKENS = 2_000
SUMMARY_PREFIX = "以下是早期对话摘要：\n"


def estimate_tokens(text: str) -> int:
    """保守估算 token 数；不引入 tokenizer。"""
    return len(text)


def to_chat_messages(rows: list[dict]) -> list[dict]:
    return [{"role": row["role"], "content": row["content"]} for row in rows]


def pick_recent_within(messages: list[dict], budget: int) -> list[dict]:
    """从最新往回保留，至少保留一条。"""
    kept: list[dict] = []
    used = 0
    for message in reversed(messages):
        cost = estimate_tokens(message.get("content") or "")
        if kept and used + cost > budget:
            break
        kept.append(message)
        used += cost
    kept.reverse()
    return kept


def build_context(
    system_prompt: str, chain_rows: list[dict], budget: int = CONTEXT_BUDGET,
    *, summary: str | None = None, summary_upto_message_id: int | None = None,
) -> list[dict]:
    """游标前由摘要代言，游标后按预算保留近期原话。"""
    system = {"role": "system", "content": system_prompt}
    summary_message = (
        {"role": "user", "content": SUMMARY_PREFIX + summary}
        if summary and summary_upto_message_id is not None else None
    )
    living = [row for row in chain_rows
              if summary_upto_message_id is None or row["id"] > summary_upto_message_id]
    reserved = estimate_tokens(system_prompt) + (
        estimate_tokens(summary_message["content"]) if summary_message else 0
    )
    kept = pick_recent_within(to_chat_messages(living), max(0, budget - reserved))
    return [system, *([summary_message] if summary_message else []), *kept]


def roll_trigger() -> int:
    """从环境变量读取摘要触发字符数，空值使用默认值。"""
    value = os.getenv("SUMMARY_ROLL_TRIGGER") or str(DEFAULT_ROLL_TRIGGER)
    try:
        trigger = int(value)
    except ValueError as error:
        raise ValueError("SUMMARY_ROLL_TRIGGER 必须是正整数") from error
    if trigger <= 0:
        raise ValueError("SUMMARY_ROLL_TRIGGER 必须是正整数")
    return trigger


def choose_to_absorb(
    living: list[dict], trigger: int | None = None,
    recent_budget: int = RECENT_BUDGET, absorb_budget: int = ABSORB_BUDGET,
) -> list[dict]:
    """每次最多收编约 8K；积压时从游标之后最旧的部分逐次补吃。"""
    if trigger is None:
        trigger = roll_trigger()
    if sum(estimate_tokens(row["content"]) for row in living) <= trigger:
        return []
    recent = pick_recent_within(living, recent_budget)
    candidates = living[:len(living) - len(recent)]
    chosen: list[dict] = []
    used = 0
    for row in candidates:
        cost = estimate_tokens(row["content"])
        if used + cost > absorb_budget:
            if chosen:
                break
            # 单条原文超过上限时只向摘要模型提供前 8K，避免无限重试。
            chosen.append({**row, "content": row["content"][:absorb_budget]})
            break
        chosen.append(row)
        used += cost
    return chosen


def summarize(old_summary: str | None, rows: list[dict]) -> str:
    """独立模型调用返回完整新摘要，不把摘要写入消息链。"""
    previous = old_summary or "（无）"
    transcript = "\n".join(f"{row['role']}: {row['content']}" for row in rows)
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": (
                "合并旧摘要和新对话，输出一份完整的中文历史摘要。保留用户目标、决定、"
                "具体数字、约束和未完成事项；省略寒暄及过程，不要编造。"
            )},
            {"role": "user", "content": f"旧摘要：\n{previous}\n\n新收编的对话：\n{transcript}"},
        ],
        max_tokens=SUMMARY_MAX_TOKENS,
    )
    text = response.choices[0].message.content
    if not text or not text.strip():
        raise ValueError("摘要模型返回空内容")
    return text.strip()


def needs_roll(database, session_id: str, upto_message_id: int) -> bool:
    """快速检查触发线；避免短会话每轮启动一个后台线程。"""
    state = database.get_session_summary(session_id)
    if state is None:
        return False
    _, cursor = state
    chain = database.get_message_chain(session_id, upto_message_id)
    living = [row for row in chain if cursor is None or row["id"] > cursor]
    return bool(choose_to_absorb(living))


def maybe_roll(database, session_id: str, upto_message_id: int) -> bool:
    """回复完成后的旁路任务；失败时不推进游标，下一轮继续尝试。"""
    state = database.get_session_summary(session_id)
    if state is None:
        return False
    old_summary, cursor = state
    chain = database.get_message_chain(session_id, upto_message_id)
    living = [row for row in chain if cursor is None or row["id"] > cursor]
    batch = choose_to_absorb(living)
    if not batch:
        return False
    new_summary = summarize(old_summary, batch)
    return database.update_session_summary(session_id, cursor, new_summary, batch[-1]["id"])
