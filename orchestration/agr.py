"""Tencent AGR CLI adapter. Credentials remain in AGR's own configuration."""

from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .errors import EnvironmentFailure, SandboxCreationRejected
from .locks import FileLock, LockBusy
from .models import positive_seconds
from .ports import PreparedEnvironment


ATTEMPT_METADATA = "auto-explore-attempt"


def is_owned_tunnel(pid: int, executable: str, instance_id: str) -> bool:
    """Verify executable AND exact tunnel argument, including paths with spaces.

    AGR 0.7.0 splits macOS argv[0] on spaces and cannot stop such a daemon.
    Fail closed on unsupported platforms or any unverifiable process identity.
    """
    try:
        if sys.platform == "darwin":
            buffer = ctypes.create_string_buffer(4096)
            library = ctypes.CDLL("/usr/lib/libproc.dylib")
            if library.proc_pidpath(pid, buffer, len(buffer)) <= 0:
                return False
            actual = os.fsdecode(buffer.value)
        else:
            actual = os.readlink(f"/proc/{pid}/exe")
        if Path(actual).resolve() != Path(executable).resolve():
            return False
        result = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                                capture_output=True, text=True, timeout=2, check=False)
        command = result.stdout.strip()
        # Do not tokenize argv[0] on whitespace: this is precisely the old AGR bug.
        prefix = executable + " instance mobile tunnel " + instance_id + " "
        return result.returncode == 0 and command.startswith(prefix) and "--daemon" in command[len(prefix):].split()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False


class AgrFailure(EnvironmentFailure):
    def __init__(self, operation: str, code: str):
        # Provider messages/stdout can include endpoint credentials. Persist only
        # the bounded structured error code, never the raw command response.
        code = code if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code) else "UnknownError"
        super().__init__(f"AGR {operation} failed ({code}); inspect AGR diagnostics")
        self.code = code


class AgrSandboxProvider:
    def __init__(self, executable: str, adb: str, *, command_timeout: float = 30,
                 tunnel_lock: Path | None = None):
        self.executable, self.adb = executable, adb
        self.command_timeout = positive_seconds(command_timeout, "cloud command timeout")
        self.tunnel_lock = tunnel_lock or Path.home() / ".cache" / "auto-explore" / "agr-mobile.lock"

    def _call(self, arguments: list[str], region: str, deadline: float,
              check: Callable[[], None] = lambda: None) -> dict:
        check()
        end = min(deadline, time.monotonic() + self.command_timeout)
        if time.monotonic() >= end:
            raise AgrFailure(arguments[0], "DeadlineExceeded")
        environment = os.environ.copy()
        environment["ADB_PATH"] = self.adb
        environment["PATH"] = str(Path(self.adb).parent) + os.pathsep + environment.get("PATH", "")
        command = [self.executable, "--region", region, "--non-interactive", "--no-color", "-o", "json", *arguments]
        try:
            # connect spawns a long-lived daemon. A fresh session lets it outlive
            # the command invocation; on interruption we kill only this session.
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, env=environment, start_new_session=True)
        except OSError as exc:
            raise AgrFailure(arguments[0], type(exc).__name__) from exc
        try:
            while True:
                check()
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise AgrFailure(arguments[0], "DeadlineExceeded")
                try:
                    stdout, _ = process.communicate(timeout=min(0.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pass
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        try:
            envelope = json.loads(stdout)
        except ValueError:
            # Some AGR/ADB combinations emit `disconnected <serial>` before
            # their compact JSON envelope. Accept exactly one structured result.
            candidates = []
            for line in stdout.splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("SchemaVersion") == "agr.v1":
                    candidates.append(item)
            if len(candidates) != 1:
                raise AgrFailure(arguments[0], "InvalidJSON") from None
            envelope = candidates[0]
        if not isinstance(envelope, dict) or envelope.get("SchemaVersion") != "agr.v1":
            raise AgrFailure(arguments[0], "InvalidEnvelope")
        if process.returncode or envelope.get("Status") != "succeeded":
            failure = envelope.get("Failure") or {}
            if not isinstance(failure, dict):
                raise AgrFailure(arguments[0], "InvalidFailure")
            raise AgrFailure(arguments[0], str(failure.get("Code", "CommandFailed")))
        data = envelope.get("Data")
        if not isinstance(data, dict):
            raise AgrFailure(arguments[0], "InvalidData")
        return data

    @staticmethod
    def _instance_id(data: dict) -> str:
        instance_id = data.get("InstanceId")
        if not isinstance(instance_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", instance_id):
            raise AgrFailure("instance", "MissingInstanceId")
        return instance_id

    def create(self, resource: dict, deadline: float, check: Callable[[], None]) -> str:
        try:
            data = self._call([
                "instance", "create", "--tool-id", resource["tool_id"],
                "--timeout", f'{resource["timeout_seconds"]}s', "--client-token", resource["client_token"],
                "--metadata", json.dumps([{"Name": ATTEMPT_METADATA, "Value": resource["attempt_id"]}]),
            ], resource["region"], deadline, check)
        except AgrFailure as exc:
            # These structured control-plane rejections cannot have allocated an
            # instance. Network failures and token conflicts remain ambiguous.
            if (exc.code.startswith(("AuthFailure", "UnauthorizedOperation", "InvalidParameter", "MissingParameter",
                                     "LimitExceeded.", "ResourceInsufficient"))
                    or exc.code in {"RequestLimitExceeded", "ResourceNotFound.SandboxTool", "TOOL_NOT_FOUND"}):
                raise SandboxCreationRejected(str(exc)) from exc
            raise
        return self._instance_id(data)

    def state(self, instance_id: str, region: str, deadline: float, check: Callable[[], None]) -> str:
        try:
            data = self._call(["instance", "get", instance_id], region, deadline, check)
        except AgrFailure as exc:
            if exc.code in {"ResourceNotFound", "INSTANCE_NOT_FOUND"} or exc.code.startswith("ResourceNotFound."):
                return "STOPPED"
            raise
        if self._instance_id(data) != instance_id or not isinstance(data.get("Status"), str):
            raise AgrFailure("get", "InvalidInstance")
        return data["Status"]

    def _mobile(self, arguments: list[str], region: str, deadline: float,
                check: Callable[[], None] = lambda: None) -> dict:
        # Older AGR versions update a shared tunnel registry. Serialize mutations
        # across all auto-explore coordinators, while cloud provisioning stays parallel.
        while True:
            check()
            if time.monotonic() >= deadline:
                raise AgrFailure("mobile", "LockTimeout")
            try:
                lock = FileLock(self.tunnel_lock)
                break
            except LockBusy:
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        with lock:
            return self._call(["instance", "mobile", *arguments], region, deadline, check)

    def connect(self, instance_id: str, region: str, deadline: float, check: Callable[[], None]) -> PreparedEnvironment:
        data = self._mobile(["connect", instance_id], region, deadline, check)
        if self._instance_id(data) != instance_id:
            raise AgrFailure("connect", "InvalidInstance")
        serial = data.get("AdbAddress", "")
        match = re.fullmatch(r"127\.0\.0\.1:([0-9]{1,5})", serial) if isinstance(serial, str) else None
        if not match or not 1 <= int(match[1]) <= 65535:
            raise AgrFailure("connect", "InvalidAdbAddress")
        pid = data.get("Pid")
        if type(pid) is not int or pid < 1:
            raise AgrFailure("connect", "InvalidTunnelPid")
        return PreparedEnvironment(serial, instance_id, tunnel_pid=pid)

    def _tunnels(self, region: str, deadline: float) -> list[dict]:
        data = self._call(["instance", "mobile", "list"], region, deadline)
        items = data.get("Items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise AgrFailure("mobile.list", "InvalidItems")
        return items

    def disconnect(self, instance_id: str, region: str, deadline: float) -> None:
        self._wait_for_connect(instance_id, deadline)
        entries = [item for item in self._tunnels(region, deadline) if item.get("InstanceId") == instance_id]
        if not entries:
            return
        try:
            self._mobile(["disconnect", instance_id], region, deadline)
        except AgrFailure as exc:
            if exc.code in {"INTERNAL_ERROR", "InvalidJSON"}:
                self._stop_verified_tunnel(entries[0], instance_id, deadline)
            # A partially successful disconnect or already-dead daemon is harmless
            # only when the registry confirms this exact instance is absent.
            if any(item.get("InstanceId") == instance_id for item in self._tunnels(region, deadline)):
                raise
        if any(item.get("InstanceId") == instance_id for item in self._tunnels(region, deadline)):
            raise AgrFailure("disconnect", "TunnelStillPresent")

    def _wait_for_connect(self, instance_id: str, deadline: float) -> None:
        # After coordinator SIGKILL, an in-flight AGR connect can still register a
        # daemon. Do not declare its tunnel absent while that command is running.
        pattern = re.compile(r"^" + re.escape(self.executable) +
                             r" .*\binstance mobile connect " + re.escape(instance_id) + r"(?:\s|$)")
        while True:
            if time.monotonic() >= deadline:
                raise AgrFailure("disconnect", "ConnectStillRunning")
            try:
                result = subprocess.run(["ps", "-A", "-o", "args="], capture_output=True, text=True,
                                        timeout=max(0.01, min(2, deadline - time.monotonic())), check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AgrFailure("disconnect", "ProcessInspectionFailed") from exc
            if result.returncode:
                raise AgrFailure("disconnect", "ProcessInspectionFailed")
            if not any(pattern.search(line.strip()) for line in result.stdout.splitlines()):
                return
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def _stop_verified_tunnel(self, entry: dict, instance_id: str, deadline: float) -> None:
        pid = entry.get("Pid")
        if type(pid) is not int or pid <= 0:
            return
        for sig, grace in ((signal.SIGTERM, 3), (signal.SIGKILL, 1)):
            if not is_owned_tunnel(pid, self.executable, instance_id):
                return
            if time.monotonic() >= deadline:
                return
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
            end = min(deadline, time.monotonic() + grace)
            while time.monotonic() < end:
                if not is_owned_tunnel(pid, self.executable, instance_id):
                    return
                time.sleep(min(0.1, max(0, end - time.monotonic())))

    def delete(self, instance_id: str, region: str, deadline: float) -> None:
        self._call(["instance", "delete", instance_id, "--yes", "--ignore-not-found"], region, deadline)

    def find(self, resource: dict, deadline: float) -> list[str]:
        data = self._call(["instance", "list", "--tool-id", resource["tool_id"], "--all"], resource["region"], deadline)
        items = data.get("Items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise AgrFailure("list", "InvalidItems")
        if any(item.get("Metadata") is not None and not isinstance(item["Metadata"], list) for item in items):
            raise AgrFailure("list", "InvalidMetadata")
        # Recover a create whose HTTP response was lost. Do not replay a create
        # during cleanup: an expired provider idempotency window could allocate again.
        return [self._instance_id(item) for item in items
                if {"Name": ATTEMPT_METADATA, "Value": resource["attempt_id"]} in (item.get("Metadata") or [])]
