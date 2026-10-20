#!/usr/bin/env python3
"""Thin executable entry point for the modular exploration harness."""

from runtime_config import load_environment

load_environment()

from harness import (  # re-export legacy names for low-friction migration
    ActionSequenceStep,
    AndroidController,
    AndroidExplorer,
    AppGraph,
    ExplorationAction,
    ExplorerConfig,
    PageState,
    ScreenCapture,
    Transition,
    UIElement,
    VLMAnalyzer,
)
from harness.cli import main

__all__ = [
    "ActionSequenceStep",
    "AndroidController",
    "AndroidExplorer",
    "AppGraph",
    "ExplorationAction",
    "ExplorerConfig",
    "PageState",
    "ScreenCapture",
    "Transition",
    "UIElement",
    "VLMAnalyzer",
    "main",
]


if __name__ == "__main__":
    main()

