"""Restart and deferred-run reconciliation through business adapters."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("API_KEY", "test-key")

from database import Database
from recovery.adapter import Capabilities, Observation
from recovery.demo_adapter import DemoBusinessAdapter
from recovery.reconciler import reconcile_run


class SlowAdapter(DemoBusinessAdapter):
    TOOL = "slow_business"
    capabilities = Capabilities(queryable=True, cancellable=True, idempotent_submission=True)
    DELAY = 0.2

    def query(self, operation_id, business_operation_id):
        time.sleep(self.DELAY)
        return Observation("succeeded", "慢查询已确认")


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = Database(self.root / "agent.sqlite3")
        self.db.initialize()
        self.session = str(uuid4())

    def _prepare_unfinished_run(self, adapter, value="样品 C"):
        user, run_id = self.db.create_run_turn(self.session, None, "下单", [])
        task = self.db.create_task(run_id, "订单", "active")
        self.db.approve_step(task, "order-1", adapter.TOOL, {"value": value},
                             run_id=run_id, goal_version=1, adapter=adapter)
        op_id = self.db.begin_approved_step(run_id, task, "order-1", "call-1",
                                            adapter.TOOL, {"value": value}, goal_version=1)
        return task, op_id, run_id

    def test_restart_reconciles_business_and_completes_run(self):
        adapter = SlowAdapter(self.root / "business.sqlite3")
        task, op_id, run_id = self._prepare_unfinished_run(adapter)
        # Simulate process loss after durable submission registration.
        self.db.recover_orphan_runs()
        reconcile_run(self.db, run_id, {adapter.TOOL: adapter}, poll_seconds=0.05)
        operation = self.db.list_operations(task)[0]
        self.assertEqual(operation["status"], "succeeded")
        self.assertEqual(operation["confirmed_result"], "慢查询已确认")
        run = self.db.get_run(run_id)
        # Process loss is never relabeled as ordinary success; the confirmed
        # business facts are kept and the Session is released.
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["reason"], "worker_lost_on_restart")
        self.assertIsNone(self.db.get_active_run(self.session))

    def test_unqueryable_operation_times_out_at_original_deadline(self):
        adapter = DemoBusinessAdapter(self.root / "business.sqlite3")
        task, op_id, run_id = self._prepare_unfinished_run(adapter)
        self.db.recover_orphan_runs()
        run = self.db.get_run(run_id)
        # No adapter registered: nothing is queryable; the original deadline governs.
        with self.db.transaction() as conn:
            conn.execute("UPDATE runs SET deadline_at=? WHERE id=?",
                         (run["created_at"] + 0.05, run_id))
        reconcile_run(self.db, run_id, None, poll_seconds=0.05)
        self.assertEqual(self.db.get_run(run_id)["status"], "timed_out")
        self.assertIsNone(self.db.get_active_run(self.session))

    def test_stop_tries_cancellation_then_persists_confirmed_result(self):
        cancelled = []
        class CancelAdapter(DemoBusinessAdapter):
            TOOL = "cancellable_business"
            capabilities = Capabilities(queryable=True, cancellable=True, idempotent_submission=True)
            def cancel(self, operation_id, business_operation_id):
                cancelled.append(operation_id)
                return super().cancel(operation_id, business_operation_id)
        adapter = CancelAdapter(self.root / "business.sqlite3")
        task, op_id, run_id = self._prepare_unfinished_run(adapter)
        # The crash happened after submission; the business side holds the order.
        receipt, _ = adapter.submit(op_id, adapter.TOOL, {"value": "样品 C"})
        self.db.request_stop(self.session)
        # Stop shortens the finishing budget; reconciliation must end within it.
        with self.db.transaction() as conn:
            conn.execute("UPDATE runs SET deadline_at=? WHERE id=?",
                         (time.time() + 2, run_id))
        self.db.recover_orphan_runs()
        reconcile_run(self.db, run_id, {adapter.TOOL: adapter}, poll_seconds=0.05)
        self.assertEqual(cancelled, [op_id])
        operation = self.db.list_operations(task)[0]
        self.assertEqual(operation["status"], "cancelled")
        self.assertEqual(self.db.get_run(run_id)["status"], "stopped")
        self.assertIsNone(self.db.get_active_run(self.session))


if __name__ == "__main__":
    unittest.main()
