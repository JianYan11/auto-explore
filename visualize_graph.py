#!/usr/bin/env python3
"""Export existing exploration evidence to an offline, self-contained viewer."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import quote


TEMPLATE = Path(__file__).parent / "visualization" / "viewer.html"


def build_app(path: Path, output_dir: Path, images: dict[str, str], warnings: list[str]) -> dict:
    path = path.resolve()
    graph = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(graph.get("pages"), dict) or not isinstance(graph.get("transitions"), list):
        raise ValueError(f"Not an exploration graph: {path}")
    # Only embed screenshots under this attempt directory. Graph strings never
    # become arbitrary local-file reads, executable HTML, or remote requests.
    image_root = path.parent
    known_paths: dict[Path, str] = {}

    def image(value: str | None) -> str:
        if not value:
            return ""
        candidate = Path(value)
        candidate = (candidate if candidate.is_absolute() else image_root / candidate).resolve()
        if candidate in known_paths:
            return known_paths[candidate]
        if not candidate.is_relative_to(image_root) or not candidate.is_file():
            warnings.append(f"Missing or out-of-scope screenshot: {candidate.name}")
            return ""
        mime = mimetypes.guess_type(candidate.name)[0]
        if mime not in {"image/png", "image/jpeg", "image/webp"}:
            warnings.append(f"Unsupported screenshot: {candidate.name}")
            return ""
        key = f"image_{len(images)}"
        images[key] = f"data:{mime};base64," + base64.b64encode(candidate.read_bytes()).decode("ascii")
        known_paths[candidate] = key
        return key

    nodes, states, work = [], {}, graph.get("work_items", {})
    for page_id, page in graph["pages"].items():
        variants = []
        for state_id, state in page.get("states", {}).items():
            variant = {
                "id": state_id, "page": page_id, "type": page.get("page_type", ""),
                "image": image(state.get("screenshot_path")), "activity": state.get("activity", ""),
                "state": state.get("behavioral_state") or {}, "values": state.get("semantic_state") or {},
                "context": state.get("context") or {},
            }
            variants.append(variant)
            states[state_id] = variant
        nodes.append({
            "id": page_id, "page": page_id, "type": page.get("page_type", ""),
            "image": image(page.get("screenshot_path")), "activity": page.get("activity", ""),
            "description": page.get("page_description", ""), "variants": variants,
            "work": [{"id": w.get("work_id", key), "status": w.get("status", ""),
                      "module": w.get("module", ""), "state": w.get("normalized_state") or {}}
                     for key, w in work.items() if w.get("page_id") == page_id],
        })
    observations = {o.get("observation_id"): o for o in graph.get("observations", [])}
    edges = []
    for index, edge in enumerate(graph["transitions"]):
        action = edge.get("action_details") or {}
        observation = observations.get(edge.get("observation_id"), {})
        edges.append({
            "id": f"edge_{index}", "index": index + 1,
            "source": edge.get("from_page", ""), "target": edge.get("to_page", ""),
            "source_state": edge.get("from_state_id", ""), "target_state": edge.get("to_state_id", ""),
            "source_work": edge.get("from_work_id", ""), "target_work": edge.get("to_work_id", ""),
            "action": edge.get("action", ""), "text": edge.get("element_text", ""),
            "description": action.get("target_description", ""),
            "success": edge.get("success"), "verified": edge.get("destination_verified"),
            "business_success": observation.get("business_success"),
            "outcome": edge.get("outcome", ""), "error": observation.get("error", ""),
            "before": image(edge.get("before_screenshot")), "after": image(edge.get("after_screenshot")),
        })
    referenced = {e[k] for e in edges for k in ("source_state", "target_state") if e[k]}
    state_nodes = [value for key, value in states.items() if key in referenced]
    source = Path(os.path.relpath(path, output_dir)).as_posix()
    return {
        "package": graph.get("app_package", path.parent.name),
        "name": graph.get("app_name") or graph.get("app_package", "App"),
        "run": graph.get("run_id", ""), "root": graph.get("root_page_id", ""),
        "steps": graph.get("completed_steps", len(edges)),
        "termination": graph.get("termination", {}).get("reason", "unknown"),
        "nodes": nodes, "states": state_nodes, "all_state_count": len(states), "edges": edges,
        "source": quote(source, safe="/ .").replace(" ", "%20"),
    }


def export_graphs(graphs: list[Path], output: Path) -> dict:
    if not graphs:
        raise ValueError("Provide at least one graph")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    images, warnings = {}, []
    apps = [build_app(p, output, images, warnings) for p in graphs]
    payload = {"apps": apps, "images": images, "warnings": warnings}
    # HTML script parsing precedes JSON parsing; escape '<' to prevent a graph
    # label such as </script> from ending the non-executable data block.
    encoded = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__GRAPH_DATA__", encoded)
    target = output / "index.html"
    target.write_text(html, encoding="utf-8")
    manifest = {
        "viewer": str(target), "graphs": [str(p.resolve()) for p in graphs],
        "apps": [{"package": a["package"], "pages": len(a["nodes"]),
                  "connected_states": len(a["states"]), "actions": len(a["edges"])} for a in apps],
        "embedded_screenshots": len(images), "warnings": warnings,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graphs", type=Path, nargs="*", help="Existing app_graph.json files")
    parser.add_argument("--report", type=Path, help="verification.json containing tasks[].graph")
    parser.add_argument("--output", type=Path, required=True, help="Directory for index.html and manifest.json")
    args = parser.parse_args()
    graphs = list(args.graphs)
    try:
        if args.report:
            report = json.loads(args.report.read_text(encoding="utf-8"))
            for task in report["tasks"]:
                value = Path(task["graph"])
                graphs.append(value if value.is_absolute() else args.report.parent / value)
        result = export_graphs(list(dict.fromkeys(graphs)), args.output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
