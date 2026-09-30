"""Contracts for pure current-Session recovery decisions."""

from dataclasses import replace
import unittest

from recovery import (
    OperationFact, PlannedStep, TaskFact, build_task_snapshot, validate_suggestion,
)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.done = OperationFact("op-a", "task", 1, "a", "succeeded", "imported")
        self.unknown = OperationFact("op-b", "task", 1, "b", "unconfirmed", queryable=True)
        self.task = TaskFact(
            "task", "session", "导入甲乙丙", "不可重复导入", 1, "active", "请导入甲乙丙",
            (self.done, self.unknown),
            (PlannedStep("c", "import", '{"name":"丙"}', ("op-b",), True),),
        )
        self.seen = self.snapshot(self.task)

    def snapshot(self, *tasks, state="running", stop=False, budget=True):
        return build_task_snapshot(
            "session", "继续", tuple(tasks), run_state=state,
            stop_requested=stop, budget_available=budget,
        )

    def suggestion(self, action="execute", **changes):
        data = {
            "intent": "resume", "task_id": "task", "task_version": 1,
            "action": action, "operation_refs": [
                {"id": op.id, "status": op.status, "result": op.result, "conflict": op.conflict}
                for op in self.task.operations
            ],
            "step_id": "c", "tool": "import", "args": {"name": "丙"},
        }
        data.update(changes)
        return data

    def test_unknown_never_releases_dependent_step_but_can_query_original(self):
        self.assertEqual(validate_suggestion(self.suggestion(), self.seen, self.seen).outcome, "blocked")
        query = validate_suggestion(
            self.suggestion("verify", operation_id="op-b"), self.seen, self.seen
        )
        self.assertEqual((query.outcome, query.operation_id), ("allow", "op-b"))
        no_query = self.snapshot(replace(self.task, operations=(self.done, replace(self.unknown, queryable=False))))
        self.assertEqual(validate_suggestion(self.suggestion("verify", operation_id="op-b"), no_query, no_query).outcome, "blocked")

    def test_new_reliable_result_skips_verification_but_rejudges_execution(self):
        new_op = replace(self.unknown, status="succeeded", result="imported")
        fresh = self.snapshot(replace(self.task, operations=(self.done, new_op)))
        self.assertEqual(validate_suggestion(self.suggestion("verify", operation_id="op-b"), self.seen, fresh).outcome, "skip")
        self.assertEqual(validate_suggestion(self.suggestion(), self.seen, fresh).outcome, "rejudge")
        new_seen = self.snapshot(replace(self.task, operations=(self.done, new_op)))
        candidate = self.suggestion(operation_refs=[
            {"id": op.id, "status": op.status, "result": op.result, "conflict": op.conflict}
            for op in (self.done, new_op)
        ])
        self.assertEqual(validate_suggestion(candidate, new_seen, new_seen).outcome, "allow")
        duplicate = self.snapshot(replace(self.task, operations=(self.done, new_op, OperationFact("op-c", "task", 1, "c", "maybe_submitted"))))
        candidate["operation_refs"].append({"id": "op-c", "status": "maybe_submitted", "result": None, "conflict": False})
        self.assertEqual(validate_suggestion(candidate, duplicate, duplicate).outcome, "blocked")

    def test_version_candidates_and_session_scope(self):
        changed = self.snapshot(replace(self.task, version=2, goal="新目标"))
        self.assertEqual(validate_suggestion(self.suggestion(), self.seen, changed).outcome, "rejudge")
        other = replace(self.task, id="other", operations=(), steps=())
        ambiguous = self.snapshot(self.task, other)
        self.assertEqual(validate_suggestion(self.suggestion(), ambiguous, ambiguous).outcome, "clarify")
        foreign = replace(self.task, session_id="foreign")
        with self.assertRaises(ValueError):
            self.snapshot(foreign)
        self.assertEqual(validate_suggestion(self.suggestion(task_id="foreign"), self.seen, self.seen).outcome, "invalid")

    def test_stop_budget_conflict_and_missing_claim(self):
        for fresh in (self.snapshot(self.task, state="finishing"), self.snapshot(self.task, stop=True), self.snapshot(self.task, budget=False)):
            self.assertEqual(validate_suggestion(self.suggestion(), self.seen, fresh).outcome, "blocked")
        conflict = self.snapshot(replace(self.task, operations=(self.done, replace(self.unknown, conflict=True))))
        cited_conflict = self.suggestion()
        cited_conflict["operation_refs"][1]["conflict"] = True
        self.assertEqual(validate_suggestion(cited_conflict, conflict, conflict).outcome, "blocked")
        self.assertEqual(validate_suggestion(self.suggestion(operation_refs=[]), self.seen, self.seen).outcome, "blocked")
        # Claims in refs no longer gate validity: the backend facts win, so
        # citing the still-unknown op without verifying is simply blocked.
        self.assertEqual(validate_suggestion(self.suggestion(operation_refs=[{"id": "op-b", "status": "succeeded", "result": "yes", "conflict": False}]), self.seen, self.seen).outcome, "blocked")

    def test_reply_uses_facts_and_invalid_model_actions_do_not_dispatch(self):
        reply = validate_suggestion(self.suggestion("reply"), self.seen, self.seen)
        self.assertEqual(reply.outcome, "allow")
        self.assertEqual(reply.facts, (self.done, self.unknown))
        self.assertEqual(validate_suggestion(self.suggestion("reply", operation_refs=[]), self.seen, self.seen).outcome, "invalid")
        for proposed in (None, "bad", self.suggestion(args={"name": "甲"}), self.suggestion(tool="other"), self.suggestion(task_version=True), self.suggestion(intent=[])):
            self.assertNotEqual(validate_suggestion(proposed, self.seen, self.seen).outcome, "allow")
        self.assertEqual(validate_suggestion(self.suggestion("modify_goal", intent="modify_goal", goal="新目标"), self.seen, self.seen).outcome, "rejudge")

    def test_snapshot_rejects_untrustworthy_or_incomplete_evidence(self):
        for op in (replace(self.unknown, status="succeeded"), replace(self.done, status="unconfirmed"), replace(self.done, task_id="foreign")):
            with self.assertRaises(ValueError):
                self.snapshot(replace(self.task, operations=(op,)))
        with self.assertRaises(ValueError):
            self.snapshot(replace(self.task, steps=(replace(self.task.steps[0], args_json='{"name": "丙"}'),)))


if __name__ == "__main__":
    unittest.main()
