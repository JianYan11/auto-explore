"""Configuration shared by the Android exploration harness."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _float_env(*names: str, default: float) -> float:
    value = _first_env(*names)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


QWEN_COORDINATE_SCALE = 1000
QWEN_RESIZE_FACTOR = 28
QWEN_MIN_PIXELS = 3136
QWEN_MAX_PIXELS = 10035200


@dataclass
class ExplorerConfig:
    """Runtime configuration for one exploration run."""

    # ADB settings
    adb_path: str = _first_env(
        "ADB_PATH",
        default="D:\\Android\\SDK\\platform-tools\\adb.exe",
    )
    device_name: str = _first_env("ANDROID_SERIAL", 
                                  default="emulator-5554"
                                #   default="96234697" 
    )
    adb_timeout: float = 30.0

    # App settings
    app_package: str = ""
    app_activity: str = ""

    # Exploration settings
    max_pages: int = 100
    max_depth: int = 10
    max_actions_per_page: int = 0
    max_steps: int = 0  # Zero preserves the original unlimited action budget.
    state_exploration: bool = True
    max_states_per_page: int = 16
    max_actions_per_state: int = 20
    continuous_steps: int = 4
    max_replay_steps: int = 100
    max_transition_repeats: int = 2
    coverage_checkpoint_steps: int = 50  # Zero disables periodic graph audits.
    coverage_checkpoint_top_k: int = 15
    graph_memory: bool = True
    memory_graph_paths: list[str] = field(default_factory=list)
    memory_top_k: int = 8
    memory_max_chars: int = 12000
    memory_scope: str = ""  # Optional App version / test environment identifier.
    verification_retries: int = 1  # Bounded revisits of unresolved actions, within page/step budgets.
    # Empty summary_model preserves deterministic evidence memory without an LLM summary.
    summary_model: str = _first_env("SUMMARY_MODEL", default = "Pro/moonshotai/Kimi-K2.6")
    summary_api_key: str = field(default_factory=lambda: _first_env("SUMMARY_API_KEY"), repr=False)
    summary_timeout: float = 1000
    summary_budget_seconds: float = 120.0
    summary_max_chars: int = 12000
    screenshot_delay: float = 10.0
    replay_attempts: int = 2
    back_after_action: bool = True

    # Screenshot identity settings. Ratios remove Android system chrome before
    # calculating a perceptual hash and conservative pixel similarity.
    fingerprint_crop_top_ratio: float = 0.07
    fingerprint_crop_bottom_ratio: float = 0.05
    visual_hash_distance_threshold: int = 3
    visual_mean_difference_threshold: float = 0.00025

    # VLM settings
    vlm_base_url: str = _first_env(
        "QWEN_BASE_URL",
        "MODELSCOPE_BASE_URL",
        "OPENAI_BASE_URL", 
        # default="https://www.cctq.ai/v1",
        default="https://api.siliconflow.cn/v1",
    )
    # Deliberately no source-code fallback: credentials must be injected.
    vlm_api_key: str = _first_env(
        "QWEN_API_KEY",
        "MODELSCOPE_API_KEY",
        "OPENAI_API_KEY",
    )
    model: str = _first_env("QWEN_MODEL", default="Qwen/Qwen3.8-27B")
    vlm_timeout: float = 1200.0
    vlm_max_image_side: int = 2048
    vlm_compare_max_attempts: int = 3
    vlm_compare_retry_delay: float = 1.0

    # Blank embedding endpoint/key inherit the VLM settings.
    semantic_dedup: bool = True
    embedding_model: str = _first_env("EMBEDDING_MODEL", default="BAAI/bge-m3")
    embedding_base_url: str = _first_env("EMBEDDING_BASE_URL")
    embedding_api_key: str = _first_env("EMBEDDING_API_KEY")
    embedding_timeout: float = 60.0
    semantic_similarity_threshold: float = _float_env(
        "SEMANTIC_SIMILARITY_THRESHOLD", default=0.8
    )

    # Output settings
    output_dir: str = "./output/android_exploration"
    save_screenshots: bool = True
    save_analysis: bool = True
