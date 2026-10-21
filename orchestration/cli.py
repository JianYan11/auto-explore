"""CLI composition root: wire local adapters without importing the VLM harness."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import sys
from pathlib import Path
from threading import Event

from runtime_config import default_adb

from .environments import AdbEnvironmentManager
from .agr import AgrSandboxProvider
from .cloud_environments import CloudEnvironmentManager
from .locks import FileLock, device_lock_path
from .models import TaskSpec, positive_seconds
from .process_runner import ProcessRunner
from .scheduler import Scheduler
from .store import TaskStore
from .worker import Worker


def cloud_options(parser: argparse.ArgumentParser) -> None:
    bundled = Path(__file__).resolve().parent.parent / ".tools" / "bin" / "agr"
    parser.add_argument("--agr", default=os.environ.get("AGR_PATH") or (str(bundled) if bundled.is_file() else "agr"))
    parser.add_argument("--cloud-command-timeout", type=float, default=30)
    parser.add_argument("--cleanup-timeout", type=float, default=120)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Single-host Android exploration queue (macOS/Linux)", allow_abbrev=False)
    parser.add_argument("--db", type=Path, default=Path("output/batch/queue.sqlite3"), help="Persistent local SQLite queue")
    commands = parser.add_subparsers(dest="command", required=True)

    enqueue = commands.add_parser("enqueue", help="Atomically enqueue a JSON array of task specs")
    enqueue.add_argument("manifest", type=Path)
    commands.add_parser("status", help="Print tasks, attempts, and environments as JSON")
    for name in ("cancel", "retry"):
        command = commands.add_parser(name, help=f"{name.capitalize()} one task")
        command.add_argument("task_id")

    env = commands.add_parser("env", help="Register and recover connected sandbox devices")
    env_commands = env.add_subparsers(dest="env_command", required=True)
    add = env_commands.add_parser("add")
    add.add_argument("--device", required=True)
    add.add_argument("--reset-policy", choices=("restart", "clear-data"), default="restart",
                     help="restart preserves app data; clear-data deletes target app data before and after each task")
    recover = env_commands.add_parser("recover", help="Check/reset a quarantined environment after old exploration has stopped")
    recover.add_argument("--device", required=True)
    recover.add_argument("--adb", default=default_adb())
    cleanup = env_commands.add_parser("cleanup", help="Retry release of cloud resources owned by this queue")
    cleanup.add_argument("--adb", default=default_adb())
    cloud_options(cleanup)

    run = commands.add_parser("run", help="Drain the queue with bounded parallelism")
    run.add_argument("--adb", default=default_adb())
    run.add_argument("--output", type=Path, default=None, help="Attempt artifacts; defaults to <database directory>/artifacts")
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument("--boot-timeout", type=float, default=90)
    run.add_argument("--adb-timeout", type=float, default=10)
    run.add_argument("--stop-grace", type=float, default=10, help="Seconds for explorer SIGINT finalization before escalation")
    run.add_argument("--tool-id", default=os.environ.get("CLOUD_SANDBOX_TOOL_ID"),
                     help="Manage fresh Tencent sandbox instances automatically; no env add required")
    run.add_argument("--region", default=os.environ.get("CLOUD_SANDBOX_REGION", "ap-guangzhou"))
    run.add_argument("--provision-timeout", type=float, default=180)
    cloud_options(run)
    return parser


def resolve_adb(value: str) -> str:
    executable = shutil.which(value)
    if executable is None:
        raise ValueError(f"ADB executable not found: {value}; pass --adb /absolute/path/to/adb")
    return str(Path(executable).resolve())


def cloud_manager(args: argparse.Namespace, store: TaskStore, adb: AdbEnvironmentManager) -> CloudEnvironmentManager:
    executable = shutil.which(args.agr)
    if executable is None:
        raise ValueError(f"AGR executable not found: {args.agr}; pass --agr /absolute/path/to/agr")
    return CloudEnvironmentManager(store, AgrSandboxProvider(str(Path(executable).resolve()), adb.adb,
                                   command_timeout=args.cloud_command_timeout), adb,
                                   provision_timeout=getattr(args, "provision_timeout", 180),
                                   cleanup_timeout=args.cleanup_timeout)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    try:
        store = TaskStore(args.db)
        if args.command == "enqueue":
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            if not isinstance(manifest, list) or not manifest:
                raise ValueError("Manifest must be a nonempty JSON array")
            # Validate the entire batch before committing any tasks.
            specs = [TaskSpec.from_dict(value) for value in manifest]
            result = {"task_ids": store.enqueue(specs)}
        elif args.command == "status":
            result = store.snapshot()
        elif args.command in {"cancel", "retry"}:
            getattr(store, args.command)(args.task_id)
            result = {"task_id": args.task_id, "operation": args.command}
        elif args.command == "env":
            if args.env_command == "add":
                store.register_environment(args.device, args.reset_policy)
            elif args.env_command == "cleanup":
                manager = cloud_manager(args, store, AdbEnvironmentManager(resolve_adb(args.adb)))
                with FileLock(Path(str(store.path) + ".coordinator.lock")):
                    store.recover_interrupted()
                    manager.reconcile(store.snapshot()["environments"])
                result = {"cloud_resources": store.cloud_resources(), "environments": store.snapshot()["environments"]}
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 2 if result["cloud_resources"] else 0
            else:
                # Recovery cannot race a running coordinator or surviving explorer.
                with FileLock(Path(str(store.path) + ".coordinator.lock")):
                    store.recover_interrupted()
                    env = next((e for e in store.snapshot()["environments"] if e["serial"] == args.device), None)
                    if env is None or env["state"] != "quarantined":
                        raise ValueError("Register the device first; only quarantined environments need recovery")
                    if env["provider"] != "adb":
                        raise ValueError("Use env cleanup to recover managed cloud environments")
                    with FileLock(device_lock_path(args.device)):
                        AdbEnvironmentManager(resolve_adb(args.adb)).reset(
                            args.device, env["last_package"], env["reset_policy"])
                        store.recover_environment(args.device)
            result = {"environments": store.snapshot()["environments"]}
        else:
            adb = resolve_adb(args.adb)
            positive_seconds(args.stop_grace, "stop_grace")
            environments = AdbEnvironmentManager(adb, boot_timeout=args.boot_timeout, command_timeout=args.adb_timeout)
            startup, selection = None, None
            if args.tool_id:
                if not re.fullmatch(r"sdt-[A-Za-z0-9]+", args.tool_id) or not re.fullmatch(r"[a-z]+-[a-z0-9-]+", args.region):
                    raise ValueError("Invalid Tencent tool ID or region")
                environments = cloud_manager(args, store, environments)
                def startup() -> None:
                    store.ensure_cloud_pool(args.tool_id, args.region, args.concurrency)
                    environments.reconcile(store.snapshot()["environments"])
                selection = lambda env: (env["provider"] == "tencent" and env["tool_id"] == args.tool_id
                                         and env["region"] == args.region)
            stop = Event()
            worker = Worker(store, environments, adb, stop, runner=ProcessRunner(stop_grace=args.stop_grace))
            scheduler = Scheduler(store, worker, args.output or store.path.parent / "artifacts", stop,
                                  concurrency=args.concurrency, startup=startup, environment_filter=selection)
            previous = {}
            def request_stop(signum: int, frame: object) -> None:
                stop.set()
            try:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous[signum] = signal.signal(signum, request_stop)
                result = scheduler.run()
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return result["exit_code"]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        # Keep expected operational errors readable. Child tracebacks are in worker.log.
        print(f"batch: {exc}", file=sys.stderr)
        return 2
