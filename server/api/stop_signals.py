"""进程内停止信号（喊话层）：stop 接口置起，执行线程在边界检查点消费。

与 chat_messages.stop_requested（字据层）分工：
内存管活线程的响应速度，库里管崩溃后的语义正确（判死者读字据）。
"""

from threading import Condition, Event


class StopSignals:
    def __init__(self):
        self.condition = Condition()
        self.signals: dict[str, Event] = {}

    def create(self, session_id: str) -> Event:
        """chat 请求进入时创建（或复用）本会话的停止信号。"""
        with self.condition:
            signal = self.signals.get(session_id)
            if signal is None:
                signal = Event()
                self.signals[session_id] = signal
            return signal

    def set(self, session_id: str) -> None:
        with self.condition:
            signal = self.signals.get(session_id)
            if signal is not None:
                signal.set()

    def discard(self, session_id: str) -> None:
        """chat 流结束后清理（BackgroundTask）。"""
        with self.condition:
            self.signals.pop(session_id, None)


stop_signals = StopSignals()
