"""Bounded single-coordinator scheduling with independently supervised workers."""

from __future__ import annotations

import logging
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Event
from uuid import uuid4

from .locks import FileLock, LockBusy, device_lock_path
from .models import positive_seconds
from .ports import SchedulerRepository, TaskExecutor


LOG = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, store: SchedulerRepository, worker: TaskExecutor, output_root: Path, stop: Event,
                 *, concurrency: int = 2, poll_interval: float = 0.2, lock_root: Path | None = None):
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("concurrency must be a positive integer")
        self.store, self.worker, self.output_root, self.stop = store, worker, output_root, stop
        self.concurrency = concurrency
        self.poll_interval = positive_seconds(poll_interval, "poll_interval")
        self.lock_root = lock_root
        self.owner = uuid4().hex

    def run(self) -> dict:
        with FileLock(Path(str(self.store.path) + ".coordinator.lock")):
            recovered = self.store.recover_interrupted()
            if recovered:
                LOG.warning("Marked %s abandoned attempts lost; their environments are quarantined", recovered)
            active: dict[Future, str] = {}
            incomplete = False
            with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="explore-worker") as pool:
                try:
                    while True:
                        for future in list(active):
                            if future.done():
                                attempt_id = active.pop(future)
                                outcome = future.result()
                                LOG.info("Attempt %s: %s (%s)", attempt_id, outcome.state, outcome.reason)
                        if self.stop.is_set():
                            if not active:
                                break
                        else:
                            view = self.store.snapshot()
                            ready = [t for t in view["tasks"] if t["state"] in {"queued", "retry_wait"}
                                     and t["available_at"] <= time.time()]
                            for env in view["environments"]:
                                if self.stop.is_set() or len(active) >= self.concurrency:
                                    break
                                if env["state"] != "available":
                                    continue
                                if not any(not t["spec"]["devices"] or env["serial"] in t["spec"]["devices"] for t in ready):
                                    continue
                                try:
                                    lock = FileLock(device_lock_path(env["serial"], self.lock_root))
                                except LockBusy:
                                    self.store.quarantine(env["serial"], "Device lock held by another coordinator or surviving explorer")
                                    continue
                                try:
                                    assignment = self.store.claim(env["serial"], self.owner, self.output_root)
                                    if assignment is None:
                                        lock.close()
                                        continue
                                    future = pool.submit(self.worker.execute, assignment, lock)
                                except BaseException:
                                    lock.close()
                                    raise
                                active[future] = assignment.attempt_id
                                LOG.info("Assigned %s to %s", assignment.task_id, assignment.device)

                        if active:
                            wait(active, timeout=self.poll_interval, return_when=FIRST_COMPLETED)
                            continue
                        pending_view = self.store.snapshot()
                        pending = [t for t in pending_view["tasks"] if t["state"] in {"queued", "retry_wait"}]
                        if self.stop.is_set() or not pending:
                            break
                        available = {e["serial"] for e in pending_view["environments"] if e["state"] == "available"}
                        compatible = [t for t in pending if not t["spec"]["devices"] or available.intersection(t["spec"]["devices"])]
                        if not available or not compatible:
                            incomplete = True
                            LOG.error("Pending tasks have no available compatible environment; inspect status and recover devices")
                            break
                        delay = min(t["available_at"] for t in compatible) - time.time()
                        self.stop.wait(min(max(delay, 0.01), self.poll_interval))
                except BaseException:
                    self.stop.set()
                    raise

            final = self.store.snapshot()
            counts = dict(Counter(t["state"] for t in final["tasks"]))
            exit_code = (130 if self.stop.is_set() else 2 if incomplete else
                         1 if counts.get("failed", 0) or counts.get("cancelled", 0) else 0)
            return {"tasks": counts, "environments": dict(Counter(e["state"] for e in final["environments"])),
                    "exit_code": exit_code, "recovered_attempts": recovered}
