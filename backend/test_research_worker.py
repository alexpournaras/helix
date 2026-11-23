import asyncio
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone

from research_testkit import ResearchTestCase

from app.research.db import now
from app.research.lock import InstanceLock, LockHeld
from app.research.pipeline import STAY, Complete, Pipeline, StageDef, StepDef, Wait, status_values
from app.research.providers import ModelError
from app.research.runner import Worker


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Dummy:
    """intake → work (2 tasks) → empty → review loop (max_iterations) → complete(findings)."""

    def __init__(self):
        self.runs: dict[str, int] = {}
        self.fail_first: dict[str, ModelError] = {}
        self.wait_after_work = False

    def pipeline(self) -> Pipeline:
        async def run_a(tc):
            key = tc.task["key"]
            self.runs[key] = self.runs.get(key, 0) + 1
            if key in self.fail_first:
                raise self.fail_first.pop(key)

            def commit(conn):
                tc.db.event(tc.task["investigation_id"], "did", key, conn=conn)
            return commit

        async def run_code(tc):
            raise RuntimeError("bug in code step")

        def work_enter(sc):
            sc.queue("a", "a:1")
            sc.queue("a", "a:2")
            sc.queue("search", "search:1")  # an acquisition step

        def work_done(sc):
            return Wait("plan_approval") if self.wait_after_work else "empty"

        def review_enter(sc):
            sc.queue("a", f"review:{sc.inv['review_round']}")

        def review_done(sc):
            max_it = sc.settings.get("review", {}).get("max_iterations", 3)
            if sc.inv["review_round"] + 1 < max_it:
                sc.update(review_round=sc.inv["review_round"] + 1)
                review_enter(sc)
                return STAY
            return "done"

        stages = {
            "intake": StageDef("intake", on_complete=lambda sc: "work"),
            "work": StageDef("work", on_enter=work_enter, on_complete=work_done, skip_check_steps=("a",)),
            "empty": StageDef("empty", on_complete=lambda sc: "review"),
            "review": StageDef("review", on_enter=review_enter, on_complete=review_done),
            "done": StageDef("done", on_complete=lambda sc: Complete("findings")),
            "codefail": StageDef("codefail", on_enter=lambda sc: sc.queue("code", "code:1"),
                                 on_complete=lambda sc: "done"),
        }
        steps = {"a": StepDef("a", run_a, on_fail="skip"), "search": StepDef("search", run_a, on_fail="skip"),
                 "code": StepDef("code", run_code, code_step=True)}
        return Pipeline(stages=stages, steps=steps)


class WorkerTests(ResearchTestCase):
    def setUp(self):
        super().setUp()
        self.dummy = Dummy()
        self.worker = Worker(self.db, self.dummy.pipeline(), max_parallel=4)

    def new(self, **settings) -> int:
        snap = {"review": {"max_iterations": 3}, "guards": {}, **settings}
        return self.db.create_investigation("Synthetic.", profile="quality", settings_snapshot=snap, synthetic=True)

    async def run_all(self, inv):
        await self.worker.machine.advance(inv)
        await self.worker.run_until_idle()

    async def test_completes(self):
        inv = self.new()
        await self.run_all(inv)
        row = self.db.investigation(inv)
        self.assertEqual((row["status"], row["outcome"], row["stage"]), ("complete", "findings", "done"))
        self.assertEqual(self.dummy.runs["a:1"], 1)
        self.assertEqual(self.db.open_task_count(inv), 0)

    async def test_review_loop_stops_at_max_iterations(self):
        inv = self.new(review={"max_iterations": 3})
        await self.run_all(inv)
        self.assertEqual(sorted(k for k in self.dummy.runs if k.startswith("review")),
                         ["review:0", "review:1", "review:2"])
        self.assertEqual(self.db.investigation(inv)["review_round"], 2)

    async def test_empty_stages_pass_through(self):
        inv = self.new()
        await self.run_all(inv)
        stages = [e["message"] for e in self.db.query(
            "SELECT message FROM events WHERE investigation_id = ? AND kind = 'stage' ORDER BY id", (inv,))]
        self.assertEqual(stages, ["work", "empty", "review", "done"])

    async def test_hard_kill_resumes_once(self):
        inv = self.new()
        await self.worker.machine.advance(inv)
        killed = self.db.claim_task()  # a task was running when the process died
        self.assertEqual(self.db.task(killed["id"])["status"], "running")
        fresh = Worker(self.db, self.dummy.pipeline(), max_parallel=4)
        await fresh.recover()
        task = self.db.task(killed["id"])
        self.assertEqual((task["status"], task["restarts"]), ("queued", 1))
        await fresh.run_until_idle()
        self.assertEqual(self.dummy.runs[killed["key"]], 1)
        done = self.db.query("SELECT * FROM events WHERE investigation_id = ? AND kind = 'did' AND message = ?",
                             (inv, killed["key"]))
        self.assertEqual(len(done), 1)
        self.assertEqual(self.db.investigation(inv)["status"], "complete")

    async def test_restart_limit(self):
        inv = self.new()
        await self.worker.machine.advance(inv)
        task = self.db.claim_task()
        with self.db.tx() as conn:
            conn.execute("UPDATE research_tasks SET restarts = 20 WHERE id = ?", (task["id"],))
        await Worker(self.db, self.dummy.pipeline()).recover()
        self.assertEqual(self.db.task(task["id"])["status"], "skipped")  # step policy: skip
        self.assertEqual(self.db.task(task["id"])["error_kind"], "restarts")

    async def test_transient_backoff_sets_not_before(self):
        inv = self.new()
        self.dummy.fail_first["a:1"] = ModelError("transient", "busy")
        before = datetime.now(timezone.utc)
        await self.run_all(inv)
        task = [t for t in self.db.tasks(inv) if t["key"] == "a:1"][0]
        self.assertEqual((task["status"], task["attempts"], task["error_kind"]), ("queued", 1, "transient"))
        delay = (datetime.fromisoformat(task["not_before"].replace("Z", "+00:00")) - before).total_seconds()
        self.assertTrue(4 <= delay <= 7, delay)
        self.assertEqual(self.db.investigation(inv)["stage"], "work")  # waits for the retry
        with self.db.tx() as conn:
            conn.execute("UPDATE research_tasks SET not_before = ? WHERE id = ?", (now(), task["id"]))
        await self.worker.run_until_idle()
        self.assertEqual(self.db.investigation(inv)["status"], "complete")

    async def test_format_error_gets_one_more_attempt_then_policy(self):
        inv = self.new()
        self.dummy.fail_first["a:1"] = ModelError("format", "bad json")
        await self.run_all(inv)
        self.assertEqual(self.dummy.runs["a:1"], 2)
        self.assertEqual(self.db.investigation(inv)["status"], "complete")

    async def test_skip_policy_and_more_than_half_skipped_fails(self):
        inv = self.new()
        self.dummy.fail_first["a:1"] = ModelError("config", "bad")
        self.dummy.fail_first["a:2"] = ModelError("refusal", "no")
        await self.run_all(inv)
        row = self.db.investigation(inv)
        self.assertEqual(row["status"], "failed")
        self.assertIn("More than half", row["error"])

    async def test_code_step_exception_fails_investigation(self):
        inv = self.new()
        self.db.set_stage(inv, "intake", "codefail")
        await self.run_all(inv)
        row = self.db.investigation(inv)
        self.assertEqual(row["status"], "failed")
        task = self.db.tasks(inv, step="code")[0]
        self.assertEqual(task["error_kind"], "bug")
        self.assertIn("RuntimeError", task["error"])

    async def test_pause_resume_cancel_retry(self):
        inv = self.new()
        await self.worker.machine.advance(inv)
        self.assertTrue(await self.worker.machine.pause(inv))
        self.assertIsNone(self.db.claim_task())  # paused: nothing runs
        self.assertFalse(await self.worker.machine.pause(inv))
        self.assertTrue(await self.worker.machine.resume(inv))
        await self.worker.run_until_idle()
        self.assertEqual(self.db.investigation(inv)["status"], "complete")

        inv2 = self.new()
        await self.worker.machine.advance(inv2)
        self.assertTrue(await self.worker.machine.cancel(inv2))
        self.assertEqual({t["status"] for t in self.db.tasks(inv2)}, {"cancelled"})
        self.assertIsNone(self.db.claim_task())

        inv3 = self.new()
        self.db.set_stage(inv3, "intake", "codefail")
        await self.run_all(inv3)
        self.assertEqual(self.db.investigation(inv3)["status"], "failed")
        self.worker.pipeline.steps["code"].run = self.worker.pipeline.steps["a"].run
        self.assertTrue(await self.worker.machine.retry(inv3))
        await self.worker.run_until_idle()
        self.assertEqual(self.db.investigation(inv3)["status"], "complete")

    async def test_retry_single_task(self):
        inv = self.new()
        self.dummy.fail_first["search:1"] = ModelError("config", "bad")
        await self.run_all(inv)
        task = self.db.tasks(inv, step="search")[0]
        self.assertEqual(task["status"], "skipped")
        self.assertTrue(await self.worker.machine.retry_task(inv, task["id"]))
        self.assertEqual(self.db.task(task["id"])["status"], "queued")

    async def test_concurrent_advance_is_safe(self):
        inv = self.new()
        await asyncio.gather(*[self.worker.machine.advance(inv) for _ in range(10)])
        stages = self.db.query("SELECT * FROM events WHERE investigation_id = ? AND kind = 'stage'", (inv,))
        self.assertEqual(len(stages), 1)
        self.assertEqual(len(self.db.tasks(inv)), 3)

    async def test_wait_and_resolve(self):
        self.dummy.wait_after_work = True
        inv = self.new()
        await self.run_all(inv)
        row = self.db.investigation(inv)
        self.assertEqual((row["status"], row["waiting_for"]), ("waiting_user", "plan_approval"))
        self.assertFalse(await self.worker.machine.resolve_wait(inv, "answers", lambda sc: "empty"))
        self.assertTrue(await self.worker.machine.resolve_wait(inv, "plan_approval", lambda sc: "empty"))
        await self.worker.run_until_idle()
        self.assertEqual(self.db.investigation(inv)["status"], "complete")

    async def test_waiting_time_is_not_counted(self):
        ten_ago = iso(datetime.now(timezone.utc) - timedelta(seconds=10))
        values = status_values({"status": "active", "active_seconds": 5, "active_since": ten_ago}, "waiting_user")
        self.assertAlmostEqual(values["active_seconds"], 15, delta=1)
        self.assertIsNone(values["active_since"])
        resumed = status_values({"status": "waiting_user", "active_seconds": 15, "active_since": None}, "active")
        self.assertNotIn("active_seconds", resumed)
        self.assertIsNotNone(resumed["active_since"])

    async def test_guards_acquisition_stop_then_hard_stop(self):
        inv = self.new(guards={"max_tokens": 1000, "reserve": 0.2})
        self.db.log_call({"investigation_id": inv, "provider": "fake", "model": "m", "status": "ok",
                          "input_tokens": 850, "output_tokens": 0})
        await self.worker.machine.advance(inv)
        keys = {t["key"] for t in self.db.tasks(inv)}
        self.assertEqual(keys, {"a:1", "a:2"})  # search not started; processing continues
        self.assertIn("reserve", self.db.investigation(inv)["stop_reason"])
        await self.worker.run_until_idle()
        self.assertEqual(self.db.investigation(inv)["status"], "complete")

        inv2 = self.new(guards={"max_tokens": 1000, "reserve": 0.2})
        self.db.log_call({"investigation_id": inv2, "provider": "fake", "model": "m", "status": "ok",
                          "input_tokens": 1000, "output_tokens": 0})
        await self.worker.machine.advance(inv2)
        row = self.db.investigation(inv2)
        self.assertEqual((row["status"], row["outcome"]), ("complete", "incomplete"))

    async def test_worker_loop_start_stop(self):
        lock = InstanceLock(self.tmp / "research.lock")
        worker = Worker(self.db, self.dummy.pipeline(), lock=lock, poll_seconds=0.05)
        inv = self.new()
        self.assertTrue(await worker.start())
        for _ in range(200):
            if self.db.investigation(inv)["status"] == "complete":
                break
            await asyncio.sleep(0.02)
        await worker.stop()
        self.assertEqual(self.db.investigation(inv)["status"], "complete")
        self.assertFalse(lock.held)


class LockTests(ResearchTestCase):
    def test_lock_excludes_other_processes(self):
        path = self.tmp / "research.lock"
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(sys.path[0])!r})
            from app.research.lock import InstanceLock, LockHeld
            try:
                InstanceLock({str(path)!r}).acquire()
                print("acquired")
            except LockHeld:
                print("held")
        """)
        import os
        backend = os.path.dirname(os.path.abspath(__file__))

        def other():
            return subprocess.run([sys.executable, "-c", script], cwd=backend, capture_output=True, text=True,
                                  timeout=30).stdout.strip()

        with InstanceLock(path):
            self.assertEqual(other(), "held")
            with self.assertRaises(LockHeld):
                InstanceLock(path).acquire()
        self.assertEqual(other(), "acquired")  # released; the child's lock dies with the child
