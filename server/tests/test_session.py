import json
import unittest
from types import SimpleNamespace

from sessions.session import Session


class FakeSdkMessage:
    """Response output message with the serialization surface used by Session."""

    type = "message"

    def __init__(self, *, message_id, role, content):
        self.id = message_id
        self.role = role
        self.content = content

    def model_dump(self, *, exclude_none):
        self.last_exclude_none = exclude_none
        return {
            "type": self.type,
            "id": self.id,
            "role": self.role,
            "content": [
                dict(block) if isinstance(block, dict) else vars(block)
                for block in self.content
            ],
        }


class SessionResponseItemsTests(unittest.TestCase):
    def test_content_block_lists_drive_summary_and_last_exchange_after_json_round_trip(self):
        user_message = {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                {"type": "input_text", "text": "列表形式的用户问题"},
            ],
        }
        assistant_message = FakeSdkMessage(
            message_id="msg_1",
            role="assistant",
            content=[
                SimpleNamespace(type="output_text", text="列表形式的模型回答"),
            ],
        )
        session = Session(
            id=7,
            items=[
                {"role": "system", "content": "system"},
                user_message,
                {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
                {"type": "function_call_output", "call_id": "call_1", "output": "ignored"},
                assistant_message,
            ],
        )

        self.assertEqual(session.summary, "列表形式的用户问题")
        self.assertEqual(session.last_exchange, ("列表形式的用户问题", "列表形式的模型回答"))
        self.assertFalse(session.is_empty)

        plain = session.to_dict()
        self.assertEqual(set(plain), {"id", "items"})
        self.assertIs(plain["items"][0], session.items[0])
        self.assertEqual(plain["items"][-1]["content"][0]["text"], "列表形式的模型回答")
        self.assertIs(assistant_message.last_exclude_none, True)

        restored = Session.from_dict(json.loads(json.dumps(plain, ensure_ascii=False)))
        self.assertEqual(restored.to_dict(), plain)
        self.assertEqual(restored.summary, "列表形式的用户问题")
        self.assertEqual(restored.last_exchange, ("列表形式的用户问题", "列表形式的模型回答"))
        self.assertFalse(restored.is_empty)

    def test_empty_session_with_only_non_user_items_is_empty(self):
        session = Session(
            id=1,
            items=[
                {"role": "system", "content": [{"type": "input_text", "text": "system"}]},
                {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
            ],
        )

        self.assertTrue(session.is_empty)
        self.assertEqual(session.summary, "(空会话)")
        self.assertIsNone(session.last_exchange)

    def test_from_dict_accepts_items_payload(self):
        payload = {
            "id": "9",
            "items": [
                {"role": "user", "content": "question"},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "answer"}],
                },
            ],
        }

        session = Session.from_dict(payload)

        self.assertEqual(session.id, 9)
        self.assertEqual(session.items, payload["items"])
        self.assertEqual(session.summary, "question")
        self.assertEqual(session.last_exchange, ("question", "answer"))


class LegacyChatSessionMigrationTests(unittest.TestCase):
    def test_legacy_messages_convert_tool_calls_and_results_to_response_items(self):
        legacy = {
            "id": 12,
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "并行查询"},
                {
                    "role": "assistant",
                    "content": "我来查询",
                    "tool_calls": [
                        {
                            "id": "call_weather",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city":"上海"}',
                            },
                        },
                        {
                            "id": "call_calculate",
                            "type": "function",
                            "function": {
                                "name": "calculate",
                                "arguments": '{"expression":"6*7"}',
                            },
                        },
                    ],
                },
                {"role": "tool", "tool_call_id": "call_weather", "content": "晴"},
                {"role": "tool", "tool_call_id": "call_calculate", "content": "42"},
                {"role": "assistant", "content": "上海晴，答案是 42"},
            ],
        }

        session = Session.from_dict(legacy)

        self.assertEqual(session.id, 12)
        self.assertFalse(any(item.get("role") == "tool" for item in session.items))
        function_calls = [
            item for item in session.items if item.get("type") == "function_call"
        ]
        self.assertEqual(
            function_calls,
            [
                {
                    "type": "function_call",
                    "call_id": "call_weather",
                    "name": "get_weather",
                    "arguments": '{"city":"上海"}',
                },
                {
                    "type": "function_call",
                    "call_id": "call_calculate",
                    "name": "calculate",
                    "arguments": '{"expression":"6*7"}',
                },
            ],
        )
        function_outputs = [
            item
            for item in session.items
            if item.get("type") == "function_call_output"
        ]
        self.assertEqual(
            function_outputs,
            [
                {
                    "type": "function_call_output",
                    "call_id": "call_weather",
                    "output": "晴",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_calculate",
                    "output": "42",
                },
            ],
        )
        self.assertIn(
            {"role": "assistant", "content": "我来查询"},
            session.items,
        )
        self.assertEqual(session.summary, "并行查询")
        self.assertEqual(session.last_exchange, ("并行查询", "上海晴，答案是 42"))

        plain = session.to_dict()
        self.assertIn("items", plain)
        self.assertNotIn("messages", plain)
        restored = Session.from_dict(json.loads(json.dumps(plain, ensure_ascii=False)))
        self.assertEqual(restored.items, session.items)

    def test_legacy_assistant_without_text_does_not_create_empty_message(self):
        legacy = {
            "id": 13,
            "messages": [
                {"role": "user", "content": "calculate"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "calculate",
                                "arguments": '{"expression":"1+1"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "2"},
            ],
        }

        session = Session.from_dict(legacy)

        assistant_messages = [
            item
            for item in session.items
            if item.get("role") == "assistant" and item.get("type") != "function_call"
        ]
        self.assertEqual(assistant_messages, [])


if __name__ == "__main__":
    unittest.main()
