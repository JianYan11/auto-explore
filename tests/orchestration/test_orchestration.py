from __future__ import annotations

import json
import io
import os
import signal
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Event
from unittest.mock import patch

from orchestration.cli import main
from orchestration.environments import AdbEnvironmentManager
from orchestration.locks import FileLock, LockBusy, device_lock_path
from orchestration.models import Outcome, TaskSpec
from orchestration.process_runner import ProcessRunner
from orchestration.scheduler import Scheduler
from orchestration.store import TaskStore
from orchestration.worker import Worker, explorer_command


FIXTURES = Path(__file__).parent / "fixtures"


def write_python_executable(path: Path, source: str) -> None:
    # A Python venv path can contain spaces; kernels cannot parse a quoted
    # interpreter in a shebang. A shell trampoline preserves both argv and PID.
    script = path.with_name(path.name + ".py")
    script.write_text(source)
    path.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n')
    path.chmod(0o700)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = TaskStore(self.root / "queue.db")
        self.stop = Event()
        self.adb = self.root / "adb"
        write_python_executable(self.adb, (FIXTURES / "fake_adb.py").read_text())
        self.events = self.root / "events.jsonl"
        self.adb_events = self.root / "adb.jsonl"
        self.env_patch = patch.dict(os.environ, {"FAKE_EXPLORE_EVENTS": str(self.events),
                                                "FAKE_ADB_EVENTS": str(self.adb_events)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def build_command(self, assignment, adb, mode="success"):
        command = explorer_command(assignment, adb)
        command[1] = str(FIXTURES / "fake_explorer.py")
        return command + ["--mode", mode]

    def scheduler(self, *, mode="success", concurrency=2, command_builder=None, environments=None):
        worker = Worker(self.store, environments or AdbEnvironmentManager(str(self.adb), boot_timeout=2),
                        str(self.adb), self.stop, runner=ProcessRunner(poll_interval=0.02, stop_grace=0.1),
                        command_builder=command_builder or (lambda a, adb: self.build_command(a, adb, mode)))
        return Scheduler(self.store, worker, self.root / "artifacts", self.stop, concurrency=concurrency,
                         poll_interval=0.02, lock_root=self.root / "locks")

    def register(self, *devices):
        for device in devices:
            self.store.register_environment(device)

    def event_rows(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for asynchronous test condition")

    def test_two_devices_overlap_and_each_device_is_exclusive(self):
        self.register("127.0.0.1:15001", "127.0.0.1:15002", "127.0.0.1:15003")
        self.store.enqueue([TaskSpec(f"com.example.app{i}") for i in range(4)])
        result = self.scheduler().run()
        self.assertEqual(result["exit_code"], 0)
        running, peak = set(), 0
        for event in self.event_rows():
            if event["kind"] == "start":
                self.assertNotIn(event["device"], running)
                running.add(event["device"])
                peak = max(peak, len(running))
            else:
                running.remove(event["device"])
        self.assertEqual(peak, 2)
        self.assertFalse(running)
        view = self.store.snapshot()
        self.assertEqual(len({a["output_dir"] for a in view["attempts"]}), 4)
        for attempt in view["attempts"]:
            output = Path(attempt["output_dir"])
            self.assertTrue((output / "worker.log").exists())
            self.assertEqual(json.loads((output / "result.json").read_text())["state"], "succeeded")
        self.assertTrue(all(e["state"] == "available" for e in view["environments"]))
        commands = [json.loads(line)["command"] for line in self.adb_events.read_text().splitlines()]
        self.assertFalse(any(c[:3] == ["shell", "pm", "clear"] for c in commands))

    def test_priority_affinity_resource_and_concurrency_limits(self):
        self.register("127.0.0.1:15001", "127.0.0.1:15002")
        ids = self.store.enqueue([
            TaskSpec("com.example.low", priority=0, resource_key="account"),
            TaskSpec("com.example.high", priority=10, resource_key="account"),
        ])
        self.assertEqual(self.scheduler(concurrency=2).run()["exit_code"], 0)
        starts = [e for e in self.event_rows() if e["kind"] == "start"]
        self.assertEqual([e["package"] for e in starts], ["com.example.high", "com.example.low"])
        events = self.event_rows()
        self.assertEqual([e["kind"] for e in events], ["start", "end", "start", "end"])
        extra = self.store.enqueue([TaskSpec("com.example.pinned", devices=("127.0.0.1:15002",))])[0]
        self.assertEqual(self.scheduler(concurrency=1).run()["exit_code"], 0)
        attempt = next(a for a in self.store.snapshot()["attempts"] if a["task_id"] == extra)
        self.assertEqual(attempt["serial"], "127.0.0.1:15002")
        self.assertEqual(len(ids), 2)

    def test_claim_is_atomic_under_competing_threads(self):
        self.register("127.0.0.1:15001", "127.0.0.1:15002")
        self.store.enqueue([TaskSpec("com.example.one")])
        with ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(lambda d: self.store.claim(d, "owner", self.root / "outputs"),
                                   ("127.0.0.1:15001", "127.0.0.1:15002")))
        self.assertEqual(sum(c is not None for c in claims), 1)

    def test_retry_preserves_attempt_artifacts_and_uses_new_directory(self):
        self.register("127.0.0.1:15001")
        self.store.enqueue([TaskSpec("com.example.retry", max_attempts=2, retry_delay_seconds=0.03)])
        def command(a, adb):
            return self.build_command(a, adb, "exit" if a.attempt_no == 1 else "success")
        self.assertEqual(self.scheduler(command_builder=command).run()["exit_code"], 0)
        attempts = self.store.snapshot()["attempts"]
        self.assertEqual([a["state"] for a in attempts], ["failed", "succeeded"])
        self.assertNotEqual(attempts[0]["output_dir"], attempts[1]["output_dir"])
        self.assertTrue(Path(attempts[0]["output_dir"], "worker.log").exists())

    def test_timeout_stops_process_group_before_device_reuse(self):
        self.register("127.0.0.1:15001")
        self.store.enqueue([TaskSpec("com.example.slow", timeout_seconds=1), TaskSpec("com.example.next")])
        def command(a, adb):
            return self.build_command(a, adb, "spawn" if a.spec.package.endswith("slow") else "success")
        self.assertEqual(self.scheduler(command_builder=command).run()["exit_code"], 1)
        attempts = self.store.snapshot()["attempts"]
        self.assertEqual(attempts[0]["reason"], "timed_out")
        self.assertEqual(attempts[1]["state"], "succeeded")
        child_pid = int(Path(attempts[0]["output_dir"], "child.pid").read_text())
        # A terminated descendant may briefly remain a zombie until reaped by init.
        status = subprocess.run(["ps", "-o", "stat=", "-p", str(child_pid)], capture_output=True, text=True).stdout.strip()
        self.assertTrue(not status or status.startswith("Z"), status)

    def test_cancel_active_task_stops_process_and_pending_cancel_never_starts(self):
        self.register("127.0.0.1:15001")
        active, pending = self.store.enqueue([TaskSpec("com.example.active"), TaskSpec("com.example.pending")])
        self.store.cancel(pending)
        scheduler = self.scheduler(mode="sleep")
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(scheduler.run)
            self.wait_for(lambda: bool(self.event_rows()))
            self.store.cancel(active)
            self.assertEqual(future.result(timeout=5)["exit_code"], 1)
        self.assertEqual([t["state"] for t in self.store.snapshot()["tasks"]], ["cancelled", "cancelled"])
        self.assertEqual(len(self.event_rows()), 1)

    def test_stop_event_cancels_running_and_keeps_pending_tasks(self):
        self.register("127.0.0.1:15001")
        self.store.enqueue([TaskSpec("com.example.active"), TaskSpec("com.example.pending")])
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.scheduler(mode="sleep").run)
            self.wait_for(lambda: bool(self.event_rows()))
            self.stop.set()
            self.assertEqual(future.result(timeout=5)["exit_code"], 130)
        self.assertEqual([t["state"] for t in self.store.snapshot()["tasks"]], ["cancelled", "queued"])

    def test_exit_zero_with_blocked_or_invalid_graph_is_not_success(self):
        for mode in ("blocked", "invalid"):
            with self.subTest(mode=mode):
                self.register("127.0.0.1:15001")
                task_id = self.store.enqueue([TaskSpec(f"com.example.{mode}")])[0]
                self.scheduler(mode=mode).run()
                task = next(t for t in self.store.snapshot()["tasks"] if t["id"] == task_id)
                self.assertEqual(task["state"], "failed")

    def test_offline_device_is_quarantined_without_losing_other_tasks(self):
        self.register("offline")
        self.store.enqueue([TaskSpec("com.example.first"), TaskSpec("com.example.second")])
        result = self.scheduler(environments=AdbEnvironmentManager(str(self.adb), boot_timeout=0.2)).run()
        self.assertEqual(result["exit_code"], 2)
        view = self.store.snapshot()
        self.assertEqual([t["state"] for t in view["tasks"]], ["failed", "queued"])
        self.assertEqual(view["environments"][0]["state"], "quarantined")
        self.assertFalse(self.events.exists())

    def test_missing_package_does_not_poison_healthy_device(self):
        self.store.register_environment("127.0.0.1:15001", "clear-data")
        self.store.enqueue([TaskSpec("com.example.missing"), TaskSpec("com.example.present")])
        self.scheduler().run()
        self.assertEqual([t["state"] for t in self.store.snapshot()["tasks"]], ["failed", "succeeded"])
        self.assertEqual(self.store.snapshot()["environments"][0]["state"], "available")

    def test_cleanup_failure_preserves_valid_result_and_quarantines_environment(self):
        manager = AdbEnvironmentManager(str(self.adb))
        def broken_reset(*args):
            raise RuntimeError("reset failed")
        manager.reset = broken_reset
        self.register("127.0.0.1:15001")
        self.store.enqueue([TaskSpec("com.example.valid")])
        self.assertEqual(self.scheduler(environments=manager).run()["exit_code"], 0)
        view = self.store.snapshot()
        self.assertEqual(view["tasks"][0]["state"], "succeeded")
        self.assertEqual(view["environments"][0]["state"], "quarantined")

    def test_clear_data_requires_explicit_environment_policy(self):
        self.store.register_environment("127.0.0.1:15001", "clear-data")
        self.store.enqueue([TaskSpec("com.example.clean")])
        self.scheduler().run()
        commands = [json.loads(line)["command"] for line in self.adb_events.read_text().splitlines()]
        self.assertEqual(sum(c == ["shell", "pm", "clear", "com.example.clean"] for c in commands), 2)

    def test_persistent_queue_and_abandoned_attempt_recovery(self):
        self.register("127.0.0.1:15001")
        task_id = self.store.enqueue([TaskSpec("com.example.lost")])[0]
        assignment = self.store.claim("127.0.0.1:15001", "previous-owner", self.root / "artifacts")
        restarted = TaskStore(self.store.path)
        self.assertEqual(restarted.recover_interrupted(), 1)
        view = restarted.snapshot()
        self.assertEqual(view["attempts"][0]["state"], "lost")
        self.assertEqual(view["environments"][0]["state"], "quarantined")
        with self.assertRaises(RuntimeError):
            restarted.finish(assignment, Outcome("succeeded", "budget_limited"))
        restarted.recover_environment("127.0.0.1:15001")
        restarted.retry(task_id)
        self.assertEqual(self.scheduler().run()["exit_code"], 0)
        self.assertEqual(len(restarted.snapshot()["attempts"]), 2)

    def test_device_lock_survives_parent_close_while_child_alive(self):
        path = device_lock_path("127.0.0.1:15001", self.root / "locks")
        lock = FileLock(path)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], pass_fds=(lock.fd,))
        try:
            lock.close()
            with self.assertRaises(LockBusy):
                FileLock(path)
        finally:
            child.kill()
            child.wait()
        with FileLock(path):
            pass

    def test_sigkill_coordinator_leaves_live_explorer_locked_and_quarantines_on_restart(self):
        self.register("127.0.0.1:15001")
        self.store.enqueue([TaskSpec("com.example.orphan")])
        # Use another interpreter, then kill its coordinator while the real child
        # is alive. This covers inherited locks and persistent recovery together.
        script = """
import sys
from pathlib import Path
from threading import Event
from orchestration.store import TaskStore
from orchestration.environments import AdbEnvironmentManager
from orchestration.worker import Worker, explorer_command
from orchestration.process_runner import ProcessRunner
from orchestration.scheduler import Scheduler
db, adb, root, fixture = sys.argv[1:]
store = TaskStore(Path(db))
stop = Event()
def command(assignment, adb):
    value = explorer_command(assignment, adb)
    value[1] = fixture
    return value + ['--mode', 'sleep']
worker = Worker(store, AdbEnvironmentManager(adb), adb, stop,
                runner=ProcessRunner(stop_grace=0.1), command_builder=command)
Scheduler(store, worker, Path(root)/'artifacts', stop, lock_root=Path(root)/'locks').run()
"""
        coordinator = subprocess.Popen([sys.executable, "-c", script, str(self.store.path), str(self.adb),
                                        str(self.root), str(FIXTURES / "fake_explorer.py")])
        child_pid = None
        try:
            self.wait_for(lambda: bool(self.event_rows()))
            child_pid = self.event_rows()[0]["pid"]
            coordinator.kill()
            coordinator.wait(timeout=5)
            result = self.scheduler().run()
            self.assertEqual(result["recovered_attempts"], 1)
            self.assertEqual(self.store.snapshot()["attempts"][0]["state"], "lost")
            self.assertEqual(self.store.snapshot()["environments"][0]["state"], "quarantined")
            with self.assertRaises(LockBusy):
                FileLock(device_lock_path("127.0.0.1:15001", self.root / "locks"))
        finally:
            if coordinator.poll() is None:
                coordinator.kill()
                coordinator.wait()
            if child_pid is not None:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_cli_sigterm_cancels_preparation_without_model_dependencies(self):
        self.register("127.0.0.1:15001")
        self.store.enqueue([TaskSpec("com.example.cancelled")])
        # Exercise the production CLI -> scheduler -> worker signal path. Hold
        # preparation until SIGTERM, before a model-dependent explorer can start.
        slow_adb = self.root / "slow-adb"
        write_python_executable(slow_adb, "import time\ntime.sleep(10)\n")
        command = [sys.executable, str(Path(__file__).resolve().parents[2] / "explore_batch.py"),
                   "--db", str(self.store.path), "run", "--adb", str(slow_adb), "--adb-timeout", "0.1"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_for(lambda: bool(self.store.snapshot()["attempts"]))
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, stderr)
            self.assertEqual(json.loads(stdout)["tasks"], {"cancelled": 1})
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_second_coordinator_cannot_recover_active_work(self):
        self.register("127.0.0.1:15001")
        self.store.enqueue([TaskSpec("com.example.one")])
        with FileLock(Path(str(self.store.path) + ".coordinator.lock")):
            with self.assertRaises(LockBusy):
                self.scheduler().run()
        self.assertEqual(self.store.snapshot()["tasks"][0]["state"], "queued")

    def test_device_owned_by_another_queue_is_quarantined_without_starting(self):
        self.register("127.0.0.1:15001")
        self.store.enqueue([TaskSpec("com.example.one")])
        with FileLock(device_lock_path("127.0.0.1:15001", self.root / "locks")):
            self.assertEqual(self.scheduler().run()["exit_code"], 2)
        view = self.store.snapshot()
        self.assertEqual(view["tasks"][0]["state"], "queued")
        self.assertEqual(view["environments"][0]["state"], "quarantined")
        self.assertFalse(view["attempts"])

    def test_cancellation_during_final_cleanup_does_not_rewrite_committed_decision(self):
        self.register("127.0.0.1:15001")
        task_id = self.store.enqueue([TaskSpec("com.example.finalizing")])[0]
        manager = AdbEnvironmentManager(str(self.adb))
        cleanup_started, finish_cleanup = Event(), Event()
        original_reset = manager.reset
        def reset(*args):
            cleanup_started.set()
            if not finish_cleanup.wait(5):
                raise RuntimeError("Test did not release cleanup")
            original_reset(*args)
        manager.reset = reset
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.scheduler(environments=manager).run)
            try:
                self.assertTrue(cleanup_started.wait(5))
                self.store.cancel(task_id)
            finally:
                finish_cleanup.set()
            self.assertEqual(future.result(timeout=5)["exit_code"], 0)
        attempt = self.store.snapshot()["attempts"][0]
        self.assertEqual(json.loads(Path(attempt["output_dir"], "result.json").read_text())["state"], "succeeded")

    def test_no_compatible_device_keeps_task_queued(self):
        self.register("127.0.0.1:15001")
        self.store.enqueue([TaskSpec("com.example.pinned", devices=("127.0.0.1:15002",))])
        self.assertEqual(self.scheduler().run()["exit_code"], 2)
        self.assertEqual(self.store.snapshot()["tasks"][0]["state"], "queued")

    def test_validation_rejects_overrides_secrets_and_nonfinite_values(self):
        for value in ({"options": {"device": "wrong"}}, {"options": {"api_key": "secret"}},
                      {"timeout_seconds": float("nan")}, {"max_attempts": 0},
                      {"options": {"max_steps": True}}, {"options": {"summary_timeout": 0}},
                      {"devices": "127.0.0.1:15001"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TaskSpec.from_dict({"package": "com.example.app", **value})

    def test_manifest_validation_is_atomic_and_cli_needs_no_model_sdk(self):
        manifest = self.root / "tasks.json"
        manifest.write_text(json.dumps([{"package": "com.example.valid"}, {"package": "invalid"}]))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.store.path), "enqueue", str(manifest)]), 2)
        self.assertEqual(self.store.snapshot()["tasks"], [])
        manifest.write_text(json.dumps([{"package": "com.example.valid"}]))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.store.path), "enqueue", str(manifest)]), 0)
        self.assertEqual(len(self.store.snapshot()["tasks"]), 1)


if __name__ == "__main__":
    unittest.main()
