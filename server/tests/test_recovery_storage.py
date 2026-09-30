"""Durable recovery facts and dispatch boundaries."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import Mock

from database import Database, StoreError
from recovery.adapter import Observation


class RecoveryStorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(Path(temp.name) / 'state.sqlite3')
        self.db.initialize()
        self.session = str(uuid4())
        self.user, self.run_id = self.db.create_run_turn(self.session, None, 'send', [])
        self.task = self.db.create_task(self.run_id, 'send', 'active')

    def approve(self, step='first', dependencies=()):
        self.db.approve_step(self.task, step, 'send', {'value': 1}, depends_on=dependencies,
                             run_id=self.run_id, goal_version=1, adapter=Mock(approve=Mock(return_value=True)))

    def begin(self, step='first', call='call-1', args=None):
        return self.db.begin_approved_step(self.run_id, self.task, step, call, 'send',
                                           {'value': 1} if args is None else args, goal_version=1)

    def test_approved_dispatch_requires_exact_args_and_no_replay(self):
        self.approve()
        self.assertEqual(len(self.db.list_planned_steps(self.task)), 1)
        with self.assertRaises(StoreError) as mismatch:
            self.begin(args={'value': 2})
        self.assertEqual(mismatch.exception.code, 'step_mismatch')
        self.assertEqual(self.db.list_operations(self.task), [])
        op = self.begin()
        self.assertEqual(self.db.list_operations(self.task)[0]['status'], 'maybe_submitted')
        with self.assertRaises(StoreError):
            self.begin(call='fresh-call')
        with self.assertRaises(StoreError):
            self.db.finish_run(self.run_id, 'completed')
        self.assertEqual(self.db.defer_uncertain_run(self.run_id, 'business_wait'),
                         {'status': 'finishing', 'next_check_at': self.db.get_run(self.run_id)['deadline_at']})
        self.assertTrue(self.db.get_session_status(self.session)['processing'])
        with self.assertRaises(StoreError):
            self.db.create_run_turn(self.session, self.user, 'again', [])
        with self.assertRaises(StoreError):
            self.begin(call='later')
        self.assertEqual(self.db.list_operations(self.task)[0]['id'], op)

    def test_adapter_rejection_and_unconfirmed_dependency(self):
        with self.assertRaises(StoreError) as rejected:
            self.db.approve_step(self.task, 'rejected', 'send', {}, run_id=self.run_id,
                                 goal_version=1, adapter=Mock(approve=Mock(return_value=False)))
        self.assertEqual(rejected.exception.code, 'step_not_approved')
        self.assertEqual(self.db.list_planned_steps(self.task), [])
        self.approve()
        first = self.begin()
        self.approve('dependent', (first,))
        with self.assertRaises(StoreError):
            self.begin('dependent', 'call-2')
        self.db.record_confirmed_feedback(first, Observation('succeeded', 'receipt'),
                                          source='query', observed_at=time.time())
        second = self.begin('dependent', 'call-2')
        self.assertEqual(self.db.list_operations(self.task)[-1]['id'], second)

    def test_stop_blocks_dispatch_and_goal_change_invalidates_approval(self):
        self.approve()
        self.db.request_stop(self.session)
        with self.assertRaises(StoreError):
            self.begin()
        self.db.finish_run(self.run_id, 'stopped')
        next_user, next_run = self.db.create_run_turn(self.session, self.user, 'change', [])
        self.db.attach_task(next_run, self.task)
        self.assertEqual(self.db.adopt_goal(self.task, 'send differently', 'only once',
                         expected_version=1, run_id=next_run, message_id=next_user), 2)
        history = json.loads(self.db.list_tasks(next_run)[0]['goal_history'])
        self.assertEqual(history[-1]['name'], 'send')
        self.assertEqual(history[-1]['message_id'], next_user)
        with self.assertRaises(StoreError):
            self.db.begin_approved_step(next_run, self.task, 'first', 'new', 'send', {'value': 1}, goal_version=2)

    def test_feedback_query_cancel_conflict_and_late_result(self):
        self.approve()
        op = self.begin()
        self.assertEqual(self.db.record_confirmed_feedback(op, Observation('processing', detail='cancel accepted'),
                         source='cancel', observed_at=1, business_operation_id='biz-1')['status'], 'processing')
        self.assertEqual(self.db.record_confirmed_feedback(op, Observation('unconfirmed', detail='query unavailable'),
                         source='query', observed_at=2)['status'], 'unconfirmed')
        with self.assertRaises(StoreError):
            self.db.finish_run(self.run_id, 'failed')
        self.db.defer_uncertain_run(self.run_id, 'waiting')
        with self.db.transaction() as db:
            db.execute('UPDATE runs SET deadline_at=? WHERE id=?', (time.time() - 1, self.run_id))
        self.assertTrue(self.db.finish_run(self.run_id, 'timed_out'))
        conclusion = self.db.get_run(self.run_id)
        fact = self.db.record_confirmed_feedback(op, Observation('succeeded', 'receipt'),
                                                  source='query', observed_at=3)
        self.assertEqual(fact['status'], 'succeeded')
        self.assertEqual(self.db.get_run(self.run_id), conclusion)
        fact = self.db.record_confirmed_feedback(op, Observation('cancelled', 'no effect'),
                                                  source='cancel', observed_at=4)
        self.assertTrue(fact['conflict'])
        self.assertEqual(fact['confirmed_result'], 'receipt')
        self.assertEqual([r['status'] for r in self.db.list_operation_observations(op)],
                         ['processing', 'unconfirmed', 'succeeded', 'cancelled'])
        self.assertEqual(self.db.list_operations(self.task)[0]['business_operation_id'], 'biz-1')

    def test_clarification_and_modification_preserve_original_goal(self):
        self.db.save_goal_modification(self.task, self.run_id, self.user, 'change it')
        task = self.db.list_tasks(self.run_id)[0]
        self.assertEqual(task['name'], 'send')
        self.assertEqual(json.loads(task['pending_modification'])['message_id'], self.user)
        index = self.db.add_clarification(self.task, self.run_id, self.user, 'which address?')
        self.db.finish_run(self.run_id, 'completed')
        answer, followup = self.db.create_run_turn(self.session, self.user, 'address A', [])
        self.db.resolve_clarification(self.task, index, answer)
        questions = json.loads(self.db.list_tasks(self.run_id)[0]['pending_clarifications'])
        self.assertEqual(questions[0]['answer_message_id'], answer)
        self.assertEqual(questions[0]['goal_version'], 1)
        self.db.attach_task(followup, self.task)

    def test_handler_success_is_not_business_evidence(self):
        op = self.db.begin_tool_operation(self.run_id, 'legacy', 'send', {}, task_id=self.task, goal_version=1)[0]
        self.db.finish_tool_operation(op, 'transport success', True, business=True)
        fact = self.db.list_operations(self.task)[0]
        self.assertEqual((fact['status'], fact['reliable']), ('unknown', 0))
        self.assertTrue(self.db.defer_uncertain_run(self.run_id, 'needs_query'))


if __name__ == '__main__':
    unittest.main()
