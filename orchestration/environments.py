"""ADB readiness and App checks shared by managed and externally connected sandboxes."""

from __future__ import annotations

import subprocess
import time
from typing import Callable

from .errors import EnvironmentFailure
from .models import Assignment, positive_seconds
from .ports import PreparedEnvironment


class AdbEnvironmentManager:
    def __init__(self, adb: str, *, boot_timeout: float = 90, command_timeout: float = 10):
        self.adb = adb
        self.boot_timeout = positive_seconds(boot_timeout, "boot_timeout")
        self.command_timeout = positive_seconds(command_timeout, "command_timeout")

    def _adb(self, serial: str, *args: str, timeout: float | None = None,
             allow_empty_exit_one: bool = False) -> str:
        try:
            result = subprocess.run(
                [self.adb, "-s", serial, *args], capture_output=True, text=True,
                timeout=timeout or self.command_timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EnvironmentFailure(f"ADB {args[0]} failed: {type(exc).__name__}") from exc
        # Android's `pm path` reports an absent package with exit 1 and no output.
        # ADB transport errors have diagnostics and must still quarantine devices.
        empty_exit_one = (allow_empty_exit_one and result.returncode == 1
                          and not result.stdout.strip() and not result.stderr.strip())
        if result.returncode and not empty_exit_one:
            raise EnvironmentFailure(f"ADB {args[0]} exited {result.returncode}: {result.stderr.strip()[:1000]}")
        return result.stdout.strip()

    def prepare(self, assignment: Assignment, deadline: float, check: Callable[[], None]) -> PreparedEnvironment:
        boot_deadline = min(deadline, time.monotonic() + self.boot_timeout)
        last_error = "Android is not booted"
        while time.monotonic() < boot_deadline:
            check()
            try:
                timeout = max(0.01, min(self.command_timeout, boot_deadline - time.monotonic()))
                if self._adb(assignment.device, "get-state", timeout=timeout) == "device":
                    check()
                    timeout = max(0.01, min(self.command_timeout, boot_deadline - time.monotonic()))
                    if self._adb(assignment.device, "shell", "getprop", "sys.boot_completed", timeout=timeout) == "1":
                        break
            except EnvironmentFailure as exc:
                last_error = str(exc)
            time.sleep(min(0.2, max(0, boot_deadline - time.monotonic())))
        else:
            check()
            raise EnvironmentFailure(f"Device {assignment.device} not ready: {last_error}")
        check()
        package = self._adb(assignment.device, "shell", "pm", "path", assignment.spec.package,
                            timeout=max(0.01, min(self.command_timeout, deadline - time.monotonic())),
                            allow_empty_exit_one=True)
        if not any(line.startswith("package:") for line in package.splitlines()):
            raise EnvironmentFailure(f"Package not installed on {assignment.device}: {assignment.spec.package}", quarantine=False)
        check()
        if assignment.reset_policy == "clear-data":
            self._clear(assignment.device, assignment.spec.package,
                        timeout=max(0.01, min(self.command_timeout, deadline - time.monotonic())))
        check()

        return PreparedEnvironment(assignment.device)

    def _clear(self, serial: str, package: str, *, timeout: float | None = None) -> None:
        if self._adb(serial, "shell", "pm", "clear", package, timeout=timeout) != "Success":
            raise EnvironmentFailure(f"Could not clear app data: {package}")

    def reset(self, serial: str, package: str, policy: str) -> None:
        if self._adb(serial, "get-state") != "device":
            raise EnvironmentFailure(f"Device disconnected: {serial}")
        if self._adb(serial, "shell", "getprop", "sys.boot_completed") != "1":
            raise EnvironmentFailure(f"Android is not booted: {serial}")
        if package:
            self._adb(serial, "shell", "am", "force-stop", package)
            if policy == "clear-data":
                self._clear(serial, package)
