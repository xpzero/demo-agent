"""运行日志：后端关键生命周期的轻量内存环形日志，供前端运行面板消费。

不是替代 logging（排查细节仍看 uvicorn 输出）；这里只记录用户可理解
的运行事实：每次执行的预建、终态、停止、判死、恢复接管等。
进程内环形缓冲（200 条），随进程生灭；GET /api/logs?after=<id> 增量拉取。
"""

import threading
import time

MAX_ENTRIES = 200


class RunLog:
    def __init__(self, capacity: int = MAX_ENTRIES):
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._next_id = 1
        self._capacity = capacity

    def append(self, level: str, source: str, message: str, **fields) -> int:
        entry = {
            "id": 0,
            "ts": time.time(),
            "level": level,
            "source": source,
            "message": message,
        }
        if fields:
            entry.update(fields)
        with self._lock:
            entry["id"] = self._next_id
            self._next_id += 1
            self._entries.append(entry)
            if len(self._entries) > self._capacity:
                self._entries = self._entries[-self._capacity:]
        return entry["id"]

    def info(self, source: str, message: str, **fields) -> int:
        return self.append("info", source, message, **fields)

    def warn(self, source: str, message: str, **fields) -> int:
        return self.append("warn", source, message, **fields)

    def error(self, source: str, message: str, **fields) -> int:
        return self.append("error", source, message, **fields)

    def scan(self, after: int = 0) -> list[dict]:
        """返回 id > after 的条目（增量拉取）。"""
        with self._lock:
            return [dict(entry) for entry in self._entries if entry["id"] > after]


run_log = RunLog()
