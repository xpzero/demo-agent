import unittest

from agent.metrics import TurnRecorder, TurnRecord, ToolRecord


class TurnRecorderTests(unittest.TestCase):
    def test_starts_empty(self):
        recorder = TurnRecorder()
        self.assertEqual(recorder.turns, [])
        self.assertIsNone(recorder.total_usage)

    def test_start_turn_returns_record(self):
        recorder = TurnRecorder()
        record = recorder.start_turn()
        self.assertEqual(len(recorder.turns), 1)
        self.assertIs(recorder.turns[0], record)
        self.assertIsNone(record.usage)
        self.assertEqual(record.tools, [])

    def record_one_turn(self, **kwargs):
        recorder = TurnRecorder()
        with recorder.track_turn() as record:
            for key, value in kwargs.items():
                setattr(record, key, value)
        return recorder

    def test_context_manager_registers_on_exit(self):
        recorder = TurnRecorder()
        with recorder.track_turn() as record:
            record.usage = {"prompt_tokens": 3, "completion_tokens": 1}
        # 耗时由 tracker 在退出时自动计算，不依赖手动设置
        duration = recorder.turns[0].duration_ms
        self.assertIsNotNone(duration)
        self.assertGreaterEqual(duration, 0.0)
        totals = recorder.total_usage
        self.assertEqual(totals["prompt_tokens"], 3)

    def test_exception_inside_turn_still_registers(self):
        recorder = TurnRecorder()
        with self.assertRaises(ValueError):
            with recorder.track_turn():
                raise ValueError("boom")
        self.assertEqual(len(recorder.turns), 1)

    def test_total_usage_sums_partial_usage(self):
        recorder = TurnRecorder()
        with recorder.track_turn() as first:
            first.usage = {"prompt_tokens": 10, "completion_tokens": 5}
        with recorder.track_turn() as second:
            second.usage = None  # 无 usage 的请求不贡献
        self.assertEqual(
            recorder.total_usage,
            {"prompt_tokens": 10, "completion_tokens": 5},
        )

    def test_tool_record_fields(self):
        tool = ToolRecord(
            name="web_search",
            args_excerpt='{"query": "天气"}',
            result_excerpt="北京晴",
            duration_ms=123.4,
            ok=True,
        )
        self.assertEqual(tool.name, "web_search")
        self.assertTrue(tool.ok)


class ExcerptTests(unittest.TestCase):
    def test_excerpts_are_truncated_to_500(self):
        from agent.metrics import excerpt

        long_text = "x" * 600
        self.assertEqual(len(excerpt(long_text)), 500)
        self.assertEqual(excerpt("short"), "short")

    def test_excerpt_accepts_non_string(self):
        from agent.metrics import excerpt

        self.assertEqual(excerpt(None), "")
        self.assertEqual(excerpt(12345), "12345")


class AgentTurnModelTests(unittest.TestCase):
    def test_turn_record_defaults(self):
        record = TurnRecord()
        self.assertIsNone(record.duration_ms)
        self.assertIsNone(record.usage)
        self.assertEqual(record.tools, [])
        self.assertTrue(record.ok)  # 请求本身没抛异常即视为成功


class ExportAndSummarizeTests(unittest.TestCase):
    def make_recorder(self):
        from agent.metrics import TurnRecorder
        from agent.metrics import TurnRecord

        recorder = TurnRecorder()
        with_usage = TurnRecord(
            usage={"prompt_tokens": 100, "completion_tokens": 20},
            reply_text="实测轮",
        )
        without_usage = TurnRecord(
            items_snapshot=[
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "问十个字的问题"},
            ],
            reply_text="估",
        )
        recorder.turns = [with_usage, without_usage]
        return recorder

    def test_export_uses_measured_when_available(self):
        from agent.metrics import export_turns

        turns = export_turns(self.make_recorder())
        self.assertEqual(turns[0]["prompt_tokens"], 100)
        # 有实测时估算列同填实测值（每行两口径同落，便于校准）
        self.assertEqual(turns[0]["estimated_prompt_tokens"], 100)
        self.assertEqual(turns[0]["estimated_completion_tokens"], 20)

    def test_export_estimates_from_snapshot_without_usage(self):
        from agent.metrics import export_turns

        turns = export_turns(self.make_recorder())
        second = turns[1]
        # 快照内容长度 = len("sys") + len("问十个字的问题") = 3 + 7 = 10
        self.assertEqual(second["estimated_prompt_tokens"], 10)
        self.assertEqual(second["estimated_completion_tokens"], 1)

    def test_summarize_meta_marks_estimated_only_without_usage(self):
        from agent.metrics import summarize_meta

        meta = summarize_meta(self.make_recorder())
        self.assertEqual(meta["turns"], 2)
        # 只要有一轮带实测 usage，meta 用实测合计、不标估
        self.assertFalse(meta["estimated"])
        self.assertEqual(meta["prompt_tokens"], 100)
        self.assertEqual(meta["completion_tokens"], 20)
        self.assertEqual(meta["estimated_prompt_tokens"], 100)


if __name__ == "__main__":
    unittest.main()
