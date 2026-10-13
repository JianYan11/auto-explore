"""Evidence-derived topology knowledge. No model-generated causal claims or edges.

Observations are immutable; aliases affect projections, never their source evidence.
Historical knowledge is advisory and cannot add nodes or reachable edges to a run.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math

from .contracts import AppGraph, ExplorationAction, PageState


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def valid_observation(value: object) -> bool:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return False
    if not all(isinstance(value.get(key), str) for key in (
        "observation_id", "run_id", "app_package", "memory_scope",
    )):
        return False
    timestamp = value.get("timestamp")
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
        return False
    if value.get("kind") == "state_restore":
        return all(isinstance(value.get(key), str) for key in ("work_id", "page_id", "event"))
    if value.get("kind") == "state_budget":
        return all(isinstance(value.get(key), str) for key in ("page_id", "state_id", "reason"))
    if value.get("kind") == "state_conflict":
        return all(isinstance(value.get(key), str) for key in ("work_id", "state_id", "reason"))
    if value.get("kind") == "input_focus":
        return (isinstance(value.get("target"), str) and isinstance(value.get("layout_id"), str)
                and type(value.get("located")) is bool)
    if value.get("kind") == "input_verification":
        return (all(isinstance(value.get(key), str) for key in ("control_key", "field_key", "from_work_id"))
                and type(value.get("verified")) is bool
                and all(value.get(key) is None or isinstance(value[key], str) for key in ("expected", "actual")))
    if value.get("kind") == "replay":
        return (type(value.get("arrived")) is bool and isinstance(value.get("path"), list)
                and isinstance(value.get("target_description"), str)
                and isinstance(value.get("target_page"), str)
                and all(isinstance(step, dict) for step in value["path"]))
    def descriptor_valid(descriptor: object) -> bool:
        return (isinstance(descriptor, dict)
                and isinstance(descriptor.get("description", ""), str)
                and isinstance(descriptor.get("activity", ""), str)
                and isinstance(descriptor.get("state", {}), dict)
                and isinstance(descriptor.get("controls", []), list))
    return (
        value.get("kind") == "action"
        and all(isinstance(value.get(key), dict) for key in ("source", "action", "conditions"))
        and descriptor_valid(value["source"])
        and all(isinstance(value.get(key), str) for key in ("from_page", "to_page", "action_key", "outcome"))
        and type(value.get("destination_verified")) is bool
        and type(value.get("execution_success")) is bool
        and type(value.get("observation_valid")) is bool
        and (not value["destination_verified"] or (
            value["execution_success"] and value["observation_valid"] and descriptor_valid(value.get("target"))))
    )


def action_descriptor(action: ExplorationAction | dict) -> dict:
    raw = asdict(action) if isinstance(action, ExplorationAction) else action
    start, end = raw.get("swipe_start") or {}, raw.get("swipe_end") or {}
    direction = ""
    if raw.get("action_type") == "swipe" and start and end:
        dx, dy = end.get("x", 0) - start.get("x", 0), end.get("y", 0) - start.get("y", 0)
        direction = ("right" if dx > 0 else "left") if abs(dx) > abs(dy) else ("down" if dy > 0 else "up")
    return {
        "action_type": raw.get("action_type", ""),
        "target": raw.get("target_description") or raw.get("element_text", ""),
        "element_text": raw.get("element_text", ""),
        "element_type": raw.get("element_type", "unknown"),
        "input_text": raw.get("input_text", ""), "direction": direction,
    }


def action_key(action: ExplorationAction | dict) -> str:
    return digest(action_descriptor(action))


def page_descriptor(page: PageState) -> dict:
    return {
        "description": page.page_description or page.page_type,
        "activity": page.activity, "state": dict(page.semantic_state),
        "controls": sorted({f"{e.type}:{e.text}:{e.description}" for e in page.elements}),
    }


def classify_outcome(*, execution_success: bool, observation_valid: bool,
                     destination_verified: bool, same_page: bool, visual_changed: bool,
                     error_stage: str = "", outside_app: bool = False) -> str:
    if error_stage == "invalid_action":
        return "invalid_action"
    if not execution_success:
        return "execution_failed"
    if not observation_valid:
        return "observation_failed"
    if error_stage == "identification":
        return "identification_failed"
    if outside_app:
        return "outside_app"
    if not destination_verified:
        return "destination_unresolved"
    if not same_page:
        return "destination_observed"
    return "same_page_changed" if visual_changed else "no_visible_change"


def canonical(graph: AppGraph, page_id: str) -> str:
    while page_id in graph.node_aliases:
        page_id = graph.node_aliases[page_id]
    return page_id


def edge_evidence(graph: AppGraph) -> list[dict]:
    """Aggregate only current-run observed destinations, including verified self-loops."""
    groups: dict[str, dict] = {}
    attempts: dict[str, list[dict]] = {}
    for obs in graph.observations:
        if obs.get("kind") == "action" and obs.get("execution_attempted", True):
            key = digest([canonical(graph, obs["from_page"]), obs["action"], obs["conditions"]])
            attempts.setdefault(key, []).append(obs)
    for obs in graph.observations:
        if obs.get("kind") != "action" or not obs.get("destination_verified"):
            continue
        source, target = canonical(graph, obs["from_page"]), canonical(graph, obs["to_page"])
        key = digest([source, target, obs["action"], obs["conditions"]])
        item = groups.setdefault(key, {
            "edge_id": key, "from_page": source, "to_page": target,
            "action": obs["action"], "conditions": obs["conditions"],
            "observation_ids": [], "outcome_counts": {}, "status": "observed",
            "state_transitions": [],
        })
        item["observation_ids"].append(obs["observation_id"])
        from_state, to_state = obs.get("from_state_id"), obs.get("to_state_id")
        if from_state and to_state:
            state_edge = next((edge for edge in item["state_transitions"]
                               if edge["from_state_id"] == from_state and edge["to_state_id"] == to_state), None)
            if state_edge is None:
                state_edge = {"from_state_id": from_state, "to_state_id": to_state, "observation_ids": []}
                item["state_transitions"].append(state_edge)
            state_edge["observation_ids"].append(obs["observation_id"])
        counts = item["outcome_counts"]
        counts[obs["outcome"]] = counts.get(obs["outcome"], 0) + 1
        item["observation_count"] = len(item["observation_ids"])
        item["status"] = "repeated_observation" if item["observation_count"] >= 2 else "observed"
    for item in groups.values():
        matching = attempts.get(digest([item["from_page"], item["action"], item["conditions"]]), [])
        verified = [obs for obs in matching if obs["destination_verified"]]
        item["attempt_count"] = len(matching)
        item["execution_failure_count"] = sum(not obs["execution_success"] for obs in matching)
        item["unresolved_count"] = len(matching) - len(verified)
        item["other_destination_count"] = sum(canonical(graph, obs["to_page"]) != item["to_page"] for obs in verified)
        item["observed_destination_share"] = item["observation_count"] / len(verified) if verified else None
    return list(groups.values())


def route_evidence(graph: AppGraph) -> list[dict]:
    groups: dict[str, dict] = {}
    for obs in graph.observations:
        if not (obs.get("kind") == "replay" or
                (obs.get("kind") == "state_restore" and obs.get("event") == "replay")):
            continue
        target = canonical(graph, obs["target_page"])
        key = digest([target, obs.get("target_state_key", ""), obs["path"]])
        item = groups.setdefault(key, {
            "target_page": target, "path": obs["path"], "attempts": 0,
            "target_work_id": obs.get("work_id", ""), "target_state_key": obs.get("target_state_key", ""),
            "verified_arrivals": 0, "failed_steps": {}, "evidence_ids": [],
        })
        item["attempts"] += 1
        item["verified_arrivals"] += int(obs["arrived"])
        item["evidence_ids"].append(obs["observation_id"])
        if not obs["arrived"]:
            step = str(obs.get("failed_step") if obs.get("failed_step") is not None else "arrival_or_restart")
            item["failed_steps"][step] = item["failed_steps"].get(step, 0) + 1
    return list(groups.values())


def exploration_checklist(graph: AppGraph, page: PageState) -> list[dict]:
    """Attempted is not verified. Preserve unresolved work even after plan exhaustion."""
    if graph.work_items:
        from .work import local_action_key
        result = []
        for work in graph.work_items.values():
            if canonical(graph, work.page_id) != page.page_id or work.status == "merged":
                continue
            for action in work.planned_actions:
                progress = work.action_progress.get(local_action_key(action), {})
                status = progress.get("status", "pending")
                result.append({"work_id": work.work_id, "state_key": work.state_key,
                    "action_key": action_key(action), "action": action_descriptor(action),
                    "status": status, "attempts": progress.get("attempts", 0),
                    "evidence_ids": progress.get("evidence_ids", []),
                    "pending_in_plan": status in {"pending", "attempted_unresolved"}
                                       and work.status in {"pending", "exploring"},
                    "observed_targets": [], "question": "Continue only under this work item's preconditions."})
        return result
    actions: dict[str, dict] = {}
    for index, action in enumerate(page.planned_actions):
        key = action_key(action)
        item = actions.setdefault(key, {
            "action_key": key, "action": action_descriptor(action), "evidence_ids": [],
            "status": "untried", "attempts": 0, "observed_targets": [],
            "pending_in_plan": False,
        })
        item["pending_in_plan"] |= index >= page.next_action_index
        if index < page.next_action_index:
            item["status"] = "attempted_unresolved"
    planned_ids = {action.element_id for action in page.planned_actions}
    for element in page.elements:
        if element.id not in planned_ids:
            key = f"unplanned:{element.id}"
            actions[key] = {
                "action_key": key, "control": {"text": element.text, "type": element.type,
                                                "description": element.description},
                "status": "unplanned", "pending_in_plan": False, "attempts": 0,
                "evidence_ids": [], "question": "Assess this visible control before adding any safe action.",
            }
    for obs in graph.observations:
        if obs.get("kind") != "action" or canonical(graph, obs.get("from_page", "")) != page.page_id:
            continue
        key = obs["action_key"]
        item = actions.setdefault(key, {
            "action_key": key, "action": obs["action"], "evidence_ids": [],
            "attempts": 0, "observed_targets": [], "pending_in_plan": False,
        })
        item["attempts"] += 1
        item["evidence_ids"].append(obs["observation_id"])
        item["last_outcome"] = obs["outcome"]
        if obs["destination_verified"]:
            target = canonical(graph, obs["to_page"])
            if target not in item["observed_targets"]:
                item["observed_targets"].append(target)
        if obs["outcome"] == "destination_observed":
            item["status"] = "observed"
        elif obs["outcome"] in {"execution_failed", "invalid_action", "outside_app"}:
            item["status"] = "blocked"
        else:
            item["status"] = "attempted_unresolved"
        if len(item["observed_targets"]) > 1:
            item["status"] = "needs_verification"
        item["question"] = {
            "observed": "Revalidate the transition when this route is needed.",
            "blocked": "Check the recorded failure and prerequisites before retrying.",
            "needs_verification": "Explain differing observed destinations under these conditions.",
            "attempted_unresolved": "Determine whether the action has a visible effect; do not infer a cause.",
        }[item["status"]]
    return list(actions.values())


def derive_experiences(observations: list[dict]) -> list[dict]:
    """Summarize observations deterministically, retaining support and counterexamples.

All competing *valid* outcomes remain visible. Tool/capture errors do not refute a
navigation relationship. Repeated snapshots cannot increase support counts.
"""
    groups: dict[str, dict] = {}
    seen: set[str] = set()
    for obs in observations:
        oid = obs.get("observation_id")
        if not isinstance(oid, str) or oid in seen or obs.get("kind") != "action":
            continue
        if not all(isinstance(obs.get(key), dict) for key in ("source", "action", "conditions")):
            continue
        seen.add(oid)
        group_key = digest([obs.get("app_package"), obs.get("memory_scope", ""),
                            obs["source"], obs["action"], obs["conditions"]])
        group = groups.setdefault(group_key, {
            "experience_id": f"experience_{group_key}", "source": obs["source"],
            "action": obs["action"], "conditions": obs["conditions"],
            "memory_scope": obs.get("memory_scope", ""), "observations": [],
        })
        group["observations"].append(obs)
    results = []
    for group in groups.values():
        entries = group.pop("observations")
        valid = [o for o in entries if o.get("destination_verified") and isinstance(o.get("target"), dict)]
        variants: dict[str, dict] = {}
        for obs in valid:
            key = digest([obs["outcome"], obs["target"]])
            variant = variants.setdefault(key, {"outcome": obs["outcome"], "target": obs["target"], "evidence_ids": []})
            variant["evidence_ids"].append(obs["observation_id"])
        ranked = sorted(variants.values(), key=lambda v: -len(v["evidence_ids"]))
        support = ranked[0]["evidence_ids"] if ranked else []
        counterexamples = [oid for v in ranked[1:] for oid in v["evidence_ids"]]
        status = "contested" if counterexamples else ("supported" if len(support) >= 2 else "candidate")
        group.update({
            "status": status, "support_count": len(support), "support_ids": support,
            "counterexample_count": len(counterexamples),
            "counterexample_ids": counterexamples, "variants": ranked,
            "attempt_count": len(entries), "outcome_counts": dict(Counter(o.get("outcome", "unknown") for o in entries)),
            "unresolved_ids": [o["observation_id"] for o in entries if o not in valid],
            "last_observed_at": max(o.get("timestamp", 0) for o in entries),
            "summary": (
                "Different destinations or effects were observed under the recorded conditions; verify before reuse."
                if counterexamples else
                "The recorded destination/effect has repeated observational support; business success is unknown."
                if len(support) >= 2 else
                "Only limited or unresolved evidence is available; further observation is needed."
            ),
        })
        results.append(group)
    return results
