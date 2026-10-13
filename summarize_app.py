"""Rebuild the core-capability catalog from a saved graph without operating ADB."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from harness.capabilities import AppCapabilityCatalog
from harness.config import ExplorerConfig
from harness.contracts import AppGraph
from harness.summary import validate_summary_config


def rebuild(graph_path, output, config, *, client=None, reviews_path=None):
    graph_path, output = Path(graph_path).resolve(), Path(output).resolve()
    if output == graph_path.parent:
        raise ValueError("Use a separate output directory to preserve the source run")
    graph = json.loads(graph_path.read_text(encoding="utf-8-sig"))
    if not isinstance(graph, dict) or not isinstance(graph.get("pages"), dict):
        raise ValueError("Expected an app_graph.json with pages")
    if graph.get("app_package") != config.app_package or graph.get("memory_scope", "") != config.memory_scope:
        raise ValueError("Source App/environment differs from summary configuration")
    validate_summary_config(config)
    if reviews_path:
        reviews = json.loads(Path(reviews_path).read_text(encoding="utf-8"))
        if not isinstance(reviews, dict):
            raise ValueError("Reviews must map observation IDs to evidence review records")
        available = {obs.get("observation_id") for obs in graph.get("inherited_observations", []) + graph.get("observations", [])}
        if any(oid not in available or not isinstance(review, dict)
               or review.get("status") not in {"target_mismatch", "recognition_conflict"}
               or not isinstance(review.get("reason"), str) for oid, review in reviews.items()):
            raise ValueError("Reviews must reference real observations and a supported conflict status")
        graph = {**graph, "evidence_reviews": {**graph.get("evidence_reviews", {}), **reviews}}
    run_id = "capabilities_" + uuid4().hex
    config.output_dir = str(output)
    run_dir = output / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    state = SimpleNamespace(run_dir=run_dir, graph=AppGraph(
        app_package=config.app_package, app_name=graph.get("app_name", ""), pages={}, transitions=[],
        exploration_time=0, run_id=run_id, memory_scope=config.memory_scope))
    service = AppCapabilityCatalog(config, state, client)
    # Old prose is deliberately never supplied to the summary model.
    service.ingest_graph(graph)
    try:
        service.update(reason="rebuild", final=True)
    finally:
        service.save()
    manifest = {"source_graph": str(graph_path), "source_run_id": graph.get("run_id"),
                "reviews": str(Path(reviews_path).resolve()) if reviews_path else None,
                "summary_model": service.model, "output": str(output / "app_capabilities.json"),
                "summary_status": service.snapshot()["summary_status"]}
    (run_dir / "rebuild_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return service


def main():
    parser = argparse.ArgumentParser(description="使用 summary 模型从已有探索证据重建 App 核心功能目录（不调用 ADB）")
    parser.add_argument("--graph", required=True)
    parser.add_argument("--output", required=True, help="独立输出目录，不覆盖原探索目录")
    parser.add_argument("--reviews", help="可选：已确认误点/识别冲突的 observation_id 到复核结论映射 JSON")
    parser.add_argument("--summary-model", default=ExplorerConfig.summary_model)
    parser.add_argument("--summary-timeout", type=float, default=ExplorerConfig.summary_timeout)
    parser.add_argument("--summary-max-chars", type=int, default=ExplorerConfig.summary_max_chars)
    args = parser.parse_args()
    graph = json.loads(Path(args.graph).read_text(encoding="utf-8-sig"))
    config = ExplorerConfig(app_package=graph["app_package"], memory_scope=graph.get("memory_scope", ""),
                            output_dir=args.output, summary_model=args.summary_model,
                            summary_timeout=args.summary_timeout, summary_max_chars=args.summary_max_chars)
    if not config.summary_model.strip():
        parser.error("重建模型功能目录需要 --summary-model 或 SUMMARY_MODEL")
    service = rebuild(args.graph, args.output, config, reviews_path=args.reviews)
    result = service.snapshot()
    print(f"功能目录：{Path(args.output).resolve() / 'app_capabilities.md'}")
    print(f"状态：{result['summary_status']}；功能数：{len(result['capabilities'])}；待处理：{len(result['pending_evidence_ids'])}")
    return 0 if result["summary_status"] == "accepted" and not result["pending_evidence_ids"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
