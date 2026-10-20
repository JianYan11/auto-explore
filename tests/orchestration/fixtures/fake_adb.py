#!/usr/bin/env python3
"""Small deterministic ADB fixture used only by offline integration tests."""

import json
import os
import sys

serial, command = sys.argv[2], sys.argv[3:]
log = os.environ.get("FAKE_ADB_EVENTS")
if log:
    fd = os.open(log, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    os.write(fd, (json.dumps({"serial": serial, "command": command}) + "\n").encode())
    os.close(fd)
if serial == "offline":
    sys.exit(1)
if command == ["get-state"]:
    print("device")
elif command == ["shell", "getprop", "sys.boot_completed"]:
    print("1")
elif command[:3] == ["shell", "pm", "path"]:
    if command[-1] == "com.example.missing":
        sys.exit(1)
    print("package:/data/app/base.apk")
elif command[:3] == ["shell", "pm", "clear"]:
    print("Failed" if command[-1] == "com.example.missing" else "Success")
