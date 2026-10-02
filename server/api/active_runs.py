"""进程内运行态 Run：每个消息一份事件缓冲 + 订阅者队列，终态即弃。

身份 = unfinished 消息行（库）；历史本体 = fragments 表（库）；
本模块是运行态——丢了不丢数据，判死/崩溃/排查一律走库。
"""

from queue import Queue
from threading import Condition


class ActiveRuns:
    """message_id → Run；publish/attach/close 共用一把 Condition 锁。

    先建 Run、再预建 DB 行（缺口 D 顺序）——保证
    「DB 有 unfinished 行 ⇒ Run 必在」；resume 见「行在、Run 不在」
    即进程重启残留，直接判死收口。
    """

    def __init__(self):
        self.condition = Condition()
        self.events: dict[int, list[tuple[int, dict]]] = {}
        self.subscribers: dict[int, list[Queue]] = {}
        self._next_placeholder = -1

    def reserve(self) -> int:
        """占位键：DB 行 ID 尚未产生时先建 Run（缺口 D 顺序要求先 Run 后 DB 行）。"""
        with self.condition:
            key = self._next_placeholder
            self._next_placeholder -= 1
            self.events[key] = []
            return key

    def bind(self, placeholder: int, message_id: int) -> None:
        """DB 行落库后，把占位 Run 迁移到真实 message_id（原子）。"""
        with self.condition:
            if placeholder in self.events:
                self.events[message_id] = self.events.pop(placeholder)

    def create(self, message_id: int) -> None:
        """发送即建：预建 DB 行之前调用。幂等。"""
        with self.condition:
            self.events.setdefault(message_id, [])

    def has(self, message_id: int) -> bool:
        with self.condition:
            return message_id in self.events

    def publish(self, message_id: int, event: dict, seq: int) -> None:
        """执行线程每事件调用：先落库（调用方保证），后进 Run + 广播。"""
        with self.condition:
            bucket = self.events.setdefault(message_id, [])
            bucket.append((seq, event))
            for queue in self.subscribers.get(message_id, []):
                queue.put((event, seq))

    def attach(self, message_id: int) -> tuple[list[tuple[int, dict]], Queue] | None:
        """resume 用：一把锁内拿完整快照 + 挂上实时队列（原子交接）。

        只读不建：Run 不存在返回 None，由调用方走判死/快照路径；
        绝不隐式创建空 Run（否则会在没有生产者的 Run 上死等）。
        """
        with self.condition:
            if message_id not in self.events:
                return None
            snapshot = list(self.events[message_id])
            queue: Queue = Queue()
            self.subscribers.setdefault(message_id, []).append(queue)
            return snapshot, queue

    def detach(self, message_id: int, queue: Queue) -> None:
        """连接断开时移除自己的队列。"""
        with self.condition:
            queues = self.subscribers.get(message_id)
            if queues and queue in queues:
                queues.remove(queue)

    def close(self, message_id: int) -> None:
        """终态落定后调用：Run 与订阅者就地消失（终态即弃）。幂等。"""
        with self.condition:
            self.events.pop(message_id, None)
            self.subscribers.pop(message_id, None)


active_runs = ActiveRuns()
