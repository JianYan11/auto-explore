from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from unittest.mock import patch

from orchestration.cloud_environments import CloudEnvironmentManager
from orchestration.environments import AdbEnvironmentManager
from orchestration.errors import EnvironmentFailure, SandboxCreationRejected
from orchestration.locks import FileLock, LockBusy, device_lock_path
from orchestration.models import TaskSpec
from orchestration.ports import PreparedEnvironment
from orchestration.process_runner import ProcessRunner
from orchestration.scheduler import Scheduler
from orchestration.store import SCHEMA, TaskStore
from orchestration.worker import Worker, explorer_command
from tests.orchestration.test_orchestration import FIXTURES, write_python_executable


class FakeProvider:
    def __init__(self):
        self.instances = {}
        self.calls = []
        self.lock = Lock()
        self.peak = 0
        self.starting = False
        self.connect_failure = False
        self.create_response_lost = False
        self.disconnect_failure = False
        self.delete_failure = False

    def create(self, resource, deadline, check):
        check()
        with self.lock:
            instance_id = f"instance-{len(self.instances) + 1}"
            self.instances[instance_id] = {**resource, "stopped": False, "connected": False}
            self.calls.append(("create", instance_id))
            self.peak = max(self.peak, sum(not i["stopped"] for i in self.instances.values()))
        if self.create_response_lost:
            raise EnvironmentFailure("Create response lost")
        return instance_id

    def state(self, instance_id, region, deadline, check):
        check()
        if self.instances[instance_id]["stopped"]:
            return "STOPPED"
        return "STARTING" if self.starting else "RUNNING"

    def connect(self, instance_id, region, deadline, check):
        self.instances[instance_id]["connected"] = True
        self.calls.append(("connect", instance_id))
        if self.connect_failure:
            raise EnvironmentFailure("ADB connect response lost after tunnel creation")
        serial = f'127.0.0.1:{15000 + int(instance_id.split("-")[-1])}'
        return PreparedEnvironment(serial, instance_id)

    def disconnect(self, instance_id, region, deadline):
        self.calls.append(("disconnect", instance_id))
        if self.disconnect_failure:
            raise EnvironmentFailure("Tunnel cleanup failed")
        self.instances[instance_id]["connected"] = False

    def delete(self, instance_id, region, deadline):
        self.calls.append(("delete", instance_id))
        if self.delete_failure:
            raise EnvironmentFailure("Cloud unavailable")
        self.instances[instance_id]["stopped"] = True

    def find(self, resource, deadline):
        return [key for key, value in self.instances.items() if value["attempt_id"] == resource["attempt_id"]]


class CloudLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = TaskStore(self.root / "queue.sqlite3")
        self.adb = self.root / "adb"
        write_python_executable(self.adb, (FIXTURES / "fake_adb.py").read_text())
        self.provider = FakeProvider()
        self.stop = Event()
        self.manager = CloudEnvironmentManager(self.store, self.provider, AdbEnvironmentManager(str(self.adb)),
                                                poll_interval=0.01, cleanup_timeout=1, lock_root=self.root / "locks")

    def scheduler(self, *, concurrency=2, mode="success"):
        def command(assignment, adb):
            result = explorer_command(assignment, adb)
            result[1] = str(FIXTURES / "fake_explorer.py")
            return result + ["--mode", mode]
        def startup():
            self.store.ensure_cloud_pool("sdt-test", "ap-guangzhou", concurrency)
            self.manager.reconcile(self.store.snapshot()["environments"])
        worker = Worker(self.store, self.manager, str(self.adb), self.stop,
                        runner=ProcessRunner(stop_grace=0.1, poll_interval=0.01), command_builder=command)
        return Scheduler(self.store, worker, self.root / "artifacts", self.stop, concurrency=concurrency,
                         poll_interval=0.01, lock_root=self.root / "locks", startup=startup,
                         environment_filter=lambda env: env["provider"] == "tencent")

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for lifecycle transition")

    def test_multiple_apps_get_distinct_instances_with_bounded_concurrency_and_release(self):
        self.store.register_environment("127.0.0.1:19999")  # Must not mix providers.
        self.store.enqueue([TaskSpec(f"com.example.app{i}") for i in range(4)])
        self.assertEqual(self.scheduler().run()["exit_code"], 0)
        self.assertEqual(self.provider.peak, 2)
        self.assertEqual(len(self.provider.instances), 4)
        self.assertTrue(all(i["stopped"] and not i["connected"] for i in self.provider.instances.values()))
        self.assertFalse(self.store.cloud_resources())
        for attempt in self.store.snapshot()["attempts"]:
            self.assertTrue(attempt["serial"].startswith("cloud:"))
            actual = json.loads((Path(attempt["output_dir"]) / "environment.json").read_text())
            self.assertTrue(actual["adb_serial"].startswith("127.0.0.1:"))
            self.assertEqual(actual["environment_id"], attempt["serial"])
        self.assertEqual([c[0] for c in self.provider.calls].count("delete"), 4)

    def test_empty_queue_creates_no_instances(self):
        self.assertEqual(self.scheduler(concurrency=3).run()["exit_code"], 0)
        self.assertEqual(self.provider.calls, [])

    def test_cancel_during_startup_and_exploration_always_releases(self):
        for phase in ("starting", "running"):
            with self.subTest(phase=phase):
                self.provider.starting = phase == "starting"
                task_id = self.store.enqueue([TaskSpec("com.example.cancel")])[0]
                with ThreadPoolExecutor() as pool:
                    future = pool.submit(self.scheduler(concurrency=1, mode="sleep").run)
                    self.wait_for(lambda: any(r["state"] == ("starting" if phase == "starting" else "ready")
                                             for r in self.store.cloud_resources()))
                    self.store.cancel(task_id)
                    future.result(timeout=5)
                task = next(t for t in self.store.snapshot()["tasks"] if t["id"] == task_id)
                self.assertEqual(task["state"], "cancelled")
                self.assertFalse(self.store.cloud_resources())
                self.assertTrue(all(i["stopped"] for i in self.provider.instances.values()))

    def test_timeout_and_shutdown_release_without_task_deadline_or_cancel_check(self):
        self.provider.starting = True
        self.store.enqueue([TaskSpec("com.example.timeout", timeout_seconds=0.2)])
        self.scheduler(concurrency=1).run()
        self.assertEqual(self.store.snapshot()["attempts"][0]["reason"], "timed_out")
        self.assertFalse(self.store.cloud_resources())
        self.provider.starting = False
        self.store.enqueue([TaskSpec("com.example.shutdown")])
        with ThreadPoolExecutor() as pool:
            future = pool.submit(self.scheduler(concurrency=1, mode="sleep").run)
            self.wait_for(lambda: any(r["state"] == "ready" for r in self.store.cloud_resources()))
            self.stop.set()
            self.assertEqual(future.result(timeout=5)["exit_code"], 130)
        self.assertFalse(self.store.cloud_resources())

    def test_partial_connect_and_missing_app_release_and_do_not_poison_slot(self):
        self.provider.connect_failure = True
        self.store.enqueue([TaskSpec("com.example.first")])
        self.scheduler(concurrency=1).run()
        self.provider.connect_failure = False
        self.store.enqueue([TaskSpec("com.example.missing"), TaskSpec("com.example.present")])
        self.scheduler(concurrency=1).run()
        self.assertEqual([t["state"] for t in self.store.snapshot()["tasks"]], ["failed", "failed", "succeeded"])
        self.assertTrue(all(i["stopped"] and not i["connected"] for i in self.provider.instances.values()))

    def test_failed_cleanup_is_durable_and_retry_does_not_rerun_or_create(self):
        self.provider.disconnect_failure = True
        self.store.enqueue([TaskSpec("com.example.valid")])
        self.assertEqual(self.scheduler(concurrency=1).run()["exit_code"], 2)
        self.assertTrue(next(iter(self.provider.instances.values()))["stopped"])
        self.assertEqual(self.store.snapshot()["tasks"][0]["state"], "succeeded")
        self.assertEqual(self.store.snapshot()["environments"][0]["state"], "quarantined")
        self.assertEqual(self.store.cloud_resources()[0]["state"], "cleanup_pending")
        self.provider.disconnect_failure = False
        self.assertEqual(self.scheduler(concurrency=1).run()["exit_code"], 0)
        self.assertEqual(len(self.provider.instances), 1)
        self.assertFalse(self.store.cloud_resources())

    def test_create_response_loss_and_restart_recover_only_owned_metadata(self):
        slot = self.store.ensure_cloud_pool("sdt-test", "ap-guangzhou", 1)[0]
        self.store.enqueue([TaskSpec("com.example.crashed")])
        assignment = self.store.claim(slot, "old-owner", self.root / "artifacts")
        resource = self.store.begin_cloud_resource(assignment, 300)
        self.store.update_cloud_resource(assignment.attempt_id, state="creating")
        self.provider.create(resource, time.monotonic() + 1, lambda: None)
        # Simulate SIGKILL between cloud allocation and saving InstanceId.
        self.store.recover_interrupted()
        with FileLock(device_lock_path(slot, self.root / "locks")):
            self.manager.reconcile(self.store.snapshot()["environments"])
            self.assertFalse(self.provider.instances["instance-1"]["stopped"])
        self.manager.reconcile(self.store.snapshot()["environments"])
        self.assertTrue(self.provider.instances["instance-1"]["stopped"])
        self.assertFalse(self.store.cloud_resources())
        self.assertEqual(len(self.provider.instances), 1)

    def test_unknown_create_is_retained_without_blind_replay(self):
        slot = self.store.ensure_cloud_pool("sdt-test", "ap-guangzhou", 1)[0]
        self.store.enqueue([TaskSpec("com.example.unknown")])
        assignment = self.store.claim(slot, "old-owner", self.root / "artifacts")
        self.store.begin_cloud_resource(assignment, 300)
        self.store.update_cloud_resource(assignment.attempt_id, state="creating")
        self.store.recover_interrupted()
        self.manager.reconcile(self.store.snapshot()["environments"])
        self.assertEqual(self.store.cloud_resources()[0]["state"], "cleanup_pending")
        self.assertFalse(self.provider.calls)
        with self.assertRaises(ValueError):
            self.store.recover_environment(slot)

    def test_actual_adb_address_is_locked_until_release(self):
        slot = self.store.ensure_cloud_pool("sdt-test", "ap-guangzhou", 1)[0]
        self.store.enqueue([TaskSpec("com.example.locked")])
        assignment = self.store.claim(slot, "owner", self.root / "artifacts")
        prepared = self.manager.prepare(assignment, time.monotonic() + 5, lambda: None)
        with self.assertRaises(LockBusy):
            FileLock(device_lock_path(prepared.device, self.root / "locks"))
        self.manager.reset(slot, "", "restart")
        with FileLock(device_lock_path(prepared.device, self.root / "locks")):
            pass

    def test_lost_create_response_is_found_and_compensated_before_next_task(self):
        self.provider.create_response_lost = True
        self.store.enqueue([TaskSpec("com.example.lostresponse")])
        self.assertEqual(self.scheduler(concurrency=1).run()["exit_code"], 1)
        self.assertEqual(len(self.provider.instances), 1)
        self.assertTrue(self.provider.instances["instance-1"]["stopped"])
        self.assertFalse(self.store.cloud_resources())
        self.assertEqual(self.store.snapshot()["environments"][0]["state"], "available")

    def test_rejected_create_does_not_leave_fictitious_cleanup_debt(self):
        with patch.object(self.provider, "create", side_effect=SandboxCreationRejected("Quota exceeded")):
            self.store.enqueue([TaskSpec("com.example.rejected")])
            self.scheduler(concurrency=1).run()
        self.assertFalse(self.store.cloud_resources())
        self.assertFalse(self.provider.calls)
        self.assertEqual(self.store.snapshot()["environments"][0]["state"], "available")

    def test_delete_failure_blocks_reuse_and_cleanup_retry_stops_only_owned_instance(self):
        self.provider.delete_failure = True
        self.store.enqueue([TaskSpec("com.example.debt")])
        self.assertEqual(self.scheduler(concurrency=1).run()["exit_code"], 2)
        self.assertFalse(self.provider.instances["instance-1"]["stopped"])
        self.provider.instances["foreign"] = {"attempt_id": "not-this-queue", "stopped": False, "connected": True}
        self.provider.delete_failure = False
        self.manager.reconcile(self.store.snapshot()["environments"])
        self.assertTrue(self.provider.instances["instance-1"]["stopped"])
        self.assertFalse(self.provider.instances["foreign"]["stopped"])
        self.assertFalse(self.store.cloud_resources())

    def test_database_finalization_error_still_releases_cloud(self):
        slot = self.store.ensure_cloud_pool("sdt-test", "ap-guangzhou", 1)[0]
        self.store.enqueue([TaskSpec("com.example.database")])
        assignment = self.store.claim(slot, "owner", self.root / "artifacts")
        worker = self.scheduler(concurrency=1).worker
        touch = self.store.touch
        def broken_touch(assignment, *, state=None, pid=None):
            if state == "finalizing":
                raise RuntimeError("Cannot write heartbeat")
            touch(assignment, state=state, pid=pid)
        with FileLock(device_lock_path(slot, self.root / "locks")) as lease, \
             patch.object(self.store, "touch", side_effect=broken_touch):
            with self.assertRaisesRegex(RuntimeError, "heartbeat"):
                worker.execute(assignment, lease)
        self.assertFalse(self.store.cloud_resources())
        self.assertTrue(self.provider.instances["instance-1"]["stopped"])

    def test_v1_queue_migrates_without_losing_history(self):
        path = self.root / "v1.sqlite3"
        with sqlite3.connect(path) as db:
            db.executescript(SCHEMA)
            db.execute("PRAGMA user_version=1")
            db.execute("INSERT INTO environments(serial,reset_policy) VALUES('old-serial','restart')")
        migrated = TaskStore(path)
        self.assertEqual(migrated.snapshot()["environments"][0]["provider"], "adb")
        migrated.ensure_cloud_pool("sdt-test", "ap-guangzhou", 1)
        self.assertEqual(len(TaskStore(path).snapshot()["environments"]), 2)
