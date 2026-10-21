"""Run one explorer in an isolated POSIX process group with bounded shutdown."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Callable

from .models import positive_seconds


class ProcessRunner:
    def __init__(self, *, poll_interval: float = 0.2, stop_grace: float = 10):
        self.poll_interval = positive_seconds(poll_interval, "poll_interval")
        self.stop_grace = positive_seconds(stop_grace, "stop_grace")

    @staticmethod
    def _signal(pid: int, sig: int) -> None:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass

    def _stop(self, process: subprocess.Popen) -> None:
        for sig, grace in ((signal.SIGINT, self.stop_grace), (signal.SIGTERM, 2), (signal.SIGKILL, 5)):
            self._signal(process.pid, sig)
            try:
                process.wait(timeout=grace)
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            raise RuntimeError(f"Explorer process group {process.pid} did not stop")
        # A child could outlive its group leader. Stop the entire owned group
        # before the device lock can be released or an environment reused.
        self._signal(process.pid, signal.SIGKILL)

    def run(self, command: list[str], output: Path, *, lock_fd: int | tuple[int, ...],
            check: Callable[[], None], on_start: Callable[[int], None]) -> int:
        check()
        with output.open("ab", buffering=0) as log:
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True, pass_fds=(lock_fd,) if isinstance(lock_fd, int) else lock_fd,
            )
            try:
                on_start(process.pid)
                while True:
                    check()
                    try:
                        return process.wait(timeout=self.poll_interval)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                self._stop(process)
