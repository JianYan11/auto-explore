"""Managed sandbox lifecycle, with durable intent and compensating cleanup."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .errors import EnvironmentFailure, SandboxCreationRejected
from .locks import FileLock, device_lock_path
from .models import Assignment, positive_seconds
from .ports import CloudResourceRepository, DeviceLease, EnvironmentManager, PreparedEnvironment, SandboxProvider


LOG = logging.getLogger(__name__)


class CloudEnvironmentManager:
    """One new instance per attempt; never adopt or stop unrelated instances."""

    def __init__(self, store: CloudResourceRepository, provider: SandboxProvider, adb: EnvironmentManager,
                 *, provision_timeout: float = 180, cleanup_timeout: float = 120,
                 poll_interval: float = 1, lock_root: Path | None = None):
        self.store, self.provider, self.adb = store, provider, adb
        self.provision_timeout = positive_seconds(provision_timeout, "provision_timeout")
        self.cleanup_timeout = positive_seconds(cleanup_timeout, "cleanup_timeout")
        self.poll_interval = positive_seconds(poll_interval, "cloud poll_interval")
        self.lock_root = lock_root
        self._leases: dict[str, DeviceLease] = {}

    def _pause(self, deadline: float, check: Callable[[], None]) -> None:
        check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EnvironmentFailure("Sandbox lifecycle deadline exceeded")
        time.sleep(min(self.poll_interval, remaining))

    def prepare(self, assignment: Assignment, deadline: float, check: Callable[[], None]) -> PreparedEnvironment:
        check()
        # TTL is a fallback for timed tools; persistent tools require explicit stop.
        ttl = math.ceil(assignment.spec.timeout_seconds + self.cleanup_timeout)
        resource = self.store.begin_cloud_resource(assignment, ttl)
        end = min(deadline, time.monotonic() + self.provision_timeout)
        check()
        self.store.update_cloud_resource(assignment.attempt_id, state="creating")
        try:
            # Once allocation is dispatched, let this bounded RPC return its ID
            # before honoring cancellation. Killing it mid-response unnecessarily
            # turns an ordinary cancel into an ambiguous cloud allocation.
            instance_id = self.provider.create(resource, end, lambda: None)
        except SandboxCreationRejected:
            self.store.update_cloud_resource(assignment.attempt_id, state="planned")
            raise
        # Persist before any wait, cancellation check or ADB call can fail.
        self.store.update_cloud_resource(assignment.attempt_id, state="starting", instance_id=instance_id)
        LOG.info("Created sandbox %s for attempt %s", instance_id, assignment.attempt_id)
        while True:
            check()
            state = self.provider.state(instance_id, resource["region"], end, check)
            if state == "RUNNING":
                break
            if state != "STARTING":
                raise EnvironmentFailure(f"Sandbox {instance_id} cannot start: {state}")
            self._pause(end, check)
        self.store.update_cloud_resource(assignment.attempt_id, state="connecting")
        connection = self.provider.connect(instance_id, resource["region"], end, check)
        self.store.update_cloud_resource(assignment.attempt_id, adb_serial=connection.device, tunnel_pid=connection.tunnel_pid)
        # Protect the actual transport too, so the legacy/external device path
        # cannot run a second explorer against this instance.
        lease = FileLock(device_lock_path(connection.device, self.lock_root))
        self._leases[assignment.device] = lease
        self.adb.prepare(replace(assignment, device=connection.device), deadline, check)
        self.store.update_cloud_resource(assignment.attempt_id, state="ready")
        return PreparedEnvironment(connection.device, instance_id, lease, connection.tunnel_pid)

    def _release(self, resource: dict) -> None:
        attempt_id = resource["attempt_id"]
        deadline = time.monotonic() + self.cleanup_timeout
        try:
            instance_id = resource["instance_id"]
            if not instance_id and resource["state"] == "planned":
                self.store.update_cloud_resource(attempt_id, state="released", error="")
                return
            if not instance_id:
                matches = self.provider.find(resource, deadline)
                if len(matches) != 1:
                    raise EnvironmentFailure("Create outcome is unknown; expected exactly one instance with this attempt's metadata. "
                                             "Retained journal for env cleanup; no create was replayed")
                instance_id = matches[0]
                self.store.update_cloud_resource(attempt_id, instance_id=instance_id)
            self.store.update_cloud_resource(attempt_id, state="releasing", error="")
            tunnel_error = None
            try:
                self.provider.disconnect(instance_id, resource["region"],
                                         min(deadline, time.monotonic() + self.cleanup_timeout / 3))
            except Exception as exc:
                # An unhealthy local tunnel must not prevent stopping a billable
                # cloud instance. Keep a cleanup debt if either operation fails.
                tunnel_error = exc
            if self.provider.state(instance_id, resource["region"], deadline, lambda: None) != "STOPPED":
                try:
                    self.provider.delete(instance_id, resource["region"], deadline)
                except EnvironmentFailure:
                    # A retry may race the previous stop's completion. Terminal
                    # state, rather than a repeated delete's exit code, is authoritative.
                    if self.provider.state(instance_id, resource["region"], deadline, lambda: None) != "STOPPED":
                        raise
            while self.provider.state(instance_id, resource["region"], deadline, lambda: None) != "STOPPED":
                self._pause(deadline, lambda: None)
            if tunnel_error is not None:
                raise EnvironmentFailure(f"Cloud instance stopped but tunnel cleanup failed: {tunnel_error}")
            self.store.update_cloud_resource(attempt_id, state="released", error="")
            LOG.info("Released sandbox %s", instance_id)
        except Exception as exc:
            self.store.update_cloud_resource(attempt_id, state="cleanup_pending", error=str(exc))
            raise

    def reset(self, serial: str, package: str, policy: str) -> bool:
        # This is intentionally independent of the cancelled/expired task check.
        try:
            for resource in self.store.cloud_resources():
                if resource["environment_id"] == serial:
                    self._release(resource)
            return True  # The next attempt gets a fresh instance, even after a failed boot.
        finally:
            lease = self._leases.pop(serial, None)
            if lease is not None:
                lease.close()

    def reconcile(self, environments: list[dict]) -> None:
        """Call under the coordinator lock, after abandoned attempts are marked lost."""
        for env in environments:
            if env["provider"] != "tencent":
                continue
            # A SIGKILL'ed coordinator may leave a running explorer inheriting the
            # slot lock. Never delete its sandbox underneath it.
            try:
                with FileLock(device_lock_path(env["serial"], self.lock_root)):
                    self.reset(env["serial"], "", env["reset_policy"])
                    if env["state"] == "quarantined":
                        self.store.recover_environment(env["serial"])
            except (EnvironmentFailure, OSError, RuntimeError) as exc:
                LOG.error("Cloud recovery for %s: %s", env["serial"], exc)
                self.store.quarantine(env["serial"], str(exc))
