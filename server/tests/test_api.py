import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("API_KEY", "test-key")

import api  # noqa: E402
from database import Database  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class ChatApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "test.sqlite3"
        self.root = Path(self.directory.name) / "files"
        self.patches = [
            patch.object(api, "DATABASE_PATH", self.database_path),
            patch.object(api, "FILE_ROOT", self.root),
        ]
        for item in self.patches:
            item.start()
        api._running_sessions.clear()
        self.database = Database(self.database_path)
        self.database.initialize()
        self.client = TestClient(api.app)
        self.session_id = str(uuid4())

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def payload(self, **overrides):
        return {
            "session_id": self.session_id,
            "parent_message_id": None,
            "message": "hi",
            "ref_file_ids": [],
            **overrides,
        }

    def events(self, response):
        return [json.loads(frame.removeprefix("data: ")) for frame in response.text.strip().split("\n\n")]

    def test_first_chat_creates_session_and_persists_assistant(self):
        with patch.object(api, "stream_events", return_value=iter([
            {"type": "text_delta", "text": "你好"},
            {"type": "done", "content": "你好"},
        ])):
            response = self.client.post("/api/chat", json=self.payload())
        self.assertEqual(response.status_code, 200)
        events = self.events(response)
        self.assertEqual(events[0]["type"], "user_message")
        self.assertEqual(events[1], {"type": "text_delta", "text": "你好"})
        self.assertEqual(events[-1]["type"], "done")
        history = self.client.get(f"/api/sessions/{self.session_id}").json()
        messages = history["messages"]
        self.assertEqual([entry["role"] for entry in messages], ["user", "assistant"])
        self.assertEqual(messages[1]["parent_id"], messages[0]["id"])
        self.assertEqual(events[-1]["message_id"], messages[1]["id"])
        self.assertEqual(history["session"]["current_message_id"], messages[1]["id"])
        self.assertEqual(self.client.get("/api/sessions").json()["sessions"][0]["id"], self.session_id)

    def test_stale_parent_is_rejected(self):
        first = self.database.create_user_turn(
            session_id=self.session_id,
            parent_message_id=None,
            content="first",
            file_ids=[],
        )
        assistant = self.database.add_assistant_message(
            session_id=self.session_id,
            parent_message_id=first,
            content="answer",
        )
        response = self.client.post(
            "/api/chat",
            json=self.payload(parent_message_id=first, message="stale"),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "stale_parent_message")
        history = self.client.get(f"/api/sessions/{self.session_id}").json()
        self.assertEqual(history["session"]["current_message_id"], assistant)
        self.assertEqual(len(history["messages"]), 2)

    def test_followup_uses_database_chain_and_parent(self):
        first = self.database.create_user_turn(
            session_id=self.session_id, parent_message_id=None, content="first", file_ids=[]
        )
        assistant = self.database.add_assistant_message(
            session_id=self.session_id, parent_message_id=first, content="answer"
        )
        seen = []

        def stream(items):
            seen.extend(items)
            yield {"type": "done", "content": "second answer"}

        with patch.object(api, "stream_events", side_effect=stream):
            response = self.client.post("/api/chat", json=self.payload(parent_message_id=assistant, message="second"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["role"] for item in seen], ["system", "user", "assistant", "user"])
        self.assertEqual(seen[-1]["content"], "second")

    def test_concurrent_and_validation_failures_do_not_write(self):
        api._running_sessions.add(self.session_id)
        self.assertEqual(self.client.post("/api/chat", json=self.payload()).status_code, 409)
        api._running_sessions.clear()
        response = self.client.post("/api/chat", json=self.payload(parent_message_id=123))
        self.assertEqual(response.status_code, 404)
        response = self.client.post("/api/chat", json=self.payload(ref_file_ids=["file_missing"]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get("/api/sessions").json()["sessions"], [])
        self.assertEqual(api._running_sessions, set())

    def test_failed_stream_keeps_user_message_as_parent(self):
        with patch.object(api, "stream_events", return_value=iter([
            {"type": "error", "message": "gateway failed"},
        ])):
            response = self.client.post("/api/chat", json=self.payload())
        self.assertEqual(response.status_code, 200)
        events = self.events(response)
        self.assertEqual(events[0]["type"], "user_message")
        self.assertEqual(events[1]["type"], "error")
        history = self.client.get(f"/api/sessions/{self.session_id}").json()
        self.assertEqual(len(history["messages"]), 1)
        self.assertEqual(history["session"]["current_message_id"], history["messages"][0]["id"])
        self.assertEqual(api._running_sessions, set())

    def test_assistant_persistence_failure_emits_error_not_done(self):
        with patch.object(Database, "add_assistant_message", side_effect=RuntimeError("write failed")):
            with patch.object(api, "stream_events", return_value=iter([
                {"type": "done", "content": "answer"},
            ])):
                response = self.client.post("/api/chat", json=self.payload())
        events = self.events(response)
        self.assertEqual([event["type"] for event in events], ["user_message", "error"])
        self.assertEqual(len(self.client.get(f"/api/sessions/{self.session_id}").json()["messages"]), 1)
        self.assertEqual(api._running_sessions, set())

    def test_history_missing_and_invalid_uuid(self):
        self.assertEqual(self.client.get(f"/api/sessions/{self.session_id}").status_code, 404)
        self.assertEqual(self.client.get("/api/sessions/not-a-uuid").status_code, 400)
        self.assertEqual(self.client.post("/api/chat", json=self.payload(
            session_id="not-a-uuid"
        )).status_code, 400)

    def test_file_binding_and_history(self):
        uploaded = self.client.post(
            "/api/documents", files={"file": ("report.pdf", b"%PDF-content", "application/pdf")}
        ).json()
        with patch.object(api, "stream_events", return_value=iter([{"type": "done", "content": "ok"}])):
            response = self.client.post("/api/chat", json=self.payload(ref_file_ids=[uploaded["file_id"]]))
        self.assertEqual(response.status_code, 200)
        messages = self.client.get(f"/api/sessions/{self.session_id}").json()["messages"]
        self.assertEqual(messages[0]["files"][0]["id"], uploaded["file_id"])
        other_session = str(uuid4())
        response = self.client.post("/api/chat", json=self.payload(
            session_id=other_session, ref_file_ids=[uploaded["file_id"]]
        ))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.get(f"/api/sessions/{other_session}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
