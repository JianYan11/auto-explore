from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
import signal
import ctypes
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from orchestration.agr import ATTEMPT_METADATA, AgrFailure, AgrSandboxProvider, is_owned_tunnel
from orchestration.errors import ExecutionAborted, SandboxCreationRejected
from tests.orchestration.test_orchestration import FIXTURES, write_python_executable


class AgrAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        executable = self.root / "agr"
        write_python_executable(executable, (FIXTURES / "fake_agr.py").read_text())
        self.settings, self.call = self.root / "settings.json", self.root / "call.json"
        self.provider = AgrSandboxProvider(str(executable), str(self.root / "adb"), command_timeout=2,
                                           tunnel_lock=self.root / "mobile.lock")
        env = patch.dict(os.environ, {"FAKE_AGR_SETTINGS": str(self.settings), "FAKE_AGR_CALL": str(self.call)})
        env.start()
        self.addCleanup(env.stop)
        self.resource = {"attempt_id": "attempt-1", "tool_id": "sdt-test", "region": "ap-guangzhou",
                         "client_token": "idempotency-token", "timeout_seconds": 300}

    def respond(self, data=None, **settings):
        self.settings.write_text(json.dumps({"data": data, **settings}))

    def deadline(self):
        return time.monotonic() + 3

    def test_create_maps_official_envelope_and_preserves_idempotency_and_adb_path(self):
        self.respond({"InstanceId": "ins-123", "Status": "STARTING"})
        with patch("orchestration.agr.subprocess.Popen", wraps=subprocess.Popen) as popen:
            instance_id = self.provider.create(self.resource, self.deadline(), lambda: None)
        self.assertEqual(instance_id, "ins-123")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        call = json.loads(self.call.read_text())
        arguments = call["arguments"]
        self.assertEqual(arguments[arguments.index("--tool-id") + 1], "sdt-test")
        self.assertEqual(arguments[arguments.index("--timeout") + 1], "300s")
        self.assertEqual(arguments[arguments.index("--client-token") + 1], "idempotency-token")
        self.assertEqual(json.loads(arguments[arguments.index("--metadata") + 1]),
                         [{"Name": ATTEMPT_METADATA, "Value": "attempt-1"}])
        self.assertNotIn("--wait", arguments)
        self.assertEqual(call["adb"], str(self.root / "adb"))
        self.assertTrue(call["path"].startswith(str(self.root) + os.pathsep))

    def test_connect_requires_matching_instance_and_loopback_address(self):
        self.respond({"InstanceId": "ins-123", "AdbAddress": "127.0.0.1:15001", "Port": 15001, "Pid": 123})
        self.assertEqual(self.provider.connect("ins-123", "ap-guangzhou", self.deadline(), lambda: None).device,
                         "127.0.0.1:15001")
        for response in ({"InstanceId": "another", "AdbAddress": "127.0.0.1:15001"},
                         {"InstanceId": "ins-123", "AdbAddress": "remote.example:5555"},
                         {"InstanceId": "ins-123", "AdbAddress": "127.0.0.1:99999"}):
            self.respond(response)
            with self.assertRaises(AgrFailure):
                self.provider.connect("ins-123", "ap-guangzhou", self.deadline(), lambda: None)

    def test_provider_errors_are_redacted_and_real_agr_not_found_is_terminal(self):
        self.respond(failure={"Code": "INSTANCE_NOT_FOUND", "Message": "credential=secret"}, returncode=1)
        self.assertEqual(self.provider.state("ins-123", "ap-guangzhou", self.deadline(), lambda: None), "STOPPED")
        self.respond(failure={"Code": "ACCESS_DENIED", "Message": "credential=secret"}, returncode=1)
        with self.assertRaises(AgrFailure) as raised:
            self.provider.state("ins-123", "ap-guangzhou", self.deadline(), lambda: None)
        self.assertIn("ACCESS_DENIED", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_malformed_or_incomplete_response_is_never_success(self):
        for settings in ({"raw": "not json"}, {"raw": '{"Data":{"InstanceId":"x"}}'},
                         {"data": {}}, {"data": []}, {"failure": "invalid", "returncode": 1}):
            self.respond(**settings)
            with self.assertRaises(AgrFailure):
                self.provider.create(self.resource, self.deadline(), lambda: None)

    def test_ambiguous_create_search_matches_only_exact_attempt_metadata(self):
        self.respond({"Items": [
            {"InstanceId": "unrelated", "Metadata": []},
            {"InstanceId": "owned", "Metadata": [{"Name": ATTEMPT_METADATA, "Value": "attempt-1"}]},
            {"InstanceId": "other-attempt", "Metadata": [{"Name": ATTEMPT_METADATA, "Value": "attempt-2"}]},
        ]})
        self.assertEqual(self.provider.find(self.resource, self.deadline()), ["owned"])
        self.assertIn("--all", json.loads(self.call.read_text())["arguments"])

    def test_delete_targets_only_owned_id_and_disconnect_absent_is_idempotent(self):
        self.respond({"Deleted": 1})
        self.provider.delete("ins-123", "ap-guangzhou", self.deadline())
        arguments = json.loads(self.call.read_text())["arguments"]
        self.assertEqual(arguments[arguments.index("instance"):],
                         ["instance", "delete", "ins-123", "--yes", "--ignore-not-found"])
        self.respond({"Items": [{"InstanceId": "unrelated"}]})
        self.provider.disconnect("ins-123", "ap-guangzhou", self.deadline())
        self.assertEqual(json.loads(self.call.read_text())["arguments"][-3:], ["instance", "mobile", "list"])

    def test_command_timeout_and_cancellation_reap_the_owned_process(self):
        self.respond(sleep=True)
        real_popen = subprocess.Popen
        for cancel in (False, True):
            self.provider.command_timeout = 0.15
            started = []
            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                started.append(process)
                return process
            def check():
                if cancel and started:
                    raise ExecutionAborted("cancelled")
            with patch("orchestration.agr.subprocess.Popen", side_effect=launch):
                with self.assertRaises(ExecutionAborted if cancel else AgrFailure):
                    self.provider.create(self.resource, self.deadline(), check)
            self.assertIsNotNone(started[0].poll())

    def test_adb_stdout_prefix_is_accepted_but_multiple_envelopes_are_rejected(self):
        result = json.dumps({"SchemaVersion": "agr.v1", "Status": "succeeded", "Data": {"InstanceId": "ins-123"}})
        self.respond(raw="disconnected 127.0.0.1:15001\n" + result)
        self.assertEqual(self.provider.create(self.resource, self.deadline(), lambda: None), "ins-123")
        self.respond(raw=result + "\n" + result)
        with self.assertRaises(AgrFailure):
            self.provider.create(self.resource, self.deadline(), lambda: None)

    def test_rejected_creation_is_distinguished_from_unknown_network_outcome(self):
        self.respond(failure={"Code": "LimitExceeded.Instances"}, returncode=1)
        with self.assertRaises(SandboxCreationRejected):
            self.provider.create(self.resource, self.deadline(), lambda: None)
        self.respond(failure={"Code": "ClientError.NetworkError"}, returncode=1)
        with self.assertRaises(AgrFailure):
            self.provider.create(self.resource, self.deadline(), lambda: None)

    def test_mac_process_identity_handles_spaces_and_rejects_other_instance(self):
        executable = str(self.root / "directory with spaces" / "agr")
        def pidpath(pid, buffer, size):
            value = os.fsencode(executable) + b"\0"
            ctypes.memmove(buffer, value, len(value))
            return len(value)
        command = executable + " instance mobile tunnel ins-123 --daemon --port=0"
        with patch("orchestration.agr.sys.platform", "darwin"), \
             patch("orchestration.agr.ctypes.CDLL", return_value=SimpleNamespace(proc_pidpath=pidpath)), \
             patch("orchestration.agr.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=command)):
            self.assertTrue(is_owned_tunnel(123, executable, "ins-123"))
            self.assertFalse(is_owned_tunnel(123, executable, "ins-12"))
            self.assertFalse(is_owned_tunnel(123, executable + "-other", "ins-123"))

    def test_fallback_never_signals_unverified_pid_and_rechecks_before_escalation(self):
        with patch("orchestration.agr.is_owned_tunnel", return_value=False), patch("orchestration.agr.os.kill") as kill:
            self.provider._stop_verified_tunnel({"Pid": 123}, "ins-123", self.deadline())
            kill.assert_not_called()
        with patch("orchestration.agr.is_owned_tunnel", side_effect=[True, False]), \
             patch("orchestration.agr.os.kill") as kill:
            self.provider._stop_verified_tunnel({"Pid": 123}, "ins-123", self.deadline())
            kill.assert_called_once_with(123, signal.SIGTERM)

    def test_failed_agr_disconnect_uses_verified_cleanup_then_confirms_registry_absent(self):
        entry = {"InstanceId": "ins-123", "Pid": 123}
        with patch.object(self.provider, "_tunnels", side_effect=[[entry], [], []]), \
             patch.object(self.provider, "_mobile", side_effect=AgrFailure("disconnect", "INTERNAL_ERROR")), \
             patch.object(self.provider, "_stop_verified_tunnel") as stop:
            self.provider.disconnect("ins-123", "ap-guangzhou", self.deadline())
            stop.assert_called_once()

    def test_recovery_waits_for_inflight_connect_and_never_reports_premature_release(self):
        command = self.provider.executable + " --region ap-guangzhou instance mobile connect ins-123"
        with patch("orchestration.agr.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=command)):
            with self.assertRaisesRegex(AgrFailure, "ConnectStillRunning"):
                self.provider.disconnect("ins-123", "ap-guangzhou", time.monotonic() + 0.02)
        with patch("orchestration.agr.subprocess.run", side_effect=[
                SimpleNamespace(returncode=0, stdout=command), SimpleNamespace(returncode=0, stdout="")]):
            self.provider._wait_for_connect("ins-123", self.deadline())
