"""上下文预算控制：构造发给模型的 items 时的截断（trim）策略。

存储 ≠ 上下文：SQLite 存全量原文（前端历史展示的事实来源），
本模块只决定「这一轮模型能看到什么」。token 估算用 len(text)
一字一 token 的保守估算——对中文偏大、对英文更偏大，方向安全：
宁可提前截断，不可超限报错。
"""

CONTEXT_BUDGET = 24_000


def estimate_tokens(text: str) -> int:
    """保守估算 token 数：一字一 token，不引入 tokenizer 依赖。"""
    return len(text)


def to_chat_messages(rows: list[dict]) -> list[dict]:
    """把 chat_messages 链行（旧→新）映射成 Chat Completions 消息。"""
    return [{"role": row["role"], "content": row["content"]} for row in rows]


def pick_recent_within(messages: list[dict], budget: int) -> list[dict]:
    """从最新往回圈保留区，超预算丢最旧；至少保留最新一条。

    链天然有序（父指针 + id 自增），「最旧」就是列表开头，不靠额外机制。
    """
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
    system_prompt: str, chain_rows: list[dict], budget: int = CONTEXT_BUDGET
) -> list[dict]:
    """构造本轮发给模型的 items：system 永在 + 截断后的历史。

    system 的开销计入总预算。被截掉的最旧历史本轮对模型不可见，
    但库里完整保留（摘要阶段引入后由摘要代言）。
    """
    system_message = {"role": "system", "content": system_prompt}
    history_budget = max(0, budget - estimate_tokens(system_prompt))
    kept = pick_recent_within(to_chat_messages(chain_rows), history_budget)
    return [system_message, *kept]
