"""Infrastructure boundaries used by application orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from .models import Assignment, Outcome


class DeviceLease(Protocol):
    fd: int | None

    def close(self) -> None: ...


class ProcessExecutor(Protocol):
    def run(self, command: list[str], output: Path, *, lock_fd: int,
            check: Callable[[], None], on_start: Callable[[int], None]) -> int: ...


class TaskExecutor(Protocol):
    def execute(self, assignment: Assignment, device_lock: DeviceLease) -> Outcome: ...


class EnvironmentManager(Protocol):
    def prepare(self, assignment: Assignment, deadline: float, check: Callable[[], None]) -> None: ...
    def reset(self, serial: str, package: str, policy: str) -> None: ...


class WorkerRepository(Protocol):
    def touch(self, assignment: Assignment, *, state: str | None = None, pid: int | None = None) -> None: ...
    def is_cancelled(self, task_id: str) -> bool: ...
    def finish(self, assignment: Assignment, outcome: Outcome, environment_error: str = "") -> None: ...


class SchedulerRepository(WorkerRepository, Protocol):
    path: Path

    def snapshot(self) -> dict: ...
    def claim(self, serial: str, owner: str, output_root: Path) -> Assignment | None: ...
    def quarantine(self, serial: str, error: str) -> None: ...
    def recover_interrupted(self) -> int: ...
