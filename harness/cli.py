"""Command-line configuration and harness construction."""

from __future__ import annotations

import argparse
import math
from typing import Optional, Sequence

from .config import QWEN_RESIZE_FACTOR, ExplorerConfig
from .loop import AndroidExplorer
from .model_api import chat_request_options
from .summary import validate_summary_config
from .terminal_log import capture_terminal_output
from runtime_config import default_adb


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通过 adb 探索 Android app 并生成页面图谱")
    parser.add_argument("--package", required=True, help="App package name") ##这里输入了要探索的app
    parser.add_argument("--activity", default="", help="Main activity；不传则用 monkey 启动")
    parser.add_argument("--device", default=ExplorerConfig().device_name, help="云沙箱的 ADB serial；也可设置 ANDROID_SERIAL")
    parser.add_argument("--adb", default=default_adb(), help="adb 可执行文件路径")
    parser.add_argument("--max-pages", type=int, default=ExplorerConfig.max_pages, help="最大页面数")
    parser.add_argument("--max-depth", type=int, default=ExplorerConfig.max_depth, help="最大探索深度")
    parser.add_argument("--max-steps", type=int, default=ExplorerConfig.max_steps,
                        help="最大探索动作尝试数；0 表示不限，回放/Back 不计入")
    parser.add_argument("--coverage-checkpoint-steps", type=int,
                        default=ExplorerConfig.coverage_checkpoint_steps,
                        help="每 N 个探索动作审计图并调整探索目标；0 表示禁用")
    parser.add_argument("--coverage-checkpoint-top-k", type=int,
                        default=ExplorerConfig.coverage_checkpoint_top_k,
                        help="覆盖检查点最多提交给模型的候选节点数")
    parser.add_argument(
        "--max-actions-per-page",
        type=int,
        default=ExplorerConfig.max_actions_per_page,
        help="每页最多探索的动作数；0 表示遍历全部 UI 元素",
    )
    parser.add_argument(
        "--screenshot-delay",
        type=float,
        default=ExplorerConfig.screenshot_delay,
        help="截图前等待时间（秒）",
    )
    parser.add_argument(
        "--replay-attempts",
        type=int,
        default=ExplorerConfig.replay_attempts,
        help="布局校验回放失败后，强制模型重定位回放的最大次数（先尝试一次布局校验回放）",
    )
    parser.add_argument("--output", default=ExplorerConfig.output_dir, help="输出目录")
    parser.add_argument("--log-file", default=None,
                        help="终端日志路径；默认在输出目录 logs/ 下按次创建，指定路径则追加写入")
    parser.add_argument("--memory-graph", action="append", default=[],
                        help="作为只读记忆的同 App app_graph.json，可重复指定；默认读取输出目录中的旧图")
    parser.add_argument("--no-graph-memory", action="store_true", help="关闭图谱记忆和自动验证重试，保留当前截图动作选择与定位")
    parser.add_argument("--page-exploration", action="store_true", help="使用旧页面队列；默认按状态探索")
    parser.add_argument("--max-states-per-page", type=int, default=ExplorerConfig.max_states_per_page,
                        help="每页最多展开的状态工作项数量")
    parser.add_argument("--max-actions-per-state", type=int, default=ExplorerConfig.max_actions_per_state,
                        help="每个状态的动作尝试和流程导航总上限")
    parser.add_argument("--continuous-steps", type=int, default=ExplorerConfig.continuous_steps,
                        help="重新调度前优先连续执行的步数")
    parser.add_argument("--max-replay-steps", type=int, default=ExplorerConfig.max_replay_steps,
                        help="单次恢复路径最多回放的动作数")
    parser.add_argument("--summary-budget-seconds", type=float, default=ExplorerConfig.summary_budget_seconds,
                        help="本次运行总结模型请求的累计时间预算；0 只保留待总结证据")
    parser.add_argument("--max-transition-repeats", type=int, default=ExplorerConfig.max_transition_repeats,
                        help="同状态功能分支的最多探索尝试次数")
    parser.add_argument("--memory-scope", default="",
                        help="经验适用环境标识（例如 App 版本/测试账号环境）；不同标识的经验不混用")
    parser.add_argument("--verification-retries", type=int, default=ExplorerConfig.verification_retries,
                        help="每个未确认动作最多追加的验证次数；0 禁用，仍受页面和总步数预算限制")
    parser.add_argument("--memory-top-k", type=int, default=ExplorerConfig.memory_top_k,
                        help="每次规划最多检索的记忆页面数")
    parser.add_argument("--memory-max-chars", type=int, default=ExplorerConfig.memory_max_chars,
                        help="图谱记忆上下文的最大字符数，至少 2000")
    parser.add_argument("--base-url", default=ExplorerConfig.vlm_base_url, help="OpenAI-compatible base URL")
    parser.add_argument("--api-key", default=ExplorerConfig.vlm_api_key, help="模型 API Key")
    parser.add_argument("--model", default=ExplorerConfig.model, help="模型名")
    parser.add_argument("--summary-model", default=ExplorerConfig.summary_model,
                        help="App 核心功能目录使用的独立总结模型，必须不同于 --model；也可设置 SUMMARY_MODEL")
    parser.add_argument("--summary-api-key", default=None,
                        help="同 API 服务上的总结模型密钥；默认 SUMMARY_API_KEY 或执行模型密钥")
    parser.add_argument("--summary-timeout", type=float, default=ExplorerConfig.summary_timeout,
                        help="总结模型单次请求超时秒数；失败不回退到执行模型")
    parser.add_argument("--summary-max-chars", type=int, default=ExplorerConfig.summary_max_chars,
                        help="总结模型证据包最大字符数，至少 4000；不包含固定 prompt")
    parser.add_argument("--embedding-model", default=ExplorerConfig.embedding_model,
                        help="页面语义描述的文本 embedding 模型")
    parser.add_argument("--embedding-base-url", default=ExplorerConfig.embedding_base_url,
                        help="Embedding API 地址；为空时复用 --base-url")
    parser.add_argument("--embedding-api-key", default=ExplorerConfig.embedding_api_key,
                        help="Embedding API Key；为空时复用 --api-key")
    parser.add_argument("--embedding-timeout", type=float, default=ExplorerConfig.embedding_timeout,
                        help="Embedding 请求超时秒数")
    parser.add_argument(
        "--semantic-similarity-threshold",
        type=float,
        default=ExplorerConfig.semantic_similarity_threshold,
        help="触发 VLM 二次判重的 page_description 余弦相似度阈值",
    )
    parser.add_argument("--no-semantic-dedup", action="store_true",
                        help="关闭语义描述检索，使用原有快速视觉 + VLM 判重")
    parser.add_argument(
        "--vlm-max-image-side",
        type=int,
        default=ExplorerConfig.vlm_max_image_side,
        help="发给 VLM 的图片最长边",
    )
    parser.add_argument(
        "--vlm-compare-max-attempts",
        type=int,
        default=ExplorerConfig.vlm_compare_max_attempts,
        help="单次 VLM 页面判重允许的最大请求次数",
    )
    parser.add_argument(
        "--vlm-compare-retry-delay",
        type=float,
        default=ExplorerConfig.vlm_compare_retry_delay,
        help="VLM 页面判重失败后的重试间隔（秒）",
    )
    parser.add_argument("--no-save-analysis", action="store_true", help="不保存每页 VLM 分析 JSON")
    parser.add_argument(
        "--no-back",
        action="store_true",
        help="跳过 Back 键验证，直接通过重启和路径回放恢复页面",
    )
    return parser


def config_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> ExplorerConfig:
    if not args.device.strip():
        parser.error("请通过 --device 或 ANDROID_SERIAL 指定云沙箱的 ADB serial")
    if args.max_pages < 1:
        parser.error("--max-pages 必须大于 0")
    for name in ("max_states_per_page", "max_actions_per_state", "continuous_steps", "max_transition_repeats", "max_replay_steps"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} 必须大于 0")
    if args.verification_retries < 0:
        parser.error("--verification-retries 不能小于 0")
    if not 0 <= args.summary_budget_seconds < float("inf"):
        parser.error("--summary-budget-seconds 必须为有限非负数")
    if args.memory_top_k < 1:
        parser.error("--memory-top-k 必须大于 0")
    if args.memory_max_chars < 2000:
        parser.error("--memory-max-chars 必须至少为 2000")
    if args.max_steps < 0:
        parser.error("--max-steps 不能小于 0")
    if args.coverage_checkpoint_steps < 0:
        parser.error("--coverage-checkpoint-steps 不能小于 0")
    if args.coverage_checkpoint_top_k < 1:
        parser.error("--coverage-checkpoint-top-k 必须大于 0")
    if args.max_depth < 0:
        parser.error("--max-depth 不能小于 0")
    if args.max_actions_per_page < 0:
        parser.error("--max-actions-per-page 不能小于 0")
    if args.screenshot_delay < 0:
        parser.error("--screenshot-delay 不能小于 0")
    if args.replay_attempts < 1:
        parser.error("--replay-attempts 必须大于 0")
    if args.vlm_max_image_side < QWEN_RESIZE_FACTOR:
        parser.error(f"--vlm-max-image-side must be at least {QWEN_RESIZE_FACTOR}")
    if args.vlm_compare_max_attempts < 1:
        parser.error("--vlm-compare-max-attempts 必须大于 0")
    if args.vlm_compare_retry_delay < 0:
        parser.error("--vlm-compare-retry-delay 不能小于 0")
    if not args.embedding_timeout > 0 or args.embedding_timeout == float("inf"):
        parser.error("--embedding-timeout 必须为有限正数")
    if not args.no_semantic_dedup and not args.embedding_model.strip():
        parser.error("--embedding-model 不能为空")
    if (
        not math.isfinite(args.semantic_similarity_threshold)
        or not -1 <= args.semantic_similarity_threshold <= 1
    ):
        parser.error("--semantic-similarity-threshold 必须在 -1 到 1 之间")

    config = ExplorerConfig(
        adb_path=args.adb,
        device_name=args.device,
        app_package=args.package,
        app_activity=args.activity,
        max_pages=args.max_pages,
        state_exploration=not args.page_exploration,
        max_states_per_page=args.max_states_per_page,
        max_actions_per_state=args.max_actions_per_state,
        continuous_steps=args.continuous_steps,
        max_replay_steps=args.max_replay_steps,
        summary_budget_seconds=args.summary_budget_seconds,
        max_transition_repeats=args.max_transition_repeats,
        max_steps=args.max_steps,
        coverage_checkpoint_steps=args.coverage_checkpoint_steps,
        coverage_checkpoint_top_k=args.coverage_checkpoint_top_k,
        max_depth=args.max_depth,
        max_actions_per_page=args.max_actions_per_page,
        screenshot_delay=args.screenshot_delay,
        replay_attempts=args.replay_attempts,
        back_after_action=not args.no_back,
        output_dir=args.output,
        graph_memory=not args.no_graph_memory,
        memory_graph_paths=args.memory_graph,
        memory_top_k=args.memory_top_k,
        memory_max_chars=args.memory_max_chars,
        memory_scope=args.memory_scope.strip(),
        verification_retries=args.verification_retries,
        vlm_base_url=args.base_url,
        vlm_api_key=args.api_key,
        model=args.model,
        summary_model=args.summary_model.strip(),
        summary_timeout=args.summary_timeout,
        summary_max_chars=args.summary_max_chars,
        semantic_dedup=not args.no_semantic_dedup,
        embedding_model=args.embedding_model,
        embedding_base_url=args.embedding_base_url,
        embedding_api_key=args.embedding_api_key,
        embedding_timeout=args.embedding_timeout,
        semantic_similarity_threshold=args.semantic_similarity_threshold,
        vlm_max_image_side=args.vlm_max_image_side,
        vlm_compare_max_attempts=args.vlm_compare_max_attempts,
        vlm_compare_retry_delay=args.vlm_compare_retry_delay,
        save_analysis=not args.no_save_analysis,
    )
    if args.summary_api_key is not None:
        config.summary_api_key = args.summary_api_key
    try:
        chat_request_options(config.model, profile=config.model_api_profile,
                             reasoning_effort=config.model_reasoning_effort)
        validate_summary_config(config)
    except ValueError as exc:
        parser.error(str(exc))
    return config


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    with capture_terminal_output(args.output, args.log_file):
        config = config_from_args(args, parser)
        AndroidExplorer(config).explore()
