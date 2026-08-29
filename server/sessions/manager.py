import json
from pathlib import Path

from .session import Session

# 运行时数据目录，与代码包 sessions/ 分开，避免 .py 与 .json 混在一处
DATA_DIR = Path(__file__).resolve().parents[1] / ".sessions"


class SessionManager:
    """多会话容器：负责隔离各会话的 Items，并把它们持久化到磁盘。"""

    def __init__(self, system_prompt: str):
        self._system_prompt = system_prompt
        self._sessions: dict[int, Session] = {}
        self._current_id = 0

        self._load_all()
        # 启动时总是新建一条空会话进入，历史通过 /list 与 /switch 访问
        self._next_id = max(self._sessions, default=0) + 1
        self.new()

    def _load_all(self) -> None:
        if not DATA_DIR.exists():
            return

        for path in sorted(DATA_DIR.glob("*.json")):
            try:
                session = Session.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception as e:
                # 单个文件不受支持或无法解析时，不应拖垮启动。
                print(f"[跳过无法加载的会话文件 {path.name}：{e}]")
                continue
            self._sessions[session.id] = session

    @property
    def current(self) -> Session:
        return self._sessions[self._current_id]

    def listing(self) -> list[tuple[bool, Session]]:
        """返回 (是否为当前会话, 会话) 列表，按 id 升序"""
        return [(sid == self._current_id, s) for sid, s in sorted(self._sessions.items())]

    def new(self) -> Session:
        """新建会话并立即切换过去。id 只增不复用，避免指代混乱"""
        session = Session(
            id=self._next_id,
            items=[{"role": "system", "content": self._system_prompt}],
        )
        self._sessions[session.id] = session
        self._current_id = session.id
        self._next_id += 1
        return session

    def switch(self, session_id: int) -> bool:
        if session_id not in self._sessions:
            return False
        self._current_id = session_id
        return True

    def delete(self, session_id: int) -> str | None:
        """删除会话；成功返回 None，失败返回原因。

        当前会话不允许删除——这条限制同时保证了至少有一条会话存在。
        """
        if session_id == self._current_id:
            return "不能删除当前会话，请先 /switch 到别处"
        if session_id not in self._sessions:
            return f"没有 {session_id} 号会话"

        del self._sessions[session_id]
        (DATA_DIR / f"{session_id}.json").unlink(missing_ok=True)
        return None

    def save_current(self) -> None:
        """在一轮对话结束后调用。

        此刻 Items 里的 function_call 与 function_call_output 是配对完整的，
        落盘的文件因此永远合法；空会话不写，避免产生无内容的文件。
        """
        session = self.current
        if session.is_empty:
            return

        DATA_DIR.mkdir(exist_ok=True)
        content = json.dumps(session.to_dict(), ensure_ascii=False, indent=2)
        (DATA_DIR / f"{session.id}.json").write_text(content, encoding="utf-8")
