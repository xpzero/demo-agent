import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch


# agent.client constructs the SDK client at import time. Unit tests never make a
# network request, but the SDK still requires a syntactically present key.
os.environ.setdefault("API_KEY", "test-key")

from agent import loop  # noqa: E402


def choice(delta=None, finish_reason=None):
    return SimpleNamespace(delta=delta, finish_reason=finish_reason)


def chunk(delta=None, finish_reason=None):
    return SimpleNamespace(choices=[choice(delta, finish_reason)])


def empty_chunk():
    return SimpleNamespace(choices=[])


def text_delta(text):
    return chunk(SimpleNamespace(content=text, tool_calls=None))


def tool_delta(index, call_id=None, name=None, arguments=None):
    function = SimpleNamespace(name=name, arguments=arguments)
    return chunk(
        SimpleNamespace(
            content=None,
            tool_calls=[
                SimpleNamespace(index=index, id=call_id, function=function)
            ],
        )
    )


class RecordingChat:
    """Small fake that snapshots mutable messages at request time."""

    def __init__(self, streams):
        self._streams = iter(streams)
        self.requests = []

    def create(self, **kwargs):
        snapshot = dict(kwargs)
        snapshot["messages"] = list(kwargs["messages"])
        self.requests.append(snapshot)
        return next(self._streams)


class StreamEventsTests(unittest.TestCase):
    def run_with_streams(self, items, streams, *, max_turns=10, tool_results=None):
        chat = RecordingChat(streams)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=chat))
        execute = Mock(side_effect=tool_results) if tool_results is not None else Mock()

        with (
            patch.object(loop, "client", fake_client),
            patch.object(loop, "execute_tool", execute),
        ):
            events = list(loop.stream_events(items, max_turns=max_turns))

        return events, chat.requests, execute

    def assert_chat_request(self, request):
        self.assertEqual(request["model"], loop.MODEL)
        self.assertIs(request["tools"], loop.CHAT_TOOLS)
        self.assertIs(request["stream"], True)
        for tool in request["tools"]:
            self.assertEqual(tool["type"], "function")
            self.assertIn("name", tool["function"])
            self.assertIn("parameters", tool["function"])
            self.assertNotIn("strict", tool["function"])

    def test_text_stream_accumulates_deltas_into_done_content(self):
        initial = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ]
        items = list(initial)

        events, requests, execute = self.run_with_streams(
            items,
            [
                [
                    empty_chunk(),
                    text_delta("最终"),
                    text_delta("完整文本"),
                    chunk(finish_reason="stop"),
                ]
            ],
        )

        self.assertEqual(
            events,
            [
                {"type": "text_delta", "text": "最终"},
                {"type": "text_delta", "text": "完整文本"},
                {"type": "done", "content": "最终完整文本"},
            ],
        )
        self.assertEqual(len(requests), 1)
        self.assert_chat_request(requests[0])
        self.assertEqual(requests[0]["messages"], initial)
        self.assertEqual(
            items,
            [*initial, {"role": "assistant", "content": "最终完整文本"}],
        )
        execute.assert_not_called()

    def test_empty_reply_is_not_appended_to_history(self):
        items = [{"role": "user", "content": "hello"}]

        events, requests, execute = self.run_with_streams(
            items,
            [[empty_chunk(), chunk(finish_reason="stop")]],
        )

        self.assertEqual(events, [{"type": "done", "content": ""}])
        self.assertEqual(items, [{"role": "user", "content": "hello"}])
        execute.assert_not_called()

    def test_parallel_tool_calls_accumulate_across_chunks_and_use_call_id(self):
        initial = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "calculate both"},
        ]
        items = list(initial)

        events, requests, execute = self.run_with_streams(
            items,
            [
                [
                    text_delta("我来计算"),
                    tool_delta(0, call_id="call_add", name="calculate"),
                    tool_delta(0, arguments='{"expression"'),
                    tool_delta(0, arguments=':"1+2"}'),
                    tool_delta(1, call_id="call_multiply", name="calculate"),
                    tool_delta(1, arguments='{"expression":"2*3"}'),
                ],
                [text_delta("答案是 3 和 6")],
            ],
            tool_results=["3", "6"],
        )

        assistant_message = {
            "role": "assistant",
            "content": "我来计算",
            "tool_calls": [
                {
                    "id": "call_add",
                    "type": "function",
                    "function": {
                        "name": "calculate",
                        "arguments": '{"expression":"1+2"}',
                    },
                },
                {
                    "id": "call_multiply",
                    "type": "function",
                    "function": {
                        "name": "calculate",
                        "arguments": '{"expression":"2*3"}',
                    },
                },
            ],
        }
        tool_messages = [
            {"role": "tool", "tool_call_id": "call_add", "content": "3"},
            {"role": "tool", "tool_call_id": "call_multiply", "content": "6"},
        ]

        self.assertEqual(
            events,
            [
                {"type": "text_delta", "text": "我来计算"},
                {
                    "type": "tool_call",
                    "id": "call_add",
                    "name": "calculate",
                    "args": {"expression": "1+2"},
                },
                {"type": "tool_result", "id": "call_add", "content": "3"},
                {
                    "type": "tool_call",
                    "id": "call_multiply",
                    "name": "calculate",
                    "args": {"expression": "2*3"},
                },
                {"type": "tool_result", "id": "call_multiply", "content": "6"},
                {"type": "text_delta", "text": "答案是 3 和 6"},
                {"type": "done", "content": "答案是 3 和 6"},
            ],
        )
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assert_chat_request(request)

        self.assertEqual(requests[0]["messages"], initial)
        self.assertEqual(requests[1]["messages"], [*initial, assistant_message, *tool_messages])
        self.assertEqual(
            items,
            [
                *initial,
                assistant_message,
                *tool_messages,
                {"role": "assistant", "content": "答案是 3 和 6"},
            ],
        )
        execute.assert_has_calls(
            [
                call("calculate", {"expression": "1+2"}, None),
                call("calculate", {"expression": "2*3"}, None),
            ]
        )

    def test_tool_calls_stop_at_max_turns(self):
        items = [{"role": "user", "content": "keep calling"}]

        events, requests, execute = self.run_with_streams(
            items,
            [
                [tool_delta(0, call_id="call_1", name="calculate", arguments='{"expression":"1+1"}')],
                [tool_delta(0, call_id="call_2", name="calculate", arguments='{"expression":"2+2"}')],
            ],
            max_turns=2,
            tool_results=["2", "4"],
        )

        self.assertEqual(events[-1], {"type": "max_turns"})
        self.assertNotIn("done", [event["type"] for event in events])
        self.assertEqual(len(requests), 2)
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(
            [
                entry["tool_call_id"]
                for entry in items
                if isinstance(entry, dict) and entry.get("role") == "tool"
            ],
            ["call_1", "call_2"],
        )

    def test_slow_model_stream_becomes_timeout_error(self):
        items = [{"role": "user", "content": "hello"}]
        with patch.object(loop.time, "monotonic", side_effect=[0, 1, 301]):
            events, _, execute = self.run_with_streams(items, [[text_delta("late")]])
        self.assertEqual(events, [{"type": "error", "message": "TimeoutError: 本轮回复超时，请重试"}])
        execute.assert_not_called()

    def test_slow_tool_does_not_start_next_model_request(self):
        items = [{"role": "user", "content": "hello"}]
        stream = [[tool_delta(0, call_id="call_1", name="get_weather", arguments='{"city":"北京"}')]]
        with patch.object(loop.time, "monotonic", side_effect=[0, 1, 2, 3, 301]):
            events, requests, execute = self.run_with_streams(items, stream, tool_results=["晴"])
        self.assertEqual([event["type"] for event in events], ["tool_call", "error"])
        self.assertIn("TimeoutError", events[-1]["message"])
        self.assertEqual(len(requests), 1)
        execute.assert_called_once()

    def test_request_exception_becomes_error_event(self):
        create = Mock(side_effect=ConnectionError("network down"))
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        with patch.object(loop, "client", fake_client):
            events = list(loop.stream_events([{"role": "user", "content": "hello"}]))

        self.assertEqual(events, [{"type": "error", "message": "ConnectionError: network down"}])
        create.assert_called_once()

    def test_stream_iteration_exception_keeps_prior_deltas_then_reports_error(self):
        class BrokenStream:
            def __iter__(self):
                yield text_delta("partial")
                raise TimeoutError("stream timed out")

        events, requests, execute = self.run_with_streams(
            [{"role": "user", "content": "hello"}],
            [BrokenStream()],
        )

        self.assertEqual(
            events,
            [
                {"type": "text_delta", "text": "partial"},
                {"type": "error", "message": "TimeoutError: stream timed out"},
            ],
        )
        self.assertEqual(len(requests), 1)
        execute.assert_not_called()

    def test_invalid_tool_arguments_become_error_event(self):
        events, requests, execute = self.run_with_streams(
            [{"role": "user", "content": "hello"}],
            [
                [
                    tool_delta(
                        0,
                        call_id="call_bad",
                        name="calculate",
                        arguments="{not-json",
                    )
                ]
            ],
        )

        self.assertEqual([entry["type"] for entry in events], ["error"])
        self.assertIn("JSONDecodeError", events[0]["message"])
        self.assertEqual(len(requests), 1)
        execute.assert_not_called()

    def test_non_object_tool_arguments_become_error_event(self):
        events, requests, execute = self.run_with_streams(
            [{"role": "user", "content": "hello"}],
            [
                [
                    tool_delta(
                        0,
                        call_id="call_bad",
                        name="calculate",
                        arguments='["1 + 2"]',
                    )
                ]
            ],
        )

        self.assertEqual([entry["type"] for entry in events], ["error"])
        self.assertIn("必须是 JSON 对象", events[0]["message"])
        self.assertEqual(len(requests), 1)
        execute.assert_not_called()

    def test_invalid_arguments_leave_no_assistant_tool_call_in_history(self):
        items = [{"role": "user", "content": "hello"}]

        self.run_with_streams(
            items,
            [
                [
                    tool_delta(0, call_id="call_bad", name="calculate", arguments="{oops")
                ]
            ],
        )

        self.assertEqual(items, [{"role": "user", "content": "hello"}])


if __name__ == "__main__":
    unittest.main()
