"""Task contracts shared by the scheduler, workers, and infrastructure adapters."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any


# Only non-secret, single-task options cross the subprocess boundary. In particular,
# device/output/ADB/log paths cannot be overridden, including via argparse abbreviations.
OPTION_TYPES = {
    "activity": str, "model": str, "summary_model": str, "embedding_model": str,
    "memory_scope": str,
    "max_steps": int, "max_pages": int, "max_depth": int,
    "max_states_per_page": int, "max_actions_per_state": int,
    "continuous_steps": int, "max_replay_steps": int,
    "coverage_checkpoint_steps": int, "coverage_checkpoint_top_k": int,
    "verification_retries": int, "replay_attempts": int,
    "screenshot_delay": float, "summary_budget_seconds": float,
    "summary_timeout": float, "embedding_timeout": float,
    "no_semantic_dedup": bool, "page_exploration": bool,
    "no_save_analysis": bool, "no_back": bool,
}
SUCCESS_REASONS = frozenset({"frontier_exhausted", "budget_limited"})


def positive_seconds(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


@dataclass(frozen=True)
class TaskSpec:
    package: str
    options: dict[str, Any] = field(default_factory=dict)
    devices: tuple[str, ...] = ()
    priority: int = 0
    timeout_seconds: float = 1800
    max_attempts: int = 1
    retry_delay_seconds: float = 5
    resource_key: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.package, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", self.package
        ):
            raise ValueError("package must be an Android package name")
        positive_seconds(self.timeout_seconds, "timeout_seconds")
        if (type(self.priority) is not int or not -(2**63) <= self.priority < 2**63
                or type(self.max_attempts) is not int or not 1 <= self.max_attempts < 2**31):
            raise ValueError("priority must fit a signed 64-bit integer; max_attempts must be a positive 32-bit integer")
        if (isinstance(self.retry_delay_seconds, bool)
                or not isinstance(self.retry_delay_seconds, (int, float))
                or not math.isfinite(self.retry_delay_seconds) or self.retry_delay_seconds < 0):
            raise ValueError("retry_delay_seconds must be a finite nonnegative number")
        if (not isinstance(self.devices, (list, tuple))
                or any(not isinstance(d, str) or not d.strip() or d.startswith("-") or "\x00" in d for d in self.devices)):
            raise ValueError("devices must contain nonempty ADB serials")
        if not isinstance(self.resource_key, str):
            raise ValueError("resource_key must be a string")
        if not isinstance(self.options, dict):
            raise ValueError("options must be an object")
        options = {"max_steps": 200, **self.options}
        for name, value in options.items():
            expected = OPTION_TYPES.get(name)
            if expected is None:
                raise ValueError(f"Unsupported explorer option: {name}")
            valid = (type(value) in (int, float) if expected is float else type(value) is expected)
            if not valid or (isinstance(value, str) and "\x00" in value):
                raise ValueError(f"Invalid value for explorer option: {name}")
            if expected in (int, float) and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and nonnegative")
            if name in {"max_pages", "max_states_per_page", "max_actions_per_state", "continuous_steps",
                        "max_replay_steps", "coverage_checkpoint_top_k", "summary_timeout", "embedding_timeout"} and value <= 0:
                raise ValueError(f"{name} must be positive")
        object.__setattr__(self, "options", options)
        object.__setattr__(self, "devices", tuple(dict.fromkeys(self.devices)))

    @classmethod
    def from_dict(cls, value: dict) -> TaskSpec:
        if not isinstance(value, dict):
            raise ValueError("Each task must be an object")
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown or "package" not in value:
            raise ValueError(f"Task requires package; unknown fields: {sorted(unknown)}")
        return cls(**value)


@dataclass(frozen=True)
class Assignment:
    task_id: str
    attempt_id: str
    attempt_no: int
    device: str
    reset_policy: str
    owner: str
    output_dir: str
    spec: TaskSpec


@dataclass(frozen=True)
class Outcome:
    state: str
    reason: str
    error: str = ""
    retryable: bool = False
    returncode: int | None = None
