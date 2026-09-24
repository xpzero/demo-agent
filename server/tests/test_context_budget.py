import os
import unittest
from unittest.mock import patch

from agent.context_budget import (
    build_context,
    estimate_tokens,
    pick_recent_within,
    to_chat_messages,
)


def row(message_id, role, content, parent_id=None):
    """构造 chat_messages 链行的最小形状（get_message_chain 的返回元素）。"""
    return {"id": message_id, "parent_id": parent_id, "role": role, "content": content}


class EstimateTokensTests(unittest.TestCase):
    def test_counts_characters_as_tokens(self):
        # 一字一 token 的保守估算：中文场景偏大，方向安全
        self.assertEqual(estimate_tokens("你好"), 2)
        self.assertEqual(estimate_tokens("hello"), 5)
        self.assertEqual(estimate_tokens(""), 0)


class ToChatMessagesTests(unittest.TestCase):
    def test_maps_rows_to_api_messages(self):
        rows = [
            row(1, "user", "第一问", parent_id=None),
            row(2, "assistant", "第一答", parent_id=1),
            row(3, "user", "第二问", parent_id=2),
        ]
        self.assertEqual(
            to_chat_messages(rows),
            [
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
                {"role": "user", "content": "第二问"},
            ],
        )


class PickRecentWithinTests(unittest.TestCase):
    def test_keeps_everything_within_budget(self):
        messages = [
            {"role": "user", "content": "a" * 100},
            {"role": "assistant", "content": "b" * 100},
        ]
        self.assertEqual(pick_recent_within(messages, 1000), messages)

    def test_drops_oldest_until_within_budget(self):
        # 预算 250：总长 100+100+100=300 超了，丢最旧一条后 200 在预算内
        messages = [
            {"role": "user", "content": "a" * 100},
            {"role": "assistant", "content": "b" * 100},
            {"role": "user", "content": "c" * 100},
        ]
        self.assertEqual(pick_recent_within(messages, 250), messages[1:])

    def test_always_keeps_at_least_the_latest_message(self):
        # 单条消息本身超预算也必须保留，否则模型收不到本轮问题
        messages = [{"role": "user", "content": "a" * 500}]
        self.assertEqual(pick_recent_within(messages, 100), messages)

    def test_empty_chain_kept_empty(self):
        self.assertEqual(pick_recent_within([], 1000), [])


class BuildContextTests(unittest.TestCase):
    def test_no_truncation_when_within_budget(self):
        chain = [row(1, "user", "第一问"), row(2, "assistant", "第一答")]
        items = build_context("SYSTEM", chain, budget=1000)
        self.assertEqual(
            items,
            [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
            ],
        )

    def test_truncates_oldest_when_over_budget(self):
        # 每条 100 字，预算 350 覆盖 system(6)+最近三条(300)，
        # 最旧的 user 消息被丢弃
        chain = [
            row(1, "user", "a" * 100),
            row(2, "assistant", "b" * 100),
            row(3, "user", "c" * 100),
            row(4, "assistant", "d" * 100),
        ]
        items = build_context("SYSTEM", chain, budget=350)
        self.assertEqual(
            [message["content"] for message in items],
            ["SYSTEM", "b" * 100, "c" * 100, "d" * 100],
        )

    def test_system_message_always_kept(self):
        # 极端情况：历史全部超预算，system 仍然保留，且最近一条不丢
        chain = [
            row(1, "user", "a" * 100),
            row(2, "assistant", "b" * 100),
        ]
        items = build_context("SYSTEM", chain, budget=50)
        self.assertEqual(
            [message["role"] for message in items],
            ["system", "assistant"],
        )

    def test_configured_context_budget(self):
        chain = [row(1, "user", "a" * 100), row(2, "assistant", "b" * 100)]
        with patch.dict(os.environ, {"CONTEXT_BUDGET": "150"}):
            self.assertEqual([item["content"] for item in build_context("SYSTEM", chain)],
                             ["SYSTEM", "b" * 100])
        with patch.dict(os.environ, {"CONTEXT_BUDGET": "500"}):
            self.assertEqual(len(build_context("SYSTEM", chain)), 3)


if __name__ == "__main__":
    unittest.main()
