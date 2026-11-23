"""Stage machine (PLAN B4): `advance()` and the definitions it runs.

A pipeline is a set of stages and a set of steps:
- a **stage** has `on_enter(sc)`, which queues tasks with INSERT OR IGNORE (unique keys), and
  `on_complete(sc)`, which runs once no task of the investigation is queued or running and returns the next
  stage name, `STAY` (after queuing more work), `Wait(...)`, `Complete(...)` or `Fail(...)`;
- a **step** executes one task (see runner.py) and returns a commit function, so a task's writes and its
  completion happen in one transaction.

`advance` runs after every task, every user action, and at startup, under a per-investigation asyncio.Lock.
Stage changes use compare-and-set. A stage with nothing to do completes immediately.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

from . import budgets
from .db import ResearchDB, _decode, now, parse_time

STAY = "__stay__"
MAX_TRANSITIONS = 200  # per advance() call; a loop beyond this is a bug


@dataclass
class Wait:
    waiting_for: Literal["problem_rewrite", "answers", "safety_ack", "plan_approval", "selection_approval"]
    message: str = ""


@dataclass
class Complete:
    outcome: Literal["findings", "insufficient_evidence", "incomplete", "safety_stop"]
    reason: str = ""


@dataclass
class Fail:
    error: str


Decision = str | Wait | Complete | Fail


@dataclass
class StageContext:
    """What a stage hook sees. `conn` is an open write transaction."""
    db: ResearchDB
    conn: sqlite3.Connection
    inv: dict
    settings: dict
    guards: budgets.GuardState
    queued: int = 0

    @property
    def id(self) -> int:
        return self.inv["id"]

    def queue(self, step: str, key: str, payload: dict | None = None, *, priority: int = 100,
              max_attempts: int = 4) -> bool:
        """Queue a task unless its key exists. Acquisition steps are not started after an acquisition stop."""
        if self.guards.acquisition_stop and step in budgets.ACQUISITION_STEPS:
            return False
        added = self.db.queue_task(self.conn, self.id, step, key, payload, priority=priority, max_attempts=max_attempts)
        if added:
            self.queued += 1
        return added

    def tasks(self, step: str | None = None, statuses: tuple[str, ...] | None = None) -> list[dict]:
        sql = "SELECT * FROM research_tasks WHERE investigation_id = ?"
        params: list[Any] = [self.id]
        if step:
            sql += " AND step = ?"
            params.append(step)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params += list(statuses)
        return [_decode(r) for r in self.conn.execute(sql + " ORDER BY id", params)]

    def event(self, kind: str, message: str = "", *, level: str = "info", data: Any = None) -> None:
        self.db.event(self.id, kind, message, level=level, stage=self.inv["stage"], data=data, conn=self.conn)

    def update(self, **values: Any) -> None:
        self.db.update_investigation(self.id, conn=self.conn, **values)
        self.inv.update(values)


Hook = Callable[[StageContext], None]
CompleteHook = Callable[[StageContext], Decision]


@dataclass
class StageDef:
    name: str
    on_enter: Hook = lambda sc: None
    on_complete: CompleteHook = lambda sc: STAY
    skip_check_steps: tuple[str, ...] = ()  # steps counted for the "more than half skipped → failed" rule


@dataclass
class StepDef:
    """`run(tc)` returns an optional commit function `fn(conn)`. `on_fail` is the B10 policy:
    `fail` (investigation fails), `skip` (task skipped, logged), or a callable `fn(conn, task, error)` that
    records the failure (e.g. a grounding gap) and lets the task count as skipped."""
    name: str
    run: Callable[[Any], Awaitable[Callable[[sqlite3.Connection], None] | None]]
    on_fail: str | Callable[[sqlite3.Connection, dict, str], None] = "fail"
    code_step: bool = False


@dataclass
class Pipeline:
    stages: dict[str, StageDef]
    steps: dict[str, StepDef]
    first_stage: str = "intake"
    hard_stop_stage: str | None = None  # where a hard stop goes (finalizing); None → Complete(incomplete)
    terminal_stages: frozenset[str] = frozenset()

    def stage(self, name: str) -> StageDef:
        if name not in self.stages:
            raise KeyError(f"Unknown stage {name!r}.")
        return self.stages[name]


def _elapsed(since: str | None) -> float:
    start = parse_time(since)
    if start is None:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - start).total_seconds())


def status_values(inv: dict, status: str) -> dict[str, Any]:
    """Column updates for a status change, keeping `active_seconds` free of waiting and paused time."""
    values: dict[str, Any] = {"status": status}
    was_active = inv.get("status") == "active"
    if was_active and status != "active":
        values["active_seconds"] = float(inv.get("active_seconds") or 0) + _elapsed(inv.get("active_since"))
        values["active_since"] = None
    elif not was_active and status == "active":
        values["active_since"] = now()
    return values


class Machine:
    """Owns the per-investigation locks and runs `advance`."""

    def __init__(self, db: ResearchDB, pipeline: Pipeline, *, on_change: Callable[[], None] | None = None):
        self.db = db
        self.pipeline = pipeline
        self._locks: dict[int, asyncio.Lock] = {}
        self.on_change = on_change or (lambda: None)

    def lock(self, investigation_id: int) -> asyncio.Lock:
        if investigation_id not in self._locks:
            self._locks[investigation_id] = asyncio.Lock()
        return self._locks[investigation_id]

    async def advance(self, investigation_id: int) -> None:
        async with self.lock(investigation_id):
            await asyncio.to_thread(self._advance_sync, investigation_id)
        self.on_change()

    # The body is synchronous (SQLite); it runs in a worker thread under the asyncio lock.
    def _advance_sync(self, investigation_id: int) -> None:
        for _ in range(MAX_TRANSITIONS):
            with self.db.tx() as conn:
                row = conn.execute("SELECT * FROM investigations WHERE id = ?", (investigation_id,)).fetchone()
                if row is None:
                    return
                inv = _decode(row)
                if inv["status"] != "active":
                    return
                if self.db.open_task_count(investigation_id, conn):
                    return
                settings = inv.get("settings_snapshot") or {}
                guards = budgets.evaluate(self.db, inv, settings.get("guards") or {})
                sc = StageContext(self.db, conn, inv, settings, guards)

                if guards.hard_stop and not inv.get("stop_reason"):
                    sc.update(stop_reason=guards.hard_stop)
                    sc.event("guard", guards.hard_stop, level="warning")
                    target = self.pipeline.hard_stop_stage
                    if target and inv["stage"] != target:
                        self.db.set_stage(investigation_id, inv["stage"], target, conn)
                        continue
                    if not target:
                        self._finish(sc, Complete("incomplete", guards.hard_stop))
                        return
                if guards.acquisition_stop and not inv.get("stop_reason"):
                    sc.update(stop_reason=guards.acquisition_stop)
                    sc.event("guard", guards.acquisition_stop, level="warning")

                stage = self.pipeline.stage(inv["stage"])
                failed = self._skip_rule(sc, stage)
                if failed:
                    self._finish(sc, Fail(failed))
                    return
                stage.on_enter(sc)
                if sc.queued:
                    return
                decision = stage.on_complete(sc)
                if decision == STAY:
                    if sc.queued:
                        return
                    self._finish(sc, Fail(f"Stage {inv['stage']} stayed without queuing work."))
                    return
                if isinstance(decision, str):
                    self.pipeline.stage(decision)
                    if not self.db.set_stage(investigation_id, inv["stage"], decision, conn):
                        return  # someone else moved it; they will advance
                    self.db.event(investigation_id, "stage", decision, stage=decision, conn=conn)
                    continue
                self._finish(sc, decision)
                return
        raise RuntimeError(f"Investigation {investigation_id}: more than {MAX_TRANSITIONS} stage transitions.")

    def _skip_rule(self, sc: StageContext, stage: StageDef) -> str | None:
        """B10: if more than half of a stage's tasks are skipped, the investigation fails."""
        for step in stage.skip_check_steps:
            rows = sc.tasks(step)
            if not rows:
                continue
            skipped = sum(1 for r in rows if r["status"] == "skipped")
            if skipped * 2 > len(rows):
                return f"More than half of the {step} tasks were skipped ({skipped} of {len(rows)})."
        return None

    def _finish(self, sc: StageContext, decision: Decision) -> None:
        if isinstance(decision, Wait):
            sc.update(**status_values(sc.inv, "waiting_user"), waiting_for=decision.waiting_for)
            sc.event("waiting", decision.waiting_for)
        elif isinstance(decision, Complete):
            sc.update(**status_values(sc.inv, "complete"), outcome=decision.outcome, waiting_for=None)
            sc.event("complete", decision.outcome, data={"reason": decision.reason} if decision.reason else None)
        elif isinstance(decision, Fail):
            sc.update(**status_values(sc.inv, "failed"), error=decision.error[:2000])
            sc.event("failed", decision.error[:2000], level="error")

    # ---------- user controls (B11) ----------

    async def set_status(self, investigation_id: int, status: str, *, expect: tuple[str, ...], **extra: Any) -> bool:
        async with self.lock(investigation_id):
            def run() -> bool:
                with self.db.tx() as conn:
                    inv = _decode(conn.execute("SELECT * FROM investigations WHERE id = ?",
                                               (investigation_id,)).fetchone())
                    if inv is None or inv["status"] not in expect:
                        return False
                    self.db.update_investigation(investigation_id, conn=conn, **status_values(inv, status), **extra)
                    self.db.event(investigation_id, "status", status, stage=inv["stage"], conn=conn)
                    if status == "cancelled":
                        conn.execute("UPDATE research_tasks SET status = 'cancelled', finished_at = ? "
                                     "WHERE investigation_id = ? AND status = 'queued'", (now(), investigation_id))
                    return True
            changed = await asyncio.to_thread(run)
        if changed and status == "active":
            await self.advance(investigation_id)
        return changed

    async def pause(self, investigation_id: int) -> bool:
        return await self.set_status(investigation_id, "paused", expect=("active",))

    async def resume(self, investigation_id: int) -> bool:
        return await self.set_status(investigation_id, "active", expect=("paused",))

    async def cancel(self, investigation_id: int) -> bool:
        return await self.set_status(investigation_id, "cancelled", expect=("active", "paused", "waiting_user", "failed"))

    async def retry(self, investigation_id: int) -> bool:
        """A failed investigation: re-queue its failed tasks and continue from the same stage."""
        def requeue() -> None:
            with self.db.tx() as conn:
                conn.execute("UPDATE research_tasks SET status = 'queued', attempts = 0, error = NULL, error_kind = NULL, "
                             "not_before = NULL, finished_at = NULL WHERE investigation_id = ? AND status = 'failed'",
                             (investigation_id,))
        inv = self.db.investigation(investigation_id)
        if inv is None or inv["status"] != "failed":
            return False
        await asyncio.to_thread(requeue)
        return await self.set_status(investigation_id, "active", expect=("failed",), error=None)

    async def retry_task(self, investigation_id: int, task_id: int) -> bool:
        def requeue() -> bool:
            with self.db.tx() as conn:
                return conn.execute(
                    "UPDATE research_tasks SET status = 'queued', attempts = 0, error = NULL, error_kind = NULL, "
                    "skip_reason = NULL, not_before = NULL, finished_at = NULL "
                    "WHERE id = ? AND investigation_id = ? AND status IN ('failed','skipped')",
                    (task_id, investigation_id)).rowcount == 1
        done = await asyncio.to_thread(requeue)
        if done:
            inv = self.db.investigation(investigation_id)
            if inv and inv["status"] == "failed":
                await self.set_status(investigation_id, "active", expect=("failed",), error=None)
            else:
                await self.advance(investigation_id)
        return done

    async def resolve_wait(self, investigation_id: int, waiting_for: str, apply: Callable[[StageContext], str | None]) -> bool:
        """Apply a user action for a waiting state. `apply(sc)` writes the user's input and returns the next
        stage (or None to stay waiting). Then the investigation becomes active and advances."""
        async with self.lock(investigation_id):
            def run() -> bool:
                with self.db.tx() as conn:
                    inv = _decode(conn.execute("SELECT * FROM investigations WHERE id = ?",
                                               (investigation_id,)).fetchone())
                    if inv is None or inv["status"] != "waiting_user" or inv["waiting_for"] != waiting_for:
                        return False
                    settings = inv.get("settings_snapshot") or {}
                    sc = StageContext(self.db, conn, inv, settings,
                                      budgets.GuardState(None, None, {}))
                    nxt = apply(sc)
                    if nxt is None:
                        return True
                    self.pipeline.stage(nxt)
                    self.db.update_investigation(investigation_id, conn=conn, **status_values(inv, "active"),
                                                 waiting_for=None, stage=nxt)
                    self.db.event(investigation_id, "resolved", waiting_for, stage=nxt, conn=conn)
                    return True
            ok = await asyncio.to_thread(run)
        if ok:
            await self.advance(investigation_id)
        return ok
