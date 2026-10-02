"""判死者：崩溃/超期残留消息的收口（resume 与 send 双入口复用）。

依据设计文档缺口 1/4：回填 content（text fragments 按序拼接）、
读 stop_requested 定终态语义（stopped / interrupted）、terminal 幂等。
"""

import time

from database import Database

from .active_runs import active_runs
from .run_log import run_log


def finalize_dead(database: Database, message: dict) -> str:
    """判死者收口：读字据定终态语义，回填 content，幂等 terminal。"""
    kind = "stopped" if message["stop_requested"] else "interrupted"
    text = database.assemble_text(message["id"])
    run_log.warn("reaper", f"判死收口 → {kind}", message_id=message["id"], chars=len(text))
    finalized = database.finalize_assistant(message["id"], kind, text)
    if finalized and not database.has_terminal_fragment(message["id"]):
        # 幂等防御（漏洞 ③）：崩溃可能停在「terminal 已落库、行未 finalize」之间
        database.append_fragment(message["id"], "terminal", {"type": kind})
    active_runs.close(message["id"])
    return kind


def is_dead_residual(message: dict) -> bool:
    """判死判定的唯一实现：超期，或 Run 不在（进程重启残留）。

    resume 与 send 双入口共用——两处判定必须一致，禁止各自内联复制。
    """
    expired = (
        message["deadline_at"] is not None
        and time.time() > float(message["deadline_at"])
    )
    return expired or not active_runs.has(message["id"])


def reap_expired(database: Database, session_id: str) -> dict | None:
    """send/resume 入口的判死：残留 → 收口；活消息原样返回。"""
    message = database.get_unfinished_message(session_id)
    if message is None:
        return None
    if is_dead_residual(message):
        kind = finalize_dead(database, message)
        return {"message_id": message["id"], "finish_kind": kind}
    return message
