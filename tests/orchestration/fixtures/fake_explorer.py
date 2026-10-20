"""A real subprocess emulating the output contract of the exploration CLI."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--device")
parser.add_argument("--package")
parser.add_argument("--output", type=Path)
parser.add_argument("--mode", default="success")
args, _ = parser.parse_known_args()
events = os.environ.get("FAKE_EXPLORE_EVENTS")


def event(kind):
    if events:
        fd = os.open(events, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.write(fd, (json.dumps({"kind": kind, "device": args.device, "package": args.package,
                                 "pid": os.getpid(), "time": time.time()}) + "\n").encode())
        os.close(fd)


event("start")
if args.mode == "spawn":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (args.output / "child.pid").write_text(str(child.pid))
if args.mode == "sleep" or args.mode == "spawn":
    time.sleep(60)
time.sleep(float(os.environ.get("FAKE_EXPLORE_DELAY", "0.15")))
if args.mode == "exit":
    sys.exit(1)
if args.mode == "invalid":
    (args.output / "app_graph.json").write_text("{}")
else:
    (args.output / "app_graph.json").write_text(json.dumps({
        "app_package": args.package, "run_id": "fake-run", "root_page_id": "root",
        "pages": {"root": {"page_id": "root"}},
        "termination": {"reason": "blocked" if args.mode == "blocked" else "budget_limited"},
    }))
event("end")
