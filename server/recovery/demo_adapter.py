"""Local SQLite business service for exercising the recovery contract.

This ledger is separate from the Agent database. The operation ID is the stable
submission key; querying it never performs another submission. Demo use only.
"""
import sqlite3
from pathlib import Path

from .adapter import Capabilities, Observation


class DemoBusinessAdapter:
    TOOL = "demo_business_order"
    capabilities = Capabilities(queryable=True, cancellable=True, idempotent_submission=True)

    def __init__(self, path: str | Path):
        self.path = str(path)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS demo_orders (
                operation_id INTEGER PRIMARY KEY, value TEXT NOT NULL,
                status TEXT NOT NULL, submit_count INTEGER NOT NULL DEFAULT 1
            )""")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def approve(self, task_id: int, goal_version: int, step_id: str, tool: str,
                args: dict, dependencies: tuple[int, ...]) -> bool:
        return (task_id > 0 and goal_version > 0 and bool(step_id)
                and tool == self.TOOL and isinstance(args, dict)
                and set(args) == {"value"} and isinstance(args["value"], str)
                and bool(args["value"].strip()) and not dependencies)

    def submit(self, operation_id: int, tool: str, args: dict) -> tuple[str, Observation]:
        if tool != self.TOOL or not isinstance(args, dict) or set(args) != {"value"}:
            raise ValueError("invalid demo business order")
        value = args["value"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("invalid demo order value")
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO demo_orders(operation_id,value,status) VALUES (?,?,'succeeded')",
                       (operation_id, value))
            row = db.execute("SELECT value FROM demo_orders WHERE operation_id=?", (operation_id,)).fetchone()
            if row[0] != value:
                raise ValueError("operation key belongs to a different order")
        receipt = f"demo-order-{operation_id}"
        # Submission only acknowledges receipt. The separate query supplies
        # authoritative business evidence to the Agent.
        return receipt, Observation("processing", detail="订单已受理，等待可靠查询")

    def query(self, operation_id: int, business_operation_id: str | None) -> Observation:
        if business_operation_id is not None and business_operation_id != f"demo-order-{operation_id}":
            return Observation("unconfirmed", detail="receipt mismatch")
        with self._connect() as db:
            row = db.execute("SELECT value,status FROM demo_orders WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            return Observation("unconfirmed", detail="operation absent; submission cannot be ruled out")
        if row[1] == "cancelled":
            return Observation("cancelled", "订单已取消")
        return Observation("succeeded", f"订单已确认：{row[0]}")

    def cancel(self, operation_id: int, business_operation_id: str | None) -> Observation:
        if business_operation_id is not None and business_operation_id != f"demo-order-{operation_id}":
            return Observation("unconfirmed", detail="receipt mismatch")
        with self._connect() as db:
            db.execute("UPDATE demo_orders SET status='cancelled' WHERE operation_id=? AND status='succeeded'",
                       (operation_id,))
        return self.query(operation_id, business_operation_id)
