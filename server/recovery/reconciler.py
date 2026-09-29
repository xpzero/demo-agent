"""Finite background reconciliation of an original Run's business operations.

Only query the stable operation ID. Never retry submission or infer a business
outcome from a transport failure. This worker may be restarted from SQLite facts.
"""
import logging
import time
from collections.abc import Mapping
from threading import Event, Thread

from database import Database
from recovery.adapter import BusinessAdapter, Observation

logger = logging.getLogger(__name__)
POLL_SECONDS = 2
QUERY_SECONDS = 10
PENDING = frozenset({"running", "unknown", "maybe_submitted", "processing", "unconfirmed"})


def _query(adapter: BusinessAdapter, operation: dict, result: dict, finished: Event,
           *, cancel: bool = False):
    try:
        action = adapter.cancel if cancel else adapter.query
        result["observation"] = action(operation["id"], operation["business_operation_id"])
    except Exception as error:
        result["observation"] = Observation("unconfirmed", detail=f"{type(error).__name__}: {error}")
    finally:
        finished.set()


def reconcile_run(database: Database, run_id: int,
                  adapters: Mapping[str, BusinessAdapter] | None = None,
                  *, poll_seconds: float = POLL_SECONDS):
    """Wait for original business facts until confirmed or original deadline.

    A single hung query per operation is left running, never duplicated. Its
    result is ignored after timeout of the Run; late feedback has a separate
    trusted database API and cannot rewrite the old Run's terminal state.
    """
    pending_queries: dict[int, tuple[dict, Event, float]] = {}
    cancelled: set[int] = set()
    adapters = dict(adapters or {})
    while True:
        try:
            run = database.get_run(run_id)
        except Exception:
            # The store vanished (e.g. test teardown deleted the file); stop.
            logger.debug("读取 Run 失败，停止核实线程：run_id=%s", run_id, exc_info=True)
            return
        if run is None or run["status"] not in ("running", "finishing"):
            return
        if run["status"] == "running":
            # An active model worker owns running Runs. Startup changes abandoned
            # Runs to finishing before scheduling this worker.
            return
        deadline = run["deadline_at"] or run["created_at"] + 300
        if time.time() >= deadline:
            database.reconcile_orphan_run(run_id)
            return
        operations = database.list_run_operations(run_id)
        outstanding = [op for op in operations if op["status"] in PENDING or op["conflict"]]
        if not outstanding and operations:
            # Completion after a crash is allowed only if a completed model
            # response was persisted before the worker was lost.
            reason = run["reason"]
            outcome = ("stopped" if run["stop_requested"] else
                       "completed" if reason == "business_unconfirmed" else "failed")
            try:
                database.finish_run(
                    run_id, outcome, reason=reason,
                    assistant_status="finished" if outcome == "completed" and run["assistant_message_id"] else None,
                )
            except Exception:
                logger.exception("业务核实后保存 Run 终态失败：run_id=%s", run_id)
            return
        for op in outstanding:
            if op["status"] not in PENDING or op["conflict"]:
                continue
            adapter = adapters.get(op["name"])
            if adapter is None or not adapter.capabilities.queryable:
                continue
            current = pending_queries.get(op["id"])
            if current is None and run["stop_requested"] and adapter.capabilities.cancellable and op["id"] not in cancelled:
                # Stop first blocks new dispatch; cancellation attempts target the
                # original operation only, and an accepted cancel stays nonfinal.
                cancelled.add(op["id"])
                result = {}
                finished = Event()
                pending_queries[op["id"]] = (result, finished, time.monotonic())
                Thread(target=_query, args=(adapter, op, result, finished), kwargs={"cancel": True},
                       daemon=True, name=f"business-cancel-{op['id']}").start()
                continue
            if current:
                result, finished, started = current
                if finished.is_set():
                    pending_queries.pop(op["id"])
                    observation = result["observation"]
                    try:
                        database.record_confirmed_feedback(
                            op["id"], observation, source=type(adapter).__name__,
                            observed_at=time.time(), business_operation_id=op["business_operation_id"])
                    except Exception:
                        logger.exception("保存业务查询结果失败：operation_id=%s", op["id"])
                elif time.monotonic() - started >= QUERY_SECONDS:
                    # Retain the in-flight query; do not start another one.
                    continue
            else:
                result = {}
                finished = Event()
                pending_queries[op["id"]] = (result, finished, time.monotonic())
                Thread(target=_query, args=(adapter, op, result, finished), daemon=True,
                       name=f"business-query-{op['id']}").start()
        Event().wait(min(poll_seconds, max(0, deadline - time.time())))


def start_reconciliation(database: Database, run_id: int,
                         adapters: Mapping[str, BusinessAdapter] | None = None) -> Thread:
    thread = Thread(target=reconcile_run, args=(database, run_id, adapters),
                    daemon=True, name=f"business-reconcile-{run_id}")
    thread.start()
    return thread
