"""Execute one allocated task; supervise the existing CLI rather than its UI loop."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from threading import Event
from typing import Callable

from .errors import EnvironmentFailure, ExecutionAborted
from .models import Assignment, Outcome, SUCCESS_REASONS
from .ports import DeviceLease, EnvironmentManager, ProcessExecutor, WorkerRepository


def explorer_command(assignment: Assignment, adb: str, *, python: str = sys.executable) -> list[str]:
    script = Path(__file__).resolve().parent.parent / "explore_android_app.py"
    command = [python, str(script), "--adb", adb, "--device", assignment.device,
               "--package", assignment.spec.package, "--output", assignment.output_dir]
    for key, value in assignment.spec.options.items():
        flag = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                command.append(flag)
        else:
            command.extend([flag, str(value)])
    return command


def validate_result(assignment: Assignment, returncode: int) -> Outcome:
    if returncode:
        return Outcome("failed", "process_failed", f"Explorer exited {returncode}; see worker.log",
                       retryable=returncode != 2, returncode=returncode)
    try:
        graph = json.loads((Path(assignment.output_dir) / "app_graph.json").read_text(encoding="utf-8"))
        if not isinstance(graph, dict) or graph.get("app_package") != assignment.spec.package:
            raise ValueError("Graph package does not match task")
        pages = graph.get("pages")
        if not isinstance(pages, dict) or not pages or graph.get("root_page_id") not in pages:
            raise ValueError("Graph has no valid root page")
        if not isinstance(graph.get("run_id"), str) or not graph["run_id"]:
            raise ValueError("Graph has no run_id")
        termination = graph.get("termination")
        if not isinstance(termination, dict):
            raise ValueError("Graph has no termination record")
        reason = termination.get("reason")
        if reason not in SUCCESS_REASONS:
            return Outcome("failed", "exploration_incomplete", f"Exploration stopped with {reason!r}", returncode=0)
        return Outcome("succeeded", reason, returncode=0)
    except (OSError, ValueError, TypeError) as exc:
        return Outcome("failed", "invalid_result", str(exc), retryable=True, returncode=0)


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class Worker:
    def __init__(self, store: WorkerRepository, environments: EnvironmentManager, adb: str, stop: Event,
                 *, runner: ProcessExecutor,
                 command_builder: Callable[[Assignment, str], list[str]] = explorer_command):
        self.store = store
        self.environments = environments
        self.adb = adb
        self.stop = stop
        self.runner = runner
        self.command_builder = command_builder

    def execute(self, assignment: Assignment, device_lock: DeviceLease) -> Outcome:
        deadline = time.monotonic() + assignment.spec.timeout_seconds
        last_heartbeat = 0.0
        touched_environment = False
        cleanup_package = assignment.spec.package
        environment_error = ""
        output = Path(assignment.output_dir)
        outcome = Outcome("failed", "worker_error", "Worker did not finish")

        def check() -> None:
            nonlocal last_heartbeat
            if self.stop.is_set() or self.store.is_cancelled(assignment.task_id):
                raise ExecutionAborted("cancelled")
            now = time.monotonic()
            if now >= deadline:
                raise ExecutionAborted("timed_out")
            if now - last_heartbeat >= 1:
                self.store.touch(assignment)
                last_heartbeat = now

        try:
            try:
                output.mkdir(parents=True, exist_ok=False)
                atomic_json(output / "assignment.json", {
                    "task_id": assignment.task_id, "attempt_id": assignment.attempt_id,
                    "device": assignment.device, "spec": asdict(assignment.spec),
                })
                check()
                touched_environment = True
                self.environments.prepare(assignment, deadline, check)
                check()
                assert device_lock.fd is not None
                returncode = self.runner.run(
                    self.command_builder(assignment, self.adb), output / "worker.log", lock_fd=device_lock.fd,
                    check=check, on_start=lambda pid: self.store.touch(assignment, state="running", pid=pid),
                )
                self.store.touch(assignment, state="finalizing")
                check()
                outcome = validate_result(assignment, returncode)
            except ExecutionAborted as exc:
                outcome = Outcome("cancelled" if exc.reason == "cancelled" else "failed", exc.reason,
                                  retryable=exc.reason == "timed_out")
            except EnvironmentFailure as exc:
                outcome = Outcome("failed", "environment_error", str(exc))
                if exc.quarantine:
                    environment_error = str(exc)
                else:
                    # A missing package is a task input problem. Check device
                    # health without attempting to clear an app that is absent.
                    cleanup_package = ""
            except Exception as exc:
                outcome = Outcome("failed", "worker_error", f"{type(exc).__name__}: {exc}")
                # Unexpected failures may include inability to stop the process group.
                environment_error = str(exc) if touched_environment else ""

            self.store.touch(assignment, state="finalizing")
            if touched_environment:
                try:
                    self.environments.reset(assignment.device, cleanup_package, assignment.reset_policy)
                except Exception as exc:
                    environment_error = f"Environment cleanup failed: {type(exc).__name__}: {exc}"
            # A valid exploration stays successful even when its environment needs repair.
            if self.store.is_cancelled(assignment.task_id) or self.stop.is_set():
                outcome = Outcome("cancelled", "cancelled", returncode=outcome.returncode)
            try:
                atomic_json(output / "result.json", {
                    "task_id": assignment.task_id, "attempt_id": assignment.attempt_id,
                    **asdict(outcome), "environment_error": environment_error,
                    "graph_path": str(output / "app_graph.json"),
                })
            except Exception as exc:
                outcome = Outcome("failed", "result_io_error", f"{type(exc).__name__}: {exc}")
            # Physical cleanup is complete. Close before publishing availability,
            # so the scheduler cannot mistake our last few instructions for contention.
            device_lock.close()
            self.store.finish(assignment, outcome, environment_error)
            return outcome
        finally:
            device_lock.close()
