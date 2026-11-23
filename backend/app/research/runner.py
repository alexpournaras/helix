"""Background worker (PLAN B11).

- Single instance: an OS file lock (lock.py). Without the lock the worker does not run.
- Recovery at startup: re-queue tasks left `running` (restarts += 1, limit 20), then advance every active
  investigation.
- Loop: claim tasks by priority then ID, at most `max_parallel` at once. Each task's writes and its completion
  happen in one transaction; then `advance` runs.
- Errors: transient → back off 5 s, 30 s, 2 m, 10 m (4 attempts); format → one more attempt; anything else →
  the step's B10 policy. Code steps that raise fail the investigation (a bug; the traceback is logged).
- Graceful shutdown: re-queue running tasks without counting a restart.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .db import ResearchDB, now
from .lock import InstanceLock, LockHeld
from .pipeline import Fail, Machine, Pipeline, status_values
from .providers.base import ModelError

log = logging.getLogger("helix.research")

BACKOFF_SECONDS = (5, 30, 120, 600)
MAX_RESTARTS = 20
FORMAT_EXTRA_ATTEMPTS = 1


@dataclass
class TaskContext:
    """Passed to a step's `run`. `services` holds whatever the steps need (model context, sources, …)."""
    db: ResearchDB
    task: dict
    investigation: dict
    services: Any

    @property
    def payload(self) -> dict:
        return self.task.get("payload") or {}

    @property
    def settings(self) -> dict:
        return self.investigation.get("settings_snapshot") or {}


class Worker:
    def __init__(self, db: ResearchDB, pipeline: Pipeline, *, lock: InstanceLock | None = None,
                 services: Any = None, max_parallel: int = 8, poll_seconds: float = 1.0,
                 clock: Callable[[], datetime] | None = None):
        self.db = db
        self.pipeline = pipeline
        self.lock = lock
        self.services = services
        self.max_parallel = max_parallel
        self.poll_seconds = poll_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.machine = Machine(db, pipeline, on_change=self.wake)
        self._wake = asyncio.Event()
        self._running: dict[int, asyncio.Task] = {}
        self._loop_task: asyncio.Task | None = None
        self._stopping = False
        self.enabled = False

    # ---------- lifecycle ----------

    async def start(self) -> bool:
        if self.lock is not None:
            try:
                self.lock.acquire()
            except LockHeld as exc:
                log.warning("Research worker not started: %s", exc)
                return False
        self.enabled = True
        await self.recover()
        self._loop_task = asyncio.create_task(self._loop())
        return True

    async def recover(self) -> None:
        requeued = await asyncio.to_thread(self.db.requeue_running, count_restart=True)
        if requeued:
            await asyncio.to_thread(self._enforce_restart_limit)
        for inv in await asyncio.to_thread(self.db.query, "SELECT id FROM investigations WHERE status = 'active'"):
            await self.machine.advance(inv["id"])

    def _enforce_restart_limit(self) -> None:
        with self.db.tx() as conn:
            rows = conn.execute("SELECT * FROM research_tasks WHERE status = 'queued' AND restarts > ?",
                                (MAX_RESTARTS,)).fetchall()
        for row in rows:
            task = dict(row)
            self._apply_failure(task, "restarts", f"Task restarted more than {MAX_RESTARTS} times.")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for task in list(self._running.values()):
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
        await asyncio.to_thread(self.db.requeue_running, count_restart=False)
        if self.lock is not None:
            self.lock.release()
        self.enabled = False

    def wake(self) -> None:
        self._wake.set()

    # ---------- loop ----------

    async def _loop(self) -> None:
        while not self._stopping:
            self._wake.clear()
            await self.fill()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def fill(self) -> int:
        """Claim and start tasks up to the parallel limit. Returns how many were started."""
        started = 0
        while len(self._running) < self.max_parallel and not self._stopping:
            task = await asyncio.to_thread(self.db.claim_task)
            if task is None:
                break
            self._running[task["id"]] = asyncio.create_task(self._run(task))
            started += 1
        return started

    async def run_until_idle(self, timeout: float = 30.0) -> None:
        """Test and eval helper: run tasks until nothing is runnable (not_before in the future counts as idle)."""
        async def drive() -> None:
            while True:
                started = await self.fill()
                if not self._running:
                    # A task can finish while fill() is still claiming; only stop when a fill finds nothing.
                    if started == 0:
                        return
                    continue
                await asyncio.wait(list(self._running.values()), return_when=asyncio.FIRST_COMPLETED)
        await asyncio.wait_for(drive(), timeout=timeout)

    async def _run(self, task: dict) -> None:
        try:
            await self.execute(task)
        finally:
            self._running.pop(task["id"], None)
            self.wake()

    async def execute(self, task: dict) -> None:
        inv = await asyncio.to_thread(self.db.investigation, task["investigation_id"])
        step = self.pipeline.steps.get(task["step"])
        if inv is None:
            return
        if step is None:
            await asyncio.to_thread(self._apply_failure, task, "bug", f"No step handler for {task['step']!r}.",
                                    force_fail=True)
            await self.machine.advance(task["investigation_id"])
            return
        tc = TaskContext(self.db, task, inv, self.services)
        try:
            commit = await step.run(tc)
        except asyncio.CancelledError:
            raise
        except ModelError as exc:
            await asyncio.to_thread(self._model_error, task, exc)
        except Exception as exc:  # noqa: BLE001 — a bug in the step; record it per B10
            trace = traceback.format_exc(limit=8)
            kind = "bug" if step.code_step else "error"
            await asyncio.to_thread(self._apply_failure, task, kind, f"{exc.__class__.__name__}: {exc}\n{trace}",
                                    force_fail=step.code_step)
        else:
            try:
                await asyncio.to_thread(self._commit, task, commit)
            except Exception as exc:  # noqa: BLE001
                trace = traceback.format_exc(limit=8)
                await asyncio.to_thread(self._apply_failure, task, "bug", f"{exc.__class__.__name__}: {exc}\n{trace}",
                                        force_fail=True)
        await self.machine.advance(task["investigation_id"])

    def _commit(self, task: dict, commit: Callable[[sqlite3.Connection], None] | None) -> None:
        with self.db.tx() as conn:
            current = conn.execute("SELECT status FROM research_tasks WHERE id = ?", (task["id"],)).fetchone()
            if current is None or current["status"] != "running":
                return  # cancelled or re-queued meanwhile; drop the result (the model cache keeps the calls)
            if commit is not None:
                commit(conn)
            self.db.finish_task(conn, task["id"], "done", error=None, error_kind=None)

    # ---------- failures ----------

    def _model_error(self, task: dict, exc: ModelError) -> None:
        attempts = task["attempts"]
        if exc.kind == "transient" and attempts < task["max_attempts"]:
            delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
            if exc.retry_after:
                delay = max(delay, exc.retry_after)
            self._requeue(task, delay, exc)
            return
        if exc.kind == "format" and attempts < 1 + FORMAT_EXTRA_ATTEMPTS:
            self._requeue(task, 0, exc)
            return
        self._apply_failure(task, exc.kind, exc.message)

    def _requeue(self, task: dict, delay: float, exc: ModelError) -> None:
        not_before = (self.clock() + timedelta(seconds=delay)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with self.db.tx() as conn:
            conn.execute("UPDATE research_tasks SET status = 'queued', not_before = ?, error = ?, error_kind = ?, "
                         "started_at = NULL WHERE id = ? AND status IN ('running','queued')",
                         (not_before, exc.message[:1000], exc.kind, task["id"]))

    def _apply_failure(self, task: dict, kind: str, message: str, *, force_fail: bool = False) -> None:
        step = self.pipeline.steps.get(task["step"])
        policy = "fail" if force_fail or step is None else step.on_fail
        with self.db.tx() as conn:
            inv = conn.execute("SELECT * FROM investigations WHERE id = ?", (task["investigation_id"],)).fetchone()
            if inv is None:
                return
            if policy == "fail":
                self.db.finish_task(conn, task["id"], "failed", error=message[:4000], error_kind=kind)
                if inv["status"] in ("active", "paused"):
                    self.db.update_investigation(task["investigation_id"], conn=conn,
                                                 **status_values(dict(inv), "failed"),
                                                 error=f"{task['step']} failed: {message.splitlines()[0][:500]}")
                    self.db.event(task["investigation_id"], "failed", f"{task['step']}: {kind}", level="error",
                                  stage=inv["stage"], data={"task_id": task["id"]}, conn=conn)
                return
            if callable(policy):
                policy(conn, task, message)
            self.db.finish_task(conn, task["id"], "skipped", error=message[:4000], error_kind=kind,
                                skip_reason=kind)
            self.db.event(task["investigation_id"], "task_skipped", f"{task['step']}: {kind}", level="warning",
                          stage=inv["stage"], data={"task_id": task["id"]}, conn=conn)


__all__ = ["Fail", "TaskContext", "Worker"]
