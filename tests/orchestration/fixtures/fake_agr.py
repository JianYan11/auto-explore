"""AGR wire-contract fixture; no credentials or network required."""

import json
import os
import sys
import time
from pathlib import Path

settings = json.loads(Path(os.environ["FAKE_AGR_SETTINGS"]).read_text())
arguments = sys.argv[sys.argv.index("instance"):]
Path(os.environ["FAKE_AGR_CALL"]).write_text(json.dumps({
    "arguments": sys.argv[1:], "adb": os.environ.get("ADB_PATH"), "path": os.environ.get("PATH"),
}))
if settings.get("sleep"):
    time.sleep(60)
if "raw" in settings:
    print(settings["raw"])
else:
    result = {"SchemaVersion": "agr.v1", "Status": "succeeded", "Data": settings.get("data", {}), "Failure": None}
    if settings.get("failure"):
        result.update(Status="failed", Failure=settings["failure"])
    print(json.dumps(result))
sys.exit(settings.get("returncode", 0))
