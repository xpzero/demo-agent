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
        self.assertEqual(set(plain), {"id", "items", "revision"})
        self.assertEqual(plain["revision"], 0)
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
            "id": 9,
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

    def test_from_dict_rejects_invalid_top_level_fields(self):
        invalid_payloads = [
            {"id": True, "items": []},
            {"id": 0, "items": []},
            {"id": "9", "items": []},
            {"id": 9, "items": "not-a-list"},
            {"id": 9, "items": [], "revision": -1},
            {"id": 9, "items": [], "revision": True},
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                Session.from_dict(payload)

    def test_pending_approval_survives_json_round_trip(self):
        pending = {
            "schema_version": 2,
            "remaining_turns": 9,
            "outputs_committed": False,
            "calls": [
                {
                    "id": "call_write",
                    "name": "write_file",
                    "args": {"path": "notes/demo.txt", "content": "new"},
                    "permission": {
                        "action": "ask",
                        "requests": [
                            {"permission": "write", "target": "notes/demo.txt"}
                        ],
                        "reason": "测试规则",
                    },
                    "decision": None,
                    "outcome": None,
                    "output": None,
                    "preview": {
                        "type": "code_diff",
                        "path": "notes/demo.txt",
                        "additions": 1,
                        "deletions": 0,
                        "lines": [{"kind": "added", "text": "new"}],
                    },
                }
            ],
        }
        session = Session(
            id=11,
            items=[
                {"role": "user", "content": "write"},
                {
                    "type": "function_call",
                    "id": "fc_write",
                    "call_id": "call_write",
                    "name": "write_file",
                    "arguments": '{"path":"notes/demo.txt","content":"new"}',
                },
            ],
            pending_approval=pending,
        )

        restored = Session.from_dict(json.loads(json.dumps(session.to_dict())))

        self.assertEqual(restored.pending_approval, pending)
        self.assertEqual(restored.to_dict(), session.to_dict())

    def test_unsupported_pending_approval_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不支持的待审批状态版本"):
            Session.from_dict(
                {
                    "id": 12,
                    "items": [],
                    "pending_approval": {
                        "schema_version": 1,
                        "remaining_turns": 9,
                        "outputs_committed": False,
                        "calls": [],
                    },
                }
            )

    def test_committed_pending_requires_matching_output_item(self):
        with self.assertRaisesRegex(ValueError, "Items 缺少.*工具结果"):
            Session.from_dict(
                {
                    "id": 13,
                    "items": [
                        {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "calculate",
                            "arguments": '{"expression":"1+1"}',
                        }
                    ],
                    "pending_approval": {
                        "schema_version": 2,
                        "remaining_turns": 9,
                        "outputs_committed": True,
                        "calls": [
                            {
                                "id": "call_1",
                                "name": "calculate",
                                "args": {"expression": "1+1"},
                                "permission": {
                                    "action": "ask",
                                    "requests": [
                                        {
                                            "permission": "calculate",
                                            "target": "1+1",
                                        }
                                    ],
                                    "reason": "test",
                                },
                                "decision": "approved",
                                "outcome": "completed",
                                "output": "2",
                            }
                        ],
                    },
                }
            )

    def test_persisted_approval_requires_an_execution_result(self):
        with self.assertRaisesRegex(ValueError, "决定与执行结果不一致"):
            Session.from_dict(
                {
                    "id": 14,
                    "items": [],
                    "pending_approval": {
                        "schema_version": 2,
                        "remaining_turns": 9,
                        "outputs_committed": False,
                        "calls": [
                            {
                                "id": "call_1",
                                "name": "calculate",
                                "args": {"expression": "1+1"},
                                "permission": {
                                    "action": "ask",
                                    "requests": [
                                        {
                                            "permission": "calculate",
                                            "target": "1+1",
                                        }
                                    ],
                                    "reason": "test",
                                },
                                "decision": "approved",
                                "outcome": None,
                                "output": None,
                            }
                        ],
                    },
                }
            )

    def test_pending_approval_requires_matching_function_call(self):
        with self.assertRaisesRegex(ValueError, "function_call 不匹配"):
            Session.from_dict(
                {
                    "id": 15,
                    "items": [{"role": "user", "content": "write"}],
                    "pending_approval": {
                        "schema_version": 2,
                        "remaining_turns": 9,
                        "outputs_committed": False,
                        "calls": [
                            {
                                "id": "call_1",
                                "name": "write_file",
                                "args": {"path": "notes/demo.txt", "content": "new"},
                                "permission": {
                                    "action": "ask",
                                    "requests": [
                                        {
                                            "permission": "write",
                                            "target": "notes/demo.txt",
                                        }
                                    ],
                                    "reason": "test",
                                },
                                "decision": None,
                                "outcome": None,
                                "output": None,
                            }
                        ],
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
