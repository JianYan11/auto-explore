"""CLI composition root: wire local adapters without importing the VLM harness."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import signal
import sqlite3
import sys
from pathlib import Path
from threading import Event

from runtime_config import default_adb

from .environments import AdbEnvironmentManager
from .locks import FileLock, device_lock_path
from .models import TaskSpec, positive_seconds
from .process_runner import ProcessRunner
from .scheduler import Scheduler
from .store import TaskStore
from .worker import Worker


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

    run = commands.add_parser("run", help="Drain the queue with bounded parallelism")
    run.add_argument("--adb", default=default_adb())
    run.add_argument("--output", type=Path, default=None, help="Attempt artifacts; defaults to <database directory>/artifacts")
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument("--boot-timeout", type=float, default=90)
    run.add_argument("--adb-timeout", type=float, default=10)
    run.add_argument("--stop-grace", type=float, default=10, help="Seconds for explorer SIGINT finalization before escalation")
    return parser


def resolve_adb(value: str) -> str:
    executable = shutil.which(value)
    if executable is None:
        raise ValueError(f"ADB executable not found: {value}; pass --adb /absolute/path/to/adb")
    return str(Path(executable).resolve())


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
            else:
                # Recovery cannot race a running coordinator or surviving explorer.
                with FileLock(Path(str(store.path) + ".coordinator.lock")):
                    store.recover_interrupted()
                    env = next((e for e in store.snapshot()["environments"] if e["serial"] == args.device), None)
                    if env is None or env["state"] != "quarantined":
                        raise ValueError("Register the device first; only quarantined environments need recovery")
                    with FileLock(device_lock_path(args.device)):
                        AdbEnvironmentManager(resolve_adb(args.adb)).reset(
                            args.device, env["last_package"], env["reset_policy"])
                        store.recover_environment(args.device)
            result = {"environments": store.snapshot()["environments"]}
        else:
            adb = resolve_adb(args.adb)
            positive_seconds(args.stop_grace, "stop_grace")
            environments = AdbEnvironmentManager(adb, boot_timeout=args.boot_timeout, command_timeout=args.adb_timeout)
            stop = Event()
            worker = Worker(store, environments, adb, stop, runner=ProcessRunner(stop_grace=args.stop_grace))
            scheduler = Scheduler(store, worker, args.output or store.path.parent / "artifacts", stop,
                                  concurrency=args.concurrency)
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
