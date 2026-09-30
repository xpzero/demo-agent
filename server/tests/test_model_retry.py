"""Model request retries and cooperative stopping without a live gateway."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("API_KEY", "test-key")

from agent import loop  # noqa: E402
from agent.metrics import TurnRecorder  # noqa: E402


def text(value):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=value, tool_calls=None))])


def tool(call_id):
    delta = SimpleNamespace(
        index=0, id=call_id,
        function=SimpleNamespace(name="calculate", arguments='{"expression":"1+1"}'),
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[delta]))])


class RetryTests(unittest.TestCase):
    def run_loop(self, responses, **kwargs):
        create = Mock(side_effect=responses)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        items = [{"role": "user", "content": "hello"}]
        with patch.object(loop, "client", client), patch.object(loop.time, "sleep") as sleep:
            events = list(loop.stream_events(items, **kwargs))
        return events, items, create, sleep

    def test_transient_failure_then_fallback_keeps_messages_and_stream_protocol(self):
        from openai import APIConnectionError

        error = APIConnectionError(request=Mock(), message="disconnected")
        recorder = TurnRecorder()
        events, items, create, sleep = self.run_loop(
            [error, error, [text("ok")]], max_attempts=3,
            fallback_model="backup", recorder=recorder,
        )
        self.assertEqual(events, [{"type": "text_delta", "text": "ok"}, {"type": "done", "content": "ok"}])
        self.assertEqual([call.kwargs["model"] for call in create.call_args_list],
                         [loop.MODEL, loop.MODEL, "backup"])
        self.assertTrue(all(call.kwargs["stream"] is True for call in create.call_args_list))
        self.assertEqual(create.call_args_list[0].kwargs["messages"], items)
        self.assertEqual(items[-1], {"role": "assistant", "content": "ok"})
        self.assertEqual([turn.ok for turn in recorder.turns], [False, False, True])
        self.assertEqual(sleep.call_count, 2)

    def test_total_request_budget_and_retry_cap(self):
        from openai import APIConnectionError

        error = APIConnectionError(request=Mock(), message="down")
        recorded = []
        events, items, create, _ = self.run_loop(
            [error] * 4, max_turns=2, max_attempts=3,
            on_model_attempt=recorded.append,
        )
        self.assertEqual(len(create.call_args_list), 3)
        self.assertEqual(recorded, [True, False, False])
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(items, [{"role": "user", "content": "hello"}])
        events, _, create, _ = self.run_loop([error] * 4, max_attempts=2)
        self.assertEqual(len(create.call_args_list), 2)
        self.assertEqual(events[-1]["type"], "error")

    def test_status_retry_policy(self):
        import httpx
        from openai import APIStatusError

        request = httpx.Request("POST", "https://example.invalid/chat")
        for status, expected_attempts in [(429, 2), (503, 2), (400, 1)]:
            with self.subTest(status=status):
                response = httpx.Response(status, request=request)
                error = APIStatusError("gateway", response=response, body=None)
                events, _, create, _ = self.run_loop([error, [text("ok")]])
                self.assertEqual(create.call_count, expected_attempts)
                self.assertEqual(events[-1]["type"], "done" if expected_attempts == 2 else "error")

    def test_non_transient_and_emitted_stream_errors_do_not_retry(self):
        from openai import APIConnectionError

        events, _, create, _ = self.run_loop([ValueError("bad request")])
        self.assertEqual(len(create.call_args_list), 1)
        self.assertEqual(events, [{"type": "error", "message": "ValueError: bad request"}])

        def broken():
            yield text("partial")
            raise APIConnectionError(request=Mock(), message="lost")

        events, _, create, _ = self.run_loop([broken()])
        self.assertEqual([event["type"] for event in events], ["text_delta", "error"])
        self.assertEqual(len(create.call_args_list), 1)

    def test_pre_output_stream_failure_can_retry_without_duplicate_delta(self):
        from openai import APIConnectionError

        def broken():
            yield SimpleNamespace(choices=[])
            raise APIConnectionError(request=Mock(), message="lost")

        events, _, create, _ = self.run_loop([broken(), [text("fresh")]])
        self.assertEqual([event["type"] for event in events], ["text_delta", "done"])
        self.assertEqual(len(create.call_args_list), 2)

    def test_persisted_deadline_expires_before_request(self):
        events, _, create, _ = self.run_loop([], deadline_at=0)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("TimeoutError", events[-1]["message"])
        create.assert_not_called()

    def test_future_persisted_deadline_allows_request(self):
        with patch.object(loop.time, "time", return_value=100):
            events, _, create, _ = self.run_loop([[text("ok")]], deadline_at=200)
        self.assertEqual(events[-1], {"type": "done", "content": "ok"})
        create.assert_called_once()

    def test_stop_before_model_request(self):
        events, items, create, _ = self.run_loop([], should_stop=lambda: True)
        self.assertEqual(events, [{"type": "stopped"}])
        create.assert_not_called()
        self.assertEqual(len(items), 1)

    def test_stop_after_tool_call_prevents_execution_and_dangling_history(self):
        create = Mock(side_effect=[[tool("a")]])
        execute = Mock()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        items = [{"role": "user", "content": "go"}]
        stopped = False
        with patch.object(loop, "client", client), patch.object(loop, "execute_tool", execute):
            events = []
            for event in loop.stream_events(items, should_stop=lambda: stopped):
                events.append(event)
                if event["type"] == "tool_call":
                    stopped = True
        self.assertEqual([event["type"] for event in events], ["tool_call", "stopped"])
        execute.assert_not_called()
        self.assertEqual(items, [{"role": "user", "content": "go"}])

    def test_stop_after_first_tool_keeps_only_paired_call(self):
        first = tool("a")
        second = tool("b")
        second.choices[0].delta.tool_calls[0].index = 1
        create = Mock(return_value=[first, second])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        items = [{"role": "user", "content": "go"}]
        stopped = False
        with patch.object(loop, "client", client), patch.object(loop, "execute_tool", return_value=("2", True)) as execute:
            events = []
            for event in loop.stream_events(items, should_stop=lambda: stopped):
                events.append(event)
                if event["type"] == "tool_result":
                    stopped = True
        self.assertEqual([event["type"] for event in events], ["tool_call", "tool_result", "stopped"])
        execute.assert_called_once()
        self.assertEqual([call["id"] for call in items[1]["tool_calls"]], ["a"])
        self.assertEqual(items[2], {"role": "tool", "tool_call_id": "a", "content": "2"})

    def test_stop_during_stream(self):
        stopped = False
        create = Mock(return_value=[text("first"), text("second")])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(loop, "client", client):
            events = []
            for event in loop.stream_events([], should_stop=lambda: stopped):
                events.append(event)
                stopped = True
        self.assertEqual(events, [{"type": "text_delta", "text": "first"}, {"type": "stopped"}])

    def test_recovery_requires_validated_suggestion_without_leaking_model_text(self):
        suggestion = tool("r")
        suggestion.choices[0].delta.tool_calls[0].function.name = "recovery_suggestion"
        suggestion.choices[0].delta.tool_calls[0].function.arguments = '{"intent":"reply"}'
        validate = Mock(return_value='{"outcome":"allow","facts":[]}')
        events, items, create, _ = self.run_loop(
            [[text("业务已成功"), suggestion]], recovery_prompt="facts",
            on_recovery_suggestion=validate,
        )
        self.assertEqual([event["type"] for event in events],
                         ["tool_call", "tool_result", "done"])
        # tool_result 保留机器可读裁决；done.content 是由后端依据
        # 核对后事实组成的用户可读文本，不是模型文字或原始 JSON。
        self.assertEqual(events[1]["content"], validate.return_value)
        self.assertEqual(events[-1]["content"], "已核对当前任务事实。")
        self.assertEqual(items[-1], {"role": "assistant", "content": "已核对当前任务事实。"})
        self.assertEqual(validate.call_args.args, ({"intent": "reply"},))
        self.assertEqual(create.call_count, 1)

    def test_recovery_answer_renders_business_result_and_check_time(self):
        suggestion = tool("r")
        suggestion.choices[0].delta.tool_calls[0].function.name = "recovery_suggestion"
        verdict = ('{"outcome":"observed","operation_id":7,"status":"succeeded",'
                   '"confirmed_result":"订单已确认：样品 A"}')
        events, items, create, _ = self.run_loop(
            [[suggestion]], recovery_prompt="facts",
            on_recovery_suggestion=lambda _: verdict,
        )
        self.assertEqual([event["type"] for event in events],
                         ["tool_call", "tool_result", "done"])
        answer = events[-1]["content"]
        self.assertIn("业务操作 7 已成功：订单已确认：样品 A", answer)
        self.assertIn("核对时间", answer)
        self.assertNotIn('{"outcome"', answer)
        self.assertEqual(items[-1], {"role": "assistant", "content": answer})

    def test_recovery_answer_marks_unconfirmed_and_never_replays(self):
        suggestion = tool("r")
        suggestion.choices[0].delta.tool_calls[0].function.name = "recovery_suggestion"
        verdict = ('{"outcome":"observed","operation_id":9,"status":"unconfirmed",'
                   '"detail":"业务系统暂不可查"}')
        events, _, _, _ = self.run_loop(
            [[suggestion]], recovery_prompt="facts",
            on_recovery_suggestion=lambda _: verdict,
        )
        answer = events[-1]["content"]
        self.assertIn("业务操作 9 结果待核实", answer)
        self.assertIn("不会自动重做该操作", answer)
        self.assertIn("核对时间", answer)

    def test_invalid_recovery_verdict_cannot_finish(self):
        suggestion = tool("r")
        suggestion.choices[0].delta.tool_calls[0].function.name = "recovery_suggestion"
        events, items, create, _ = self.run_loop(
            [[text("已完成"), suggestion], [text("已完成"), suggestion]], recovery_prompt="facts",
            on_recovery_suggestion=lambda _: '{"outcome":"invalid"}',
        )
        # One corrective retry is consumed before failing closed.
        self.assertEqual([event["type"] for event in events],
                         ["tool_call", "tool_result", "tool_call", "tool_result", "error"])
        self.assertNotIn("已完成", str(events))
        self.assertGreaterEqual(len(items), 1)
        self.assertEqual(create.call_count, 2)

    def test_recovery_plain_text_and_mixed_tools_fail_closed(self):
        validate = Mock(return_value='{"outcome":"valid"}')
        for stream in ([text("业务已成功")], [tool("unsafe")]):
            with self.subTest(stream=stream):
                # One corrective retry is allowed; the retry repeats the same
                # bad shape, so the loop must still fail closed.
                events, items, create, _ = self.run_loop(
                    [stream, stream], recovery_prompt="facts", on_recovery_suggestion=validate,
                )
                self.assertEqual([e["type"] for e in events], ["error"])
                self.assertNotIn("业务已成功", str(events))
                self.assertEqual(create.call_count, 2)
                self.assertGreaterEqual(len(items), 1)
        validate.assert_not_called()

    def test_retry_after_tool_draft_or_visible_text_is_forbidden(self):
        from openai import APIConnectionError

        def broken(chunk):
            yield chunk
            raise APIConnectionError(request=Mock(), message="lost")

        for chunk in (tool("a"), text("partial")):
            with self.subTest(chunk=chunk):
                events, items, create, _ = self.run_loop([broken(chunk)])
                self.assertEqual(create.call_count, 1)
                self.assertEqual(events[-1]["type"], "error")
                self.assertEqual(len(items), 1)

    def test_request_timeout_is_bounded_by_remaining_run_time(self):
        with patch.object(loop.time, "time", return_value=100):
            _, _, create, _ = self.run_loop([[text("ok")]], deadline_at=110)
        self.assertLessEqual(create.call_args.kwargs["timeout"], 10)
        self.assertEqual(create.call_args.kwargs["stream"], True)

    def test_stop_during_retry_backoff_does_not_send_second_attempt(self):
        from openai import APIConnectionError

        stopped = False
        create = Mock(side_effect=[APIConnectionError(request=Mock(), message="lost"), [text("late")]])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        def stop_on_sleep(_delay):
            nonlocal stopped
            stopped = True

        with patch.object(loop, "client", client), patch.object(loop.time, "sleep", side_effect=stop_on_sleep):
            events = list(loop.stream_events([], should_stop=lambda: stopped))
        self.assertEqual(events, [{"type": "stopped"}])
        create.assert_called_once()

    def test_ten_logical_requests_and_three_attempts_per_request(self):
        from openai import APIConnectionError

        error = APIConnectionError(request=Mock(), message="lost")
        create = Mock(side_effect=[item for n in range(10) for item in
                                   (error, error, [tool(str(n))])])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        attempts = []
        with patch.object(loop, "client", client), patch.object(loop, "execute_tool", return_value=("2", True)), patch.object(loop.time, "sleep"):
            events = list(loop.stream_events([], max_turns=100, max_attempts=100,
                                             on_model_attempt=attempts.append))
        self.assertEqual(events[-1], {"type": "max_turns"})
        self.assertEqual(create.call_count, 30)
        self.assertEqual(attempts, [True, False, False] * 10)


if __name__ == "__main__":
    unittest.main()
