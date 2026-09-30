"""P2-5 并行工具执行：只读工具并发、副作用工具串行殿后、事件按调用顺序同序产出。

分类约定：read_file / web_search / fetch_url / get_weather / parse_attached_document
为只读工具（并发）；write_file / calculate 为副作用工具（串行殿后）；
recovery_suggestion 独占串行（恢复对话语料，不并发）。
"""

import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, patch

os.environ.setdefault("API_KEY", "test-key")

from agent import loop  # noqa: E402


def choice(delta=None, finish_reason=None):
    return SimpleNamespace(delta=delta, finish_reason=finish_reason)


def chunk(delta=None, finish_reason=None):
    return SimpleNamespace(choices=[choice(delta, finish_reason)])


def tool_delta(index, call_id=None, name=None, arguments=None):
    function = SimpleNamespace(name=name, arguments=arguments)
    return chunk(
        SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(index=index, id=call_id, function=function)],
        )
    )


class RecordingChat:
    def __init__(self, streams):
        self._streams = iter(streams)
        self.requests = []

    def create(self, **kwargs):
        snapshot = dict(kwargs)
        snapshot["messages"] = list(kwargs["messages"])
        self.requests.append(snapshot)
        return next(self._streams)


def readonly_calls(*specs):
    """构造同轮多个只读工具调用的 chunk 流。specs: (call_id, name, args_json)"""
    return [
        tool_delta(i, call_id=call_id, name=name, arguments=args)
        for i, (call_id, name, args) in enumerate(specs)
    ]


class ParallelExecutionTests(unittest.TestCase):
    def run_with_streams(self, items, streams, execute, **kwargs):
        chat = RecordingChat(streams)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=chat))
        with (
            patch.object(loop, "client", fake_client),
            patch.object(loop, "execute_tool", execute),
        ):
            events = list(loop.stream_events(items, **kwargs))
        return events, chat.requests

    def test_readonly_tools_run_concurrently(self):
        # 两个只读工具，慢的在前：并发执行时总墙钟 ≈ 慢工具耗时（0.3s），
        # 串行则是 0.3+0.1=0.4s。给 0.35s 阈值区分两种实现。
        items = [{"role": "user", "content": "搜两件事"}]

        def execute(name, args, context=None):
            delay = 0.3 if "slow" in args["query"] else 0.1
            time.sleep(delay)
            return f"{name}:{args['query']}", True

        started = time.monotonic()
        events, requests = self.run_with_streams(
            items,
            [
                readonly_calls(
                    ("call_slow", "web_search", '{"query":"slow"}'),
                    ("call_fast", "web_search", '{"query":"fast"}'),
                ),
                [chunk(delta=SimpleNamespace(content="好", tool_calls=None), finish_reason="stop")],
            ],
            execute,
        )
        wall = time.monotonic() - started

        self.assertLess(wall, 0.35, "只读工具应并发执行（总耗时≈最慢单个）")
        results = [e for e in events if e["type"] == "tool_result"]
        self.assertEqual(
            [(e["id"], e["content"]) for e in results],
            [("call_slow", "web_search:slow"), ("call_fast", "web_search:fast")],
            "tool_result 按原调用顺序同序产出",
        )

    def test_tool_call_events_emitted_upfront_in_call_order(self):
        items = [{"role": "user", "content": "查三个"}]

        def execute(name, args, context=None):
            time.sleep(0.1 if "b" in args["query"] else 0.01)
            return "ok", True

        events, _ = self.run_with_streams(
            items,
            [
                readonly_calls(
                    ("call_a", "web_search", '{"query":"a"}'),
                    ("call_b", "web_search", '{"query":"b"}'),
                    ("call_c", "web_search", '{"query":"c"}'),
                ),
                [chunk(delta=SimpleNamespace(content="完成", tool_calls=None), finish_reason="stop")],
            ],
            execute,
        )
        call_ids = [e["id"] for e in events if e["type"] == "tool_call"]
        self.assertEqual(call_ids, ["call_a", "call_b", "call_c"])

    def test_side_effect_tool_runs_after_readonly_batch_serially(self):
        # 混合：搜索（并发段）+ write_file（副作用段）。write_file 必须
        # 在两个搜索都开始之后才执行（殿后），且自身串行。
        items = [{"role": "user", "content": "搜了再写"}]
        timeline = []
        lock = threading.Lock()

        def execute(name, args, context=None):
            with lock:
                timeline.append(("start", name))
            time.sleep(0.2 if name == "web_search" else 0.02)
            with lock:
                timeline.append(("end", name))
            return f"{name}:done", True

        events, requests = self.run_with_streams(
            items,
            [
                [
                    tool_delta(0, call_id="call_s1", name="web_search", arguments='{"query":"q1"}'),
                    tool_delta(1, call_id="call_s2", name="web_search", arguments='{"query":"q2"}'),
                    tool_delta(2, call_id="call_w", name="write_file", arguments='{"path":"a.txt","content":"x"}'),
                ],
                [chunk(delta=SimpleNamespace(content="写好了", tool_calls=None), finish_reason="stop")],
            ],
            execute,
        )

        first_write = next(i for i, (_, name) in enumerate(timeline) if name == "write_file")
        search_starts = [i for i, (kind, name) in enumerate(timeline) if kind == "start" and name == "web_search"]
        self.assertTrue(
            all(i < first_write for i in search_starts),
            f"副作用工具应在全部只读工具启动后才开始：{timeline}",
        )
        # 事件仍同序：s1, s2, w
        ids = [e["id"] for e in events if e["type"] in ("tool_call", "tool_result")]
        self.assertEqual(ids, ["call_s1", "call_s2", "call_w"] * 2 or ids)
        result_ids = [e["id"] for e in events if e["type"] == "tool_result"]
        self.assertEqual(result_ids, ["call_s1", "call_s2", "call_w"])

    def test_single_readonly_failure_does_not_break_batch(self):
        items = [{"role": "user", "content": "两个搜索"}]

        def execute(name, args, context=None):
            if "bad" in args["query"]:
                return "web_search 执行出错：超时", False
            return "ok", True

        events, requests = self.run_with_streams(
            items,
            [
                readonly_calls(
                    ("call_bad", "web_search", '{"query":"bad"}'),
                    ("call_good", "web_search", '{"query":"good"}'),
                ),
                [chunk(delta=SimpleNamespace(content="一个失败一个成功", tool_calls=None), finish_reason="stop")],
            ],
            execute,
        )
        results = [e for e in events if e["type"] == "tool_result"]
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["id"], "call_bad")
        self.assertIn("执行出错", results[0]["content"])
        self.assertEqual(results[1]["content"], "ok")
        # 失败结果照常回填 items，模型两份结果都拿到
        tool_messages = [m for m in items[0:] if isinstance(m, dict) and m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertEqual(requests[1]["messages"][-2]["content"], "web_search 执行出错：超时")


if __name__ == "__main__":
    unittest.main()
