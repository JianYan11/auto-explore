"""Bounded, read-only retrieval over prior graphs and the live exploration graph."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
from typing import Any

from .config import ExplorerConfig
from .content_profile import sanitize_inventory
from .contracts import ActionSequenceStep, AppGraph
from .knowledge import derive_experiences, exploration_checklist, valid_observation
from .node_memory import build_node_evidence, live_node_evidence


def usable_input(text: object) -> bool:
    """Reject placeholders and text unsupported by the existing ADB input transport."""
    return (
        isinstance(text, str) and 0 < len(text.strip()) <= 128
        and text.isascii() and text.isprintable() and "%" not in text
        and re.fullmatch(r"(?:(?:test|example|sample|dummy)(?:[ _-]+(?:text|input|query|search|value|data))?"
                         r"|foo|bar|abc|hello(?: world)?)[ _-]*\d*",
                         text.strip(), re.IGNORECASE) is None
    )


def _text(value: object, limit: int = 160) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _items(value: object) -> list:
    return value if isinstance(value, list) else []


def _state_values(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {str(key)[:60]: str(item)[:120] for key, item in list(value.items())[:10]
            if item is None or isinstance(item, (str, int, float, bool))}


def _tokens(text: str) -> set[str]:
    # Word overlap for Latin text; character bigrams for Chinese UI labels.
    words = set(re.findall(r"[a-z0-9]+", text.casefold()))
    for part in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(part[index:index + 2] for index in range(max(1, len(part) - 1)))
    return words


def _checklist_context(value: object) -> list[dict]:
    items = [item for item in _items(value) if isinstance(item, dict)]
    items.sort(key=lambda item: (
        bool(item.get("pending_in_plan")),
        item.get("status") in {"needs_verification", "attempted_unresolved", "blocked"},
    ), reverse=True)
    result = []
    for item in items[:8]:
        action = item.get("action") or item.get("control") or {}
        action = action if isinstance(action, dict) else {}
        evidence = _items(item.get("evidence_ids"))
        result.append({
            "action_key": _text(item.get("action_key"), 80),
            "action": {key: _text(action.get(key)) for key in (
                "action_type", "target", "element_text", "input_text", "direction", "description", "text",
            ) if key in action},
            "status": _text(item.get("status"), 40),
            "pending_in_plan": item.get("pending_in_plan") is True,
            "attempts": item.get("attempts", 0),
            "evidence_ids": [_text(oid, 80) for oid in evidence[-3:]],
            "evidence_count": len(evidence),
            "last_outcome": _text(item.get("last_outcome"), 40),
            "question": _text(item.get("question")),
        })
    return result


class GraphMemory:
    def __init__(self, config: ExplorerConfig):
        self.config = config
        self.sources: list[str] = []
        self.historical: list[dict] = []
        self.observations: list[dict] = []
        self.content_inventories: list[dict] = []
        self.summary_service = None
        self.node_function_service = None
        self.capability_service = None
        self.capability_graphs = []
        self.historical_node_evidence: dict[str, dict] = {}
        if not config.graph_memory:
            return
        explicit = bool(config.memory_graph_paths)
        output_graph = (Path(config.output_dir) / "app_graph.json").resolve()
        archive_dir = Path(config.output_dir) / "memory_graphs"
        paths = config.memory_graph_paths or [str(output_graph), *map(str, sorted(archive_dir.glob("*.json")))]
        seen = set()
        for path_text in dict.fromkeys(str(Path(path).resolve()) for path in paths):
            path = Path(path_text)
            if not explicit and not path.exists():
                continue
            try:
                if path.stat().st_size > 64 * 1024 * 1024:
                    raise ValueError("Memory graph exceeds 64 MiB")
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if digest in seen:
                    continue
                graph = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(graph, dict) or not isinstance(graph.get("pages"), dict):
                    raise ValueError("Expected an app_graph.json object with a pages dictionary")
                if graph.get("app_package") != config.app_package:
                    raise ValueError("Memory graph app_package differs from the target App")
                if graph.get("memory_scope", "") != config.memory_scope:
                    raise ValueError("Memory graph memory_scope differs from this run")
                evidence = _items(graph.get("inherited_observations")) + _items(graph.get("observations"))
                if any(not valid_observation(obs) for obs in evidence):
                    raise ValueError("Memory graph contains malformed observation evidence")
                # The live graph writer replaces app_graph.json. Keep the old memory
                # snapshot intact across subsequent runs; never mutate an external source.
                if path == output_graph:
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    path = (archive_dir / f"{digest}.json").resolve()
                    if not path.exists():
                        path.write_bytes(raw)
                source = f"history_{len(self.sources)}"
                self.historical.extend(self._records(graph, source))
                for page_id, page in graph["pages"].items():
                    if isinstance(page, dict):
                        self.historical_node_evidence[f"{source}/{page_id}"] = build_node_evidence(
                            page_id=page_id, page=page, observations=evidence,
                            aliases=graph.get("node_aliases", {}), run_id=graph.get("run_id", ""),
                            app_package=config.app_package, memory_scope=config.memory_scope,
                        )
                # Only structural tokens survive cross-run reuse. Never trust
                # saved model prose, fixture values, or user identity mappings.
                profile = graph.get("content_profile")
                if isinstance(profile, dict):
                    self.content_inventories.extend(
                        sanitize_inventory(item.get("inventory"))
                        for item in _items(profile.get("evidence")) if isinstance(item, dict)
                    )
                # Rebuild experience from evidence, never trust saved prose as established facts.
                self.observations.extend(
                    obs for obs in evidence if obs.get("app_package") == config.app_package
                    and obs.get("memory_scope", "") == config.memory_scope
                )
                self.sources.append(str(path))
                # The capability service rebuilds from UI/transition evidence, not old prose.
                self.capability_graphs.append(graph)
                seen.add(digest)
            except (OSError, ValueError) as exc:
                if explicit:
                    raise ValueError(f"Cannot load memory graph {path}: {exc}") from exc
                print(f"[Memory] 忽略无法使用的旧图谱 {path}: {exc}")
        if self.sources:
            print(f"[Memory] 已读取 {len(self.sources)} 个同 App 图谱作为只读记忆")
        self.observations = list({obs["observation_id"]: obs for obs in self.observations
                                  if isinstance(obs.get("observation_id"), str)}.values())

    def _records(self, graph: dict, source: str) -> list[dict]:
        records = {}
        for page_id, page in graph.get("pages", {}).items():
            if not isinstance(page, dict):
                continue
            activity = _text(page.get("activity"))
            package = page.get("app_package") or activity.split("/", 1)[0] or graph.get("app_package")
            if package != self.config.app_package:
                continue
            state = page.get("semantic_state")
            state = state if isinstance(state, dict) else {}
            variants = page.get("states", {})
            variants = list(variants.values()) if isinstance(variants, dict) else []
            variants = [variant for variant in variants if isinstance(variant, dict)]
            path = _items(page.get("action_sequence"))
            entry_path = [{
                "action": _text(step.get("action_type"), 30),
                "target": _text(step.get("target_description") or step.get("element_text")),
                "input_text": _text(step.get("input_text"), 128),
            } for step in path[-6:] if isinstance(step, dict)]
            records[page_id] = {
                "memory_id": f"{source}/{page_id}", "page_id": page_id, "source": source,
                "description": _text(page.get("page_description") or page.get("page_type")),
                "activity": activity,
                "entry_path": entry_path,
                "entry_path_length": len(path),
                "entry_path_truncated": len(entry_path) < len(path),
                "state": _state_values(state),
                "state_count": len(variants),
                "states": [{"state_id": _text(variant.get("state_id"), 80),
                            "state": _state_values(variant.get("semantic_state")),
                            "behavioral_state": _state_values(variant.get("behavioral_state")),
                            "context": _state_values(variant.get("context")),
                            "entry_path": [{"action": _text(step.get("action_type"), 30),
                                            "target": _text(step.get("target_description"))}
                                           for step in _items(variant.get("action_sequence"))[-6:]
                                           if isinstance(step, dict)],
                            "entry_path_truncated": len(_items(variant.get("action_sequence"))) > 6}
                           for variant in variants[-6:]],
                "content_hints": [_text(value) for value in _items(page.get("content_hints"))[:12]
                                  if isinstance(value, str)],
                "controls": [{"text": _text(element.get("text"), 80),
                              "description": _text(element.get("description")),
                              "type": _text(element.get("type"), 30)}
                             for element in _items(page.get("elements"))[:12] if isinstance(element, dict)],
                "outcomes": [],
                "checklist": _checklist_context(page.get("exploration_checklist")),
            }
        for edge in _items(graph.get("transitions")):
            if not isinstance(edge, dict):
                continue
            source_id = edge.get("from_page")
            if not isinstance(source_id, str) or source_id not in records:
                continue
            action = edge.get("action_details")
            action = action if isinstance(action, dict) else {}
            target_id = edge.get("to_page")
            target = records.get(target_id) if isinstance(target_id, str) else None
            records[source_id]["outcomes"].append({
                "target": target["memory_id"] if target else None,
                "target_description": target["description"] if target else "",
                "from_state_id": _text(edge.get("from_state_id"), 80),
                "to_state_id": _text(edge.get("to_state_id"), 80),
                "action": _text(action.get("action_type") or edge.get("action"), 30),
                "instruction": _text(action.get("target_description")),
                "input_text": _text(action.get("input_text"), 128),
                "execution_success": edge.get("success") is True,
                "state_changed": edge.get("state_changed") is True,
                "recovery": _text(edge.get("recovery"), 30),
                "outcome": _text(edge.get("outcome") or "legacy_unknown", 40),
                "destination_verified": edge.get("destination_verified") is True,
            })
        for record in records.values():
            record["outcomes"] = record["outcomes"][-8:]
        return list(records.values())

    def context(self, graph: AppGraph, *, query: str = "", activity: str = "",
                 current_page_id: str | None = None,
                 action_sequence: list[ActionSequenceStep] | None = None,
                 purpose: str = "action", target_page_id: str | None = None, current_work_id: str = "") -> dict:
        if purpose not in {"action", "analyze", "replay", "schedule"}:
            raise ValueError(f"Unknown memory purpose: {purpose}")
        # Project only useful fields: no screenshot bytes, embeddings or stale coordinates.
        live = self._records({
            "app_package": graph.app_package,
            "pages": {page_id: {
                "app_package": page.app_package, "activity": page.activity,
                "page_description": page.page_description, "page_type": page.page_type,
                "semantic_state": page.semantic_state, "content_hints": page.content_hints,
                "states": {sid: {"state_id": sid, "semantic_state": state.semantic_state,
                                 "behavioral_state": state.behavioral_state, "context": state.context,
                                 "action_sequence": [{"action_type": step.action_type,
                                    "target_description": step.target_description} for step in state.action_sequence]}
                           for sid, state in page.states.items()},
                "action_sequence": [{
                    "action_type": step.action_type, "target_description": step.target_description,
                    "input_text": step.input_text,
                } for step in page.action_sequence],
                "elements": [{"text": element.text, "description": element.description, "type": element.type}
                             for element in page.elements],
                "exploration_checklist": exploration_checklist(graph, page),
            } for page_id, page in graph.pages.items()},
            "transitions": [{
                "from_page": edge.from_page, "to_page": edge.to_page, "success": edge.success,
                "from_state_id": edge.from_state_id, "to_state_id": edge.to_state_id,
                "state_changed": edge.state_changed, "recovery": edge.recovery,
                "outcome": edge.outcome, "destination_verified": edge.destination_verified,
                "action_details": {"action_type": edge.action, "target_description": edge.action_details.target_description,
                                   "input_text": edge.action_details.input_text},
            } for edge in graph.transitions],
        }, "live")
        path = [{"action": step.action_type, "target": _text(step.target_description),
                 "input_text": _text(step.input_text, 128)} for step in (action_sequence or [])[-4:]]
        query_tokens = _tokens(query + " " + " ".join(item["target"] for item in path))
        focus_id = target_page_id if purpose == "replay" else current_page_id
        neighbors = {edge.to_page for edge in graph.transitions if edge.from_page == focus_id}
        neighbors.update(edge.from_page for edge in graph.transitions if edge.to_page == focus_id)

        def score(record: dict) -> int:
            # Match content, not JSON keys, node IDs or words in package names.
            searchable = " ".join([
                record["description"], *record["content_hints"], *record["state"].values(),
                *(value for variant in record["states"] for value in variant["state"].values()),
                *(control["text"] + " " + control["description"] for control in record["controls"]),
                *(step["target"] + " " + step["input_text"] for step in record["entry_path"]),
                *(outcome["instruction"] + " " + outcome["input_text"] for outcome in record["outcomes"]),
            ])
            overlap = len(query_tokens & _tokens(searchable))
            return (overlap * 5 + int(record["activity"] == activity) * 2
                    + (100 if record["source"] == "live" and record["page_id"] == focus_id else 0)
                    + (30 if record["source"] == "live" and record["page_id"] in neighbors else 0))

        records = sorted(live + self.historical, key=score, reverse=True)
        result: dict[str, Any] = {
            "app_package": self.config.app_package, "current_page_id": current_page_id,
            "purpose": purpose, "memory_scope": self.config.memory_scope,
            "recent_path": path, "pages": [],
            "experiences": [], "routes": [],
            "historical_graph_count": len(self.sources),
            "live_page_count": len(live),
            "note": "Observations only. Device execution success does not prove business success. "
                    "Historical pages may be stale; the current screenshot is authoritative.",
        }
        if purpose == "replay":
            result["target_page_id"] = target_page_id
        selected = records[:self.config.memory_top_k]
        if self.capability_service and purpose in {"action", "schedule"}:
            result["app_capabilities"] = self.capability_service.context(
                query=query, page_id=current_page_id or "", work_id=current_work_id,
                max_chars=min(2600, self.config.memory_max_chars // 3))
        relevant = {record["description"] for record in selected}
        experiences = [] if self.capability_service else derive_experiences(self.observations + graph.observations)
        experiences = [item for item in experiences if item["source"].get("description") in relevant]
        experiences.sort(key=lambda item: (
            item["status"] == "contested", len(query_tokens & _tokens(json.dumps(item["action"], ensure_ascii=False))),
            item["last_observed_at"],
        ), reverse=True)
        for experience in experiences[:4]:
            item = {key: experience[key] for key in (
                "experience_id", "source", "action", "conditions", "status", "support_count",
                "attempt_count", "outcome_counts", "summary", "last_observed_at", "counterexample_count",
            )}
            item["support_ids"] = experience["support_ids"][-3:]
            item["counterexample_ids"] = experience["counterexample_ids"][-3:]
            item["unresolved_ids"] = experience["unresolved_ids"][-3:]
            item["variants"] = [{"outcome": v["outcome"], "target": v["target"],
                                 "count": len(v["evidence_ids"])} for v in experience["variants"][:3]]
            item["advisory_only"] = True
            if self.summary_service:
                summary = self.summary_service.summarize(experience)
                if summary is not None:
                    item["model_summary"] = summary
            result["experiences"].append(item)
            if len(json.dumps(result, ensure_ascii=False)) > self.config.memory_max_chars // 2:
                # Keep the factual experience if the optional prose exceeds its context allocation.
                item.pop("model_summary", None)
            if len(json.dumps(result, ensure_ascii=False)) > self.config.memory_max_chars // 2:
                result["experiences"].pop()
                break
        if purpose == "replay":
            for obs in reversed(self.observations + graph.observations):
                if obs.get("kind") != "replay" or obs.get("target_description") not in relevant:
                    continue
                route = {key: obs.get(key) for key in (
                    "observation_id", "target_description", "arrived", "failed_step", "reason", "path",
                )}
                result["routes"].append(route)
                if len(json.dumps(result, ensure_ascii=False)) > self.config.memory_max_chars // 2:
                    result["routes"].pop()
                    break
                if len(result["routes"]) >= 3:
                    break
        for record in selected:
            # Trim a single large record rather than drop the current page entirely.
            item = {**record, "outcomes": list(record["outcomes"]), "controls": list(record["controls"]),
                    "content_hints": list(record["content_hints"]), "entry_path": list(record["entry_path"]),
                    "checklist": list(record["checklist"]), "states": list(record["states"])}
            if self.node_function_service and purpose in {"action", "schedule"}:
                historical = record["source"] != "live"
                evidence = (self.historical_node_evidence.get(record["memory_id"]) if historical else
                            live_node_evidence(graph, graph.pages[record["page_id"]]))
                if evidence is not None:
                    item["node_function_memory"] = self.node_function_service.context(
                        evidence, historical=historical,
                        max_chars=min(2400, max(400, self.config.memory_max_chars // 3)),
                    )
            if purpose == "analyze":
                item["checklist"] = []  # Identity analysis needs surfaces, not action progress.
            if purpose == "schedule":
                item["entry_path"] = []
                item["entry_path_truncated"] = item["entry_path_length"] > 0
            result["pages"].append(item)
            while len(json.dumps(result, ensure_ascii=False)) > self.config.memory_max_chars:
                # Keep the current node's functional interpretation ahead of less
                # relevant historical pages, but remain within the total budget.
                node_memory = item.get("node_function_memory")
                if node_memory and record["page_id"] != focus_id:
                    item.pop("node_function_memory")
                    continue
                field = next((key for key in ("outcomes", "controls", "entry_path", "content_hints", "checklist", "states") if item[key]), None)
                if field:
                    item[field].pop(0 if field in {"outcomes", "entry_path"} else -1)
                    if field == "entry_path":
                        item["entry_path_truncated"] = True
                elif item["state"]:
                    item["state"] = {}
                elif node_memory:
                    item.pop("node_function_memory")
                else:
                    result["pages"].pop()
                    break
        while len(json.dumps(result, ensure_ascii=False)) > self.config.memory_max_chars and result["recent_path"]:
            result["recent_path"].pop(0)
        return result
