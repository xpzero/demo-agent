"""日志开关：按 LOG_LEVEL 环境变量决定是否输出 agent 内核日志。

属于入口层职责，CLI 与 HTTP 两个入口共用，agent 包本身不做任何配置。
"""

import logging
import os


def setup_logging() -> None:
    """未设置 LOG_LEVEL 时保持静默。

    只调 agent 这一个 logger：root 留在 WARNING，
    否则 httpx / openai 的 DEBUG 会把工具调用日志淹没。
    """
    level = os.getenv("LOG_LEVEL", "").upper()
    if not level:
        return

    # 必须建 root handler：logging 兜底的 lastResort 只放行 WARNING 以上，DEBUG 会被丢掉
    logging.basicConfig(format="%(levelname)s %(name)s | %(message)s")
    logging.getLogger("agent").setLevel(level)
