"""单进程执行事件缓存；订阅独立游标，终态自动到期回收。"""

import json
import time
from threading import Condition, Thread


class RunEventBuffer:
    def __init__(self, user_message_id: int, *, max_events=4096, max_bytes=8 * 1024 * 1024):
        self.user_message_id = user_message_id
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.condition = Condition()
        self.events: list[tuple[int, str]] = []
        self.sequence = 0
        self.size = 0
        self.replayable = True
        self.producer_done = False
        self.subscribers = 0

    def append(self, frame: str) -> str:
        with self.condition:
            self.sequence += 1
            frame = f"id: {self.user_message_id}:{self.sequence}\n{frame}"
            size = len(frame.encode("utf-8"))
            if self.replayable:
                if len(self.events) >= self.max_events or self.size + size > self.max_bytes:
                    self.replayable = False
                    self.events.clear()
                    self.size = 0
                else:
                    self.events.append((self.sequence, frame))
                    self.size += size
            self.condition.notify_all()
            return frame

    def subscribe(self, cursor: tuple[int, int] | None) -> tuple[bool, int]:
        with self.condition:
            if self.subscribers >= 8:
                raise OverflowError("恢复连接过多，请稍后重试")
            self.subscribers += 1
            valid = (cursor is not None and cursor[0] == self.user_message_id
                     and cursor[1] <= self.sequence and self.replayable)
            return valid, cursor[1] if valid and cursor is not None else 0

    def unsubscribe(self):
        with self.condition:
            self.subscribers -= 1

    def read(self, after: int) -> tuple[bool, list[tuple[int, str]], bool]:
        with self.condition:
            return self.replayable, [(seq, frame) for seq, frame in self.events if seq > after], self.producer_done

    def finish_producing(self):
        with self.condition:
            self.producer_done = True
            self.condition.notify_all()


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class EventBufferRegistry:
    """一个定时清理线程；移除注册不影响已持有 buffer 的连接。"""
    def __init__(self, *, retention_seconds=60):
        self.retention_seconds = min(retention_seconds, 60)
        self.condition = Condition()
        self.buffers: dict[tuple[str, int], RunEventBuffer] = {}
        self.expiry: dict[tuple[str, int], float] = {}
        self.worker: Thread | None = None

    @staticmethod
    def key(database, run_id):
        return str(database.path.resolve()), run_id

    def register(self, database, run_id, user_message_id):
        key = self.key(database, run_id)
        with self.condition:
            buffer = self.buffers.get(key)
            if buffer is None:
                buffer = RunEventBuffer(user_message_id)
                self.buffers[key] = buffer
            return buffer

    def get_or_register_observer(self, database, run_id, user_message_id):
        with self.condition:
            self._cleanup()
            key = self.key(database, run_id)
            if key in self.buffers:
                return self.buffers[key], False
            buffer = self.register(database, run_id, user_message_id)
            buffer.replayable = False
            buffer.finish_producing()
            return buffer, True

    def get(self, database, run_id):
        with self.condition:
            self._cleanup()
            return self.buffers.get(self.key(database, run_id))

    def terminal(self, database, run_id, finished_at=None):
        with self.condition:
            key = self.key(database, run_id)
            if key in self.buffers and key not in self.expiry:
                age = max(0, time.time() - finished_at) if finished_at is not None else 0
                self.expiry[key] = time.monotonic() + max(0, self.retention_seconds - age)
            self._cleanup()
            if self.expiry and (self.worker is None or not self.worker.is_alive()):
                self.worker = Thread(target=self._reap, name="resume-cache-expiry", daemon=True)
                self.worker.start()
            self.condition.notify_all()

    def discard(self, database, run_id):
        with self.condition:
            key = self.key(database, run_id)
            self.buffers.pop(key, None)
            self.expiry.pop(key, None)
            self.condition.notify_all()

    def _cleanup(self):
        now = time.monotonic()
        for key, deadline in list(self.expiry.items()):
            if deadline <= now:
                self.expiry.pop(key)
                self.buffers.pop(key, None)

    def _reap(self):
        with self.condition:
            while self.expiry:
                self._cleanup()
                if self.expiry:
                    self.condition.wait(max(0, min(self.expiry.values()) - time.monotonic()))
            # Clear while holding the same lock used by terminal(), so a new
            # expiration cannot mistake this exiting worker for a live reaper.
            self.worker = None


buffers = EventBufferRegistry()
