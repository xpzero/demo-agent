import os
import unittest
from unittest.mock import patch


os.environ.setdefault("API_KEY", "test-key")

import api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class ChatApiTests(unittest.TestCase):
    def setUp(self):
        api.items.clear()
        api.items.append({"role": "system", "content": "system"})
        api._running = False
        self.client = TestClient(api.app)

    def test_chat_rejects_concurrent_run(self):
        api._running = True

        response = self.client.post("/api/chat", json={"message": "hello"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(api.items[-1]["role"], "system")

    def test_chat_streams_events_and_releases_running_flag(self):
        events = iter(
            [
                {"type": "text_delta", "text": "你好"},
                {"type": "done", "content": "你好"},
            ]
        )

        with patch.object(api, "stream_events", return_value=events):
            with self.client.stream(
                "POST", "/api/chat", json={"message": "hi"}
            ) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
                body = "".join(response.iter_text())

        self.assertIn('"text_delta"', body)
        self.assertIn('"done"', body)
        self.assertFalse(api._running)
        self.assertEqual(api.items[-1], {"role": "user", "content": "hi"})


if __name__ == "__main__":
    unittest.main()
