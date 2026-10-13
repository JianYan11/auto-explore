"""Evidence-backed, per-node UI function memory interpreted by the summary model."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import re

from .knowledge import action_descriptor, digest


PROMPT_PATH = Path(__file__).parent / "prompts" / "node_function_memory.md"


def _text(value: object, limit: int = 240) -> str:
    # Defense in depth for common identifiers. Free-form UI prose is NOT a
    # guaranteed anonymous export; the content-profile vocabulary serves that use.
    value = value if isinstance(value, str) else ""
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", value)
    value = re.sub(r"https?://\S+", "[url]", value)
    value = re.sub(r"(?<!\w)\+?\d[\d ()-]{6,}\d(?!\w)", "[identifier]", value)
    return value[:limit]


def _state(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {str(key)[:60]: ("[private]" if re.search(
                r"password|token|secret|phone|email|address|account|message|name|query|input|text", str(key), re.I
            ) else _text(str(value), 100))
            for key, value in list(raw.items())[:12]}


def _canonical(page_id: str, aliases: dict) -> str:
    seen = set()
    while page_id in aliases and page_id not in seen:
        seen.add(page_id)
        page_id = aliases[page_id]
    return page_id


def build_node_evidence(*, page_id: str, page: dict, observations: list[dict],
                        aliases: dict, run_id: str, app_package: str, memory_scope: str) -> dict:
    """Use recorded UI identity, source conditions and outcomes, never old prose.

    Each function owns cases separated by action, exact input and source state.
    Input values are represented by local variant labels in the model packet.
    """
    functions = {}
    units = {}
    control_aliases = page.get("control_aliases", {})
    control_aliases = control_aliases if isinstance(control_aliases, dict) else {}
    aliases = aliases if isinstance(aliases, dict) else {}
    input_variants = {}
    condition_variants = {}

    def function_for(element_id, control):
        fid = "ui_" + digest([page_id, element_id or control])
        if fid not in functions:
            functions[fid] = {"function_id": fid, "element_id": element_id,
                              "control": {key: _text(control.get(key)) for key in ("type", "text", "description")},
                              "case_ids": []}
        return fid

    raw_elements = page.get("elements")
    elements = {e["id"]: e for e in (raw_elements if isinstance(raw_elements, list) else [])
                if isinstance(e, dict) and isinstance(e.get("id"), str)}
    for eid, element in elements.items():
        function_for(eid, element)

    def identify(action, origin_page, element_id):
        element_id = element_id if isinstance(element_id, str) else None
        mapped = control_aliases.get(f"{origin_page}:{element_id}", element_id)
        if isinstance(mapped, str) and mapped in elements and (
                origin_page == page_id or f"{origin_page}:{element_id}" in control_aliases):
            return function_for(mapped, elements[mapped])
        # Legacy observations do not contain element_id. Only an unambiguous
        # semantic match may attach them to a visible control.
        matches = [e for e in elements.values() if e.get("type", "unknown") == action.get("element_type")
                   and e.get("text", "") == action.get("element_text", "")
                   and e.get("description", "") == action.get("target", "")]
        if len(matches) == 1:
            return function_for(matches[0]["id"], matches[0])
        return function_for(None, {"type": action.get("element_type", "unknown"),
                                  "text": action.get("element_text", ""), "description": action.get("target", "")})

    def get_case(fid, action, source_state, conditions):
        uid = "case_" + digest([fid, action, source_state, conditions])
        if uid not in units:
            raw_input = action.get("input_text", "")
            if raw_input:
                input_variants.setdefault(raw_input, f"input_{len(input_variants) + 1}")
            safe_action = {key: _text(value) for key, value in action.items() if key != "input_text"}
            safe_action["input_variant"] = input_variants.get(raw_input, "none")
            condition_variants.setdefault(digest(conditions), f"context_{len(condition_variants) + 1}")
            units[uid] = {"unit_id": uid, "function_id": fid, "action": safe_action,
                          "source_state": _state(source_state), "source_state_ids": [],
                          "conditions": {"variant": condition_variants[digest(conditions)],
                                         "arrival_input_count": len(conditions.get("arrival_inputs") or [])},
                          "status": "untried", "evidence_ids": [], "outcomes": [], "_observations": []}
            functions[fid]["case_ids"].append(uid)
        return units[uid]

    seen = set()
    recorded_actions = set()
    for obs in observations:
        if (obs.get("kind") != "action" or obs.get("run_id") != run_id
                or obs.get("app_package") != app_package or obs.get("memory_scope", "") != memory_scope
                or _canonical(obs.get("from_page", ""), aliases) != page_id):
            continue
        oid = obs.get("observation_id")
        if not isinstance(oid, str) or oid in seen:
            continue
        seen.add(oid)
        action = obs["action"]
        fid = identify(action, obs["from_page"], obs.get("element_id"))
        recorded_actions.add((fid, digest(action)))
        source_state = obs.get("source", {}).get("state", {})
        case = get_case(fid, action, source_state, obs.get("conditions", {}))
        case["evidence_ids"].append(oid)
        sid = obs.get("from_state_id", "")
        if sid and sid not in case["source_state_ids"]:
            case["source_state_ids"].append(sid)
        case["_observations"].append(obs)

    raw_actions = page.get("planned_actions")
    for action in raw_actions if isinstance(raw_actions, list) else []:
        if not isinstance(action, dict):
            continue
        descriptor = action_descriptor(action)
        fid = identify(descriptor, page_id, action.get("element_id"))
        if (fid, digest(descriptor)) not in recorded_actions:
            get_case(fid, descriptor, {}, {})
    for fid, function in functions.items():
        if not function["case_ids"]:
            get_case(fid, {}, {}, {})

    for case in units.values():
        attempts = case.pop("_observations")
        variants = {}
        for obs in attempts:
            target = obs.get("target") or {}
            valid = obs.get("destination_verified") is True
            # State/effect differences remain separate even if the target node is
            # the same. Recovery is recorded separately from forward arrival.
            key = digest([obs["outcome"], target if valid else None,
                          _canonical(obs.get("to_page", ""), aliases) if valid else "",
                          obs.get("recovery"), obs.get("error_stage")])
            outcome = variants.setdefault(key, {
                "outcome": obs["outcome"], "destination_verified": valid,
                "to_page": _canonical(obs.get("to_page", ""), aliases) if valid else "",
                "target_description": _text(target.get("description")) if valid else "",
                "target_state": _state(target.get("state")) if valid else {},
                "target_state_ids": [], "recovery": obs.get("recovery", ""),
                "error_stage": obs.get("error_stage", ""), "count": 0, "evidence_ids": [],
            })
            outcome["count"] += 1
            outcome["evidence_ids"].append(obs["observation_id"])
            if obs.get("to_state_id") and obs["to_state_id"] not in outcome["target_state_ids"]:
                outcome["target_state_ids"].append(obs["to_state_id"])
        effects = {digest([o["outcome"], o.get("target"), _canonical(o.get("to_page", ""), aliases)])
                   for o in attempts if o.get("destination_verified")}
        case["status"] = ("contested" if len(effects) > 1 else "observed" if effects
                          else "unresolved" if attempts else "untried")
        case["attempt_count"] = len(attempts)
        case["outcome_counts"] = dict(Counter(o["outcome"] for o in attempts))
        case["outcomes"] = list(variants.values())
    return {"schema_version": 1, "task": "node_function_memory", "node_id": f"{run_id}/{page_id}",
            "page_id": page_id, "app_package": app_package, "memory_scope": memory_scope,
            "page_description": _text(page.get("page_description") or page.get("page_type")),
            "functions": list(functions.values()), "units": list(units.values()),
            "coverage_complete": False, "business_success": "unknown"}


def live_node_evidence(graph, page) -> dict:
    return build_node_evidence(page_id=page.page_id, page=asdict(page), observations=graph.observations,
                               aliases=graph.node_aliases, run_id=graph.run_id,
                               app_package=graph.app_package, memory_scope=graph.memory_scope)


def validate_node_summary(response: object, payload: dict) -> dict:
    if not isinstance(response, dict) or set(response) != {"node_id", "page_role", "functions"}:
        raise ValueError("Invalid node summary schema")
    if response["node_id"] != payload["node_id"]:
        raise ValueError("Wrong node summary identity")

    def prose(value, limit):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError("Invalid node summary text")
        return _text(value, limit)

    role = prose(response["page_role"], 200)
    values = response["functions"]
    expected = {unit["unit_id"]: unit for unit in payload["units"]}
    if not isinstance(values, list) or len(values) != len(expected):
        raise ValueError("Each supplied UI case needs a summary")
    results = []
    seen = set()
    for item in values:
        if not isinstance(item, dict) or set(item) != {
            "unit_id", "function", "observed_effect", "applicability", "limitations", "evidence_ids",
        }:
            raise ValueError("Unexpected node function fields")
        uid = item["unit_id"]
        if not isinstance(uid, str) or uid not in expected or uid in seen:
            raise ValueError("Unknown or duplicate UI case")
        seen.add(uid)
        unit = expected[uid]
        ids = item["evidence_ids"]
        if (not isinstance(ids, list) or len(ids) > 16
                or any(not isinstance(oid, str) or oid not in unit["evidence_ids"] for oid in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("Citation belongs to another UI/state or is unknown")
        # Every supplied effect/error/recovery variant must remain represented.
        if any(not set(ids).intersection(outcome["evidence_ids"]) for outcome in unit["outcomes"]):
            raise ValueError("Node summary omitted an observed variant")
        results.append({"unit_id": uid, "function": prose(item["function"], 160),
                        "observed_effect": prose(item["observed_effect"], 400),
                        "applicability": prose(item["applicability"], 240),
                        "limitations": prose(item["limitations"], 240), "evidence_ids": ids})
    return {"node_id": payload["node_id"], "page_role": role, "functions": results}


class NodeFunctionMemory:
    """One versioned memory per node. Saving and lookup never call a model."""

    def __init__(self, config, state, client=None):
        self.config, self.state, self.client = config, state, client
        if client is not None and (not config.summary_model.strip()
                                   or getattr(client, "model", None) != config.summary_model.strip()):
            raise ValueError("Node memory must use the configured summary model")
        self.prompt = PROMPT_PATH.read_text(encoding="utf-8")
        self.cache = {}
        self.log_path = state.run_dir / "node_function_summaries.jsonl"

    def _revision(self, evidence):
        return digest([evidence, self.config.summary_model, self.prompt])

    def _packet(self, evidence, units):
        controls = {item["function_id"]: item for item in evidence["functions"]}
        selected = []
        for unit in units:
            item = deepcopy(unit)
            item["control"] = controls[item["function_id"]]["control"]
            # Keep at least one reference for EVERY distinct effect and failure.
            # If that set does not fit, fail this case rather than hide a branch.
            for outcome in item["outcomes"]:
                outcome["evidence_ids"] = outcome["evidence_ids"][-1:]
                outcome["target_state_ids"] = outcome["target_state_ids"][-2:]
            item["evidence_ids"] = [oid for o in item["outcomes"] for oid in o["evidence_ids"]]
            item["source_state_ids"] = item["source_state_ids"][-2:]
            selected.append(item)
        return {key: evidence[key] for key in ("task", "node_id", "page_id", "page_description")} | {
            "app_package": _text(evidence["app_package"]), "memory_scope": _text(evidence["memory_scope"]),
            "units": selected, "partial_node": len(units) < len(evidence["units"]),
            "descriptions_from_execution_model": True, "input_values_omitted": True,
            "business_success": "unknown",
        }

    def summarize(self, evidence: dict) -> dict:
        revision = self._revision(evidence)
        previous = self.cache.get(evidence["node_id"])
        if self.client is None or (previous and previous["revision"] == revision):
            return self.snapshot(evidence)
        cached = {"revision": revision, "summaries": {}, "roles": [], "failed_units": []}
        # Publish the empty revision before any call so failure cannot leave old prose.
        self.cache[evidence["node_id"]] = cached
        remaining = list(evidence["units"])
        while remaining:
            batch = [remaining.pop(0)]
            while remaining and len(batch) < 3:
                candidate = self._packet(evidence, batch + [remaining[0]])
                if len(json.dumps(candidate, ensure_ascii=False)) > self.config.summary_max_chars:
                    break
                batch.append(remaining.pop(0))
            event = {"node_id": evidence["node_id"], "evidence_revision": revision,
                     "model": self.config.summary_model, "unit_ids": [u["unit_id"] for u in batch],
                     "status": "failed"}
            try:
                payload = self._packet(evidence, batch)
                if (len(json.dumps(payload, ensure_ascii=False)) > self.config.summary_max_chars
                        or any(len(u["evidence_ids"]) > 16 for u in payload["units"])):
                    raise ValueError("Node evidence exceeds input/citation budget")
                response = self.client.summarize(self.prompt, payload)
                result = validate_node_summary(response, payload)
                cached["summaries"].update({item["unit_id"]: item for item in result["functions"]})
                cached["roles"].append({"page_role": result["page_role"], "unit_ids": event["unit_ids"]})
                event["status"] = "accepted"
            except Exception as exc:
                cached["failed_units"].extend(event["unit_ids"])
                event["error_type"] = type(exc).__name__
                print(f"[Node memory] 节点功能总结未采用（{type(exc).__name__}），保留 UI 操作证据")
            # No raw model response, input values or exception messages in this log.
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        return self.snapshot(evidence)

    def summarize_page(self, page_id):
        page_id = self.state.resolve_page_id(page_id)
        page = self.state.graph.pages.get(page_id)
        if page is not None:
            return self.summarize(live_node_evidence(self.state.graph, page))

    def snapshot(self, evidence):
        revision = self._revision(evidence)
        cached = self.cache.get(evidence["node_id"])
        cached = cached if cached and cached["revision"] == revision else None
        units = {unit["unit_id"]: unit for unit in evidence["units"]}
        result = {key: evidence[key] for key in ("schema_version", "node_id", "page_id", "coverage_complete", "business_success")}
        result.update({"evidence_revision": revision, "model": self.config.summary_model.strip() or None,
                       "advisory_only": True, "page_roles": deepcopy(cached["roles"]) if cached else [],
                       "functions": [], "summary_status": "disabled" if self.client is None else
                       "empty" if not units else "pending" if cached is None else
                       "partial" if cached["failed_units"] and cached["summaries"] else
                       "unavailable" if cached["failed_units"] else "accepted"})
        for function in evidence["functions"]:
            item = deepcopy(function)
            item["cases"] = [deepcopy(units[uid]) for uid in item.pop("case_ids")]
            for case in item["cases"]:
                summary = cached["summaries"].get(case["unit_id"]) if cached else None
                if summary is not None:
                    case["model_summary"] = deepcopy(summary)
            result["functions"].append(item)
        return result

    def snapshot_page(self, page):
        return self.snapshot(live_node_evidence(self.state.graph, page))

    def context(self, evidence, *, historical=False, max_chars=2200):
        memory = self.summarize(evidence) if historical else self.snapshot(evidence)
        result = {key: memory[key] for key in ("node_id", "page_id", "summary_status", "advisory_only")}
        result.update({"historical": historical, "functions": [], "truncated": False})
        for function in memory["functions"]:
            for case in function["cases"]:
                item = {"function_id": function["function_id"], "element_id": function["element_id"],
                        "control": function["control"], "unit_id": case["unit_id"],
                        "source_state": case["source_state"], "action": case["action"],
                        "conditions": case["conditions"],
                        "status": case["status"], "attempt_count": case["attempt_count"],
                        "outcome_counts": case["outcome_counts"]}
                if "model_summary" in case:
                    item["model_summary"] = case["model_summary"]
                result["functions"].append(item)
                if len(json.dumps(result, ensure_ascii=False)) > max_chars:
                    result["functions"].pop()
                    result["truncated"] = True
        return result
