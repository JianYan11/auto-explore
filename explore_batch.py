#!/usr/bin/env python3
"""Run independent App explorations on connected cloud sandbox devices."""

from runtime_config import load_environment

load_environment()

from orchestration.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
