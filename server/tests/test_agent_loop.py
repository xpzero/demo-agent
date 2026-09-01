import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch


# agent.client constructs the SDK client at import time. Unit tests never make a
# network request, but the SDK still requires a syntactically present key.
os.environ.setdefault("OPENAI_API_KEY", "test-key")

from agent import approval, loop  # noqa: E402
from services import ServiceContainer  # noqa: E402
from services.permission import PermissionDecision  # noqa: E402


def item(item_type: str, **fields):
    return SimpleNamespace(type=item_type, **fields)


def response(output, output_text: str = "", **fields):
    return SimpleNamespace(output=output, output_text=output_text, **fields)


def event(event_type: str, **fields):
    return SimpleNamespace(type=event_type, **fields)


def completed(output, output_text: str = ""):
    return event(
        "response.completed",
        response=response(output=output, output_text=output_text),
    )


class RecordingResponses:
    """Small fake that snapshots mutable input at request time."""

    def __init__(self, streams):
        self._streams = iter(streams)
        self.requests = []

    def create(self, **kwargs):
        snapshot = dict(kwargs)
        snapshot["input"] = list(kwargs["input"])
        self.requests.append(snapshot)
        return next(self._streams)


class FakePermissionService:
    def __init__(self, actions=None):
        self.actions = actions or {}
        self.checks = []

    def check(self, requests, context):
        self.checks.append((requests, context))
        return PermissionDecision(self.actions.get(context.tool_name, "allow"))


class StreamEventsTests(unittest.TestCase):
    def run_with_streams(
        self,
        items,
        streams,
        *,
        max_turns=10,
        tool_results=None,
        policies=None,
        preview=None,
    ):
        responses = RecordingResponses(streams)
        fake_client = SimpleNamespace(responses=responses)
        execute = Mock(side_effect=tool_results) if tool_results is not None else Mock()
        policies = policies or {}
        permission_service = FakePermissionService(policies)
        services = ServiceContainer(permission=permission_service)
        checkpoints = []

        with (
            patch.object(loop, "client", fake_client),
            patch.object(approval, "execute_tool", execute),
            patch.object(approval, "preview_tool", return_value=preview),
        ):
            events = list(
                loop.stream_events(
                    items,
                    services,
                    max_turns=max_turns,
                    on_approval=checkpoints.append,
                )
            )

        self.checkpoints = checkpoints
        self.permission_service = permission_service
        return events, responses.requests, execute

    def assert_responses_request(self, request):
        self.assertEqual(request["model"], loop.MODEL)
        self.assertIs(request["tools"], loop.TOOLS)
        self.assertIs(request["stream"], True)
        self.assertIs(request["store"], False)
        self.assertEqual(request["include"], ["reasoning.encrypted_content"])
        for tool in request["tools"]:
            self.assertEqual(tool["type"], "function")
            self.assertIn("name", tool)
            self.assertIn("parameters", tool)
            self.assertIs(tool["strict"], False)
            self.assertNotIn("function", tool)

    def test_text_stream_uses_completed_response_as_source_of_truth(self):
        initial = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ]
        output_message = item(
            "message",
            id="msg_1",
            role="assistant",
            content=[{"type": "output_text", "text": "最终完整文本"}],
        )
        items = list(initial)

        events, requests, execute = self.run_with_streams(
            items,
            [
                [
                    event("response.created"),
                    event("response.output_text.delta", delta="最终"),
                    event("response.output_text.delta", delta="完整文本"),
                    completed([output_message], output_text="最终完整文本"),
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
        self.assert_responses_request(requests[0])
        self.assertEqual(requests[0]["input"], initial)
        self.assertEqual(items[:2], initial)
        self.assertIs(items[2], output_message)
        execute.assert_not_called()

    def test_parallel_function_calls_preserve_every_output_item_and_use_call_id(self):
        initial = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "calculate both"},
        ]
        reasoning = item("reasoning", id="rs_1", encrypted_content="opaque")
        commentary = item(
            "message",
            id="msg_1",
            role="assistant",
            content=[{"type": "output_text", "text": "我来计算"}],
        )
        first_call = item(
            "function_call",
            id="fc_1",
            call_id="call_add",
            name="calculate",
            arguments='{"expression":"1+2"}',
        )
        second_call = item(
            "function_call",
            id="fc_2",
            call_id="call_multiply",
            name="calculate",
            arguments='{"expression":"2*3"}',
        )
        final_reasoning = item("reasoning", id="rs_2", encrypted_content="opaque-2")
        final_message = item(
            "message",
            id="msg_2",
            role="assistant",
            content=[{"type": "output_text", "text": "答案是 3 和 6"}],
        )
        items = list(initial)

        events, requests, execute = self.run_with_streams(
            items,
            [
                [
                    event("response.output_text.delta", delta="我来计算"),
                    completed(
                        [reasoning, commentary, first_call, second_call],
                        output_text="我来计算",
                    ),
                ],
                [
                    event("response.output_text.delta", delta="答案是 3 和 6"),
                    completed(
                        [final_reasoning, final_message],
                        output_text="答案是 3 和 6",
                    ),
                ],
            ],
            tool_results=["3", "6"],
        )

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
            self.assert_responses_request(request)

        second_input = requests[1]["input"]
        self.assertEqual(second_input[:2], initial)
        for actual, expected in zip(
            second_input[2:6], [reasoning, commentary, first_call, second_call]
        ):
            self.assertIs(actual, expected)
        self.assertEqual(
            second_input[6:],
            [
                {
                    "type": "function_call_output",
                    "call_id": "call_add",
                    "output": "3",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_multiply",
                    "output": "6",
                },
            ],
        )
        execute.assert_has_calls(
            [
                call("calculate", {"expression": "1+2"}),
                call("calculate", {"expression": "2*3"}),
            ]
        )

        expected_all = [
            *initial,
            reasoning,
            commentary,
            first_call,
            second_call,
            {
                "type": "function_call_output",
                "call_id": "call_add",
                "output": "3",
            },
            {
                "type": "function_call_output",
                "call_id": "call_multiply",
                "output": "6",
            },
            final_reasoning,
            final_message,
        ]
        self.assertEqual(items, expected_all)

    def test_function_calls_stop_at_max_turns(self):
        items = [{"role": "user", "content": "keep calling"}]
        first_call = item(
            "function_call",
            call_id="call_1",
            name="calculate",
            arguments='{"expression":"1+1"}',
        )
        second_call = item(
            "function_call",
            call_id="call_2",
            name="calculate",
            arguments='{"expression":"2+2"}',
        )

        events, requests, execute = self.run_with_streams(
            items,
            [
                [completed([first_call])],
                [completed([second_call])],
            ],
            max_turns=2,
            tool_results=["2", "4"],
        )

        self.assertEqual(events[-1], {"type": "max_turns"})
        self.assertNotIn("done", [event["type"] for event in events])
        self.assertEqual(len(requests), 2)
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(
            [entry["call_id"] for entry in items if isinstance(entry, dict) and entry.get("type") == "function_call_output"],
            ["call_1", "call_2"],
        )

    def test_ask_tool_is_checkpointed_before_it_can_execute(self):
        items = [{"role": "user", "content": "overwrite the file"}]
        write_call = item(
            "function_call",
            call_id="call_write",
            name="write_file",
            arguments='{"path":"notes/demo.txt","content":"new"}',
        )
        diff = {
            "type": "code_diff",
            "path": "notes/demo.txt",
            "additions": 1,
            "deletions": 1,
            "lines": [
                {"kind": "removed", "text": "old"},
                {"kind": "added", "text": "new"},
            ],
        }

        events, requests, execute = self.run_with_streams(
            items,
            [[completed([write_call])]],
            policies={"write_file": "ask"},
            preview=diff,
        )

        self.assertEqual(len(requests), 1)
        execute.assert_not_called()
        self.assertEqual(len(self.checkpoints), 1)
        self.assertEqual(self.checkpoints[0]["remaining_turns"], 9)
        self.assertEqual(
            events,
            [
                {
                    "type": "tool_call",
                    "id": "call_write",
                    "name": "write_file",
                    "args": {"path": "notes/demo.txt", "content": "new"},
                    "approval_required": True,
                    "preview": diff,
                },
                {"type": "approval_required", "call_ids": ["call_write"]},
            ],
        )
        self.assertIs(items[-1], write_call)
        self.assertFalse(
            any(
                isinstance(entry, dict) and entry.get("type") == "function_call_output"
                for entry in items
            )
        )

    def test_denied_tool_returns_output_without_approval_or_execution(self):
        items = [{"role": "user", "content": "overwrite the file"}]
        write_call = item(
            "function_call",
            call_id="call_write",
            name="write_file",
            arguments='{"path":"notes/demo.txt","content":"new"}',
        )
        final_message = item(
            "message",
            role="assistant",
            content=[{"type": "output_text", "text": "没有写入"}],
        )

        events, requests, execute = self.run_with_streams(
            items,
            [
                [completed([write_call])],
                [completed([final_message], output_text="没有写入")],
            ],
            policies={"write_file": "deny"},
        )

        execute.assert_not_called()
        self.assertEqual(self.checkpoints, [])
        self.assertEqual(len(requests), 2)
        self.assertIn("权限规则拒绝", events[1]["content"])
        self.assertEqual(events[1]["outcome"], "denied")
        self.assertNotIn("approval_required", [event["type"] for event in events])

    def test_failed_response_becomes_error_event(self):
        failure = response(
            output=[],
            error=SimpleNamespace(message="upstream exploded", code="server_error"),
        )
        events, requests, execute = self.run_with_streams(
            [{"role": "user", "content": "hello"}],
            [[event("response.failed", response=failure, message="upstream exploded")]],
        )

        self.assertEqual([entry["type"] for entry in events], ["error"])
        self.assertIn("upstream exploded", events[0]["message"])
        self.assertEqual(len(requests), 1)
        execute.assert_not_called()

    def test_incomplete_response_becomes_error_event(self):
        incomplete = response(
            output=[],
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        )
        events, requests, execute = self.run_with_streams(
            [{"role": "user", "content": "hello"}],
            [[event("response.incomplete", response=incomplete, message="max_output_tokens")]],
        )

        self.assertEqual([entry["type"] for entry in events], ["error"])
        self.assertIn("max_output_tokens", events[0]["message"])
        self.assertEqual(len(requests), 1)
        execute.assert_not_called()

    def test_stream_error_event_becomes_agent_error_event(self):
        events, requests, execute = self.run_with_streams(
            [{"role": "user", "content": "hello"}],
            [[event("error", message="stream disconnected", code="connection_error")]],
        )

        self.assertEqual([entry["type"] for entry in events], ["error"])
        self.assertIn("stream disconnected", events[0]["message"])
        self.assertEqual(len(requests), 1)
        execute.assert_not_called()

    def test_request_exception_becomes_error_event(self):
        create = Mock(side_effect=ConnectionError("network down"))
        fake_client = SimpleNamespace(responses=SimpleNamespace(create=create))
        services = ServiceContainer(permission=FakePermissionService())

        with patch.object(loop, "client", fake_client):
            events = list(
                loop.stream_events([{"role": "user", "content": "hello"}], services)
            )

        self.assertEqual(events, [{"type": "error", "message": "ConnectionError: network down"}])
        create.assert_called_once()

    def test_stream_iteration_exception_keeps_prior_deltas_then_reports_error(self):
        class BrokenStream:
            def __iter__(self):
                yield event("response.output_text.delta", delta="partial")
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

    def test_stream_without_completed_event_becomes_error_event(self):
        events, requests, execute = self.run_with_streams(
            [{"role": "user", "content": "hello"}],
            [[event("response.created"), event("response.in_progress")]],
        )

        self.assertEqual([entry["type"] for entry in events], ["error"])
        self.assertIn("completed", events[0]["message"])
        self.assertEqual(len(requests), 1)
        execute.assert_not_called()

    def test_invalid_function_arguments_become_error_event(self):
        bad_call = item(
            "function_call",
            call_id="call_bad",
            name="calculate",
            arguments="{not-json",
        )
        events, requests, execute = self.run_with_streams(
            [{"role": "user", "content": "hello"}],
            [[completed([bad_call])]],
        )

        self.assertEqual([entry["type"] for entry in events], ["error"])
        self.assertIn("JSONDecodeError", events[0]["message"])
        self.assertEqual(len(requests), 1)
        execute.assert_not_called()

    def test_non_object_function_arguments_become_error_event(self):
        bad_call = item(
            "function_call",
            call_id="call_bad",
            name="calculate",
            arguments='["1 + 2"]',
        )
        events, requests, execute = self.run_with_streams(
            [{"role": "user", "content": "hello"}],
            [[completed([bad_call])]],
        )

        self.assertEqual([entry["type"] for entry in events], ["error"])
        self.assertIn("必须是 JSON 对象", events[0]["message"])
        self.assertEqual(len(requests), 1)
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
