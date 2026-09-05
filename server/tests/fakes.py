from copy import deepcopy
from threading import RLock

from sessions import Session, SessionNotFound, SessionRevisionConflict, SessionSummary


def clone_session(session: Session) -> Session:
    return Session.from_dict(deepcopy(session.to_dict()))


class FakeSessionService:
    def __init__(self):
        self._lock = RLock()
        self._sessions: dict[int, Session] = {}
        self._next_id = 1
        self.save_calls: list[Session] = []
        self.next_save_error: Exception | None = None
        self.closed = False

    def create(self, system_prompt: str) -> Session:
        with self._lock:
            session = Session(
                id=self._next_id,
                items=[{"role": "system", "content": system_prompt}],
            )
            self._sessions[session.id] = clone_session(session)
            self._next_id += 1
            return clone_session(session)

    def seed(self, session: Session) -> None:
        with self._lock:
            self._sessions[session.id] = clone_session(session)
            self._next_id = max(self._next_id, session.id + 1)

    def get(self, session_id: int) -> Session | None:
        with self._lock:
            session = self._sessions.get(session_id)
            return None if session is None else clone_session(session)

    def list_sessions(self) -> list[SessionSummary]:
        with self._lock:
            return [
                SessionSummary(
                    id=session.id,
                    summary=session.summary,
                    message_count=len(session.items),
                    revision=session.revision,
                )
                for session in sorted(self._sessions.values(), key=lambda value: value.id)
            ]

    def save(self, session: Session) -> None:
        with self._lock:
            if self.next_save_error is not None:
                error = self.next_save_error
                self.next_save_error = None
                raise error

            stored = self._sessions.get(session.id)
            if stored is None:
                raise SessionNotFound(session.id)
            if stored.revision != session.revision:
                raise SessionRevisionConflict(session.id)

            saved = clone_session(session)
            saved.revision += 1
            self._sessions[session.id] = saved
            session.revision = saved.revision
            self.save_calls.append(clone_session(saved))

    def delete(self, session_id: int, expected_revision: int) -> bool:
        with self._lock:
            stored = self._sessions.get(session_id)
            if stored is None:
                return False
            if stored.revision != expected_revision:
                raise SessionRevisionConflict(session_id)
            del self._sessions[session_id]
            return True

    def close(self) -> None:
        self.closed = True
