"""State frontier, functional coverage and exact recovery preconditions.

Model labels are hints. Unknown conditions never authorize cross-state reuse.
Layout fingerprints are for grounding/recovery only, not exploration identity.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
import math
import re

from .contracts import ExplorationAction, StateWorkItem
from .grounding import layout_id
from .knowledge import digest
from .controls import control_identity, route_depth
from .recovery import categorical_values, canonical_guard, guard_matches, guard_differences
from .identity import behavioral_changes, compatible_behavior


def token(value: object) -> str:
    return "_".join(str(value).strip().casefold().split())


def flat_values(raw: object, *, normalized: bool = False) -> dict:
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key, value in list(raw.items())[:64]:
        if not isinstance(key, str):
            continue
        key = token(key)
        if re.search(r"(^|_)(clock|battery|cursor|timestamp|keyboard)(_|$)", key):
            continue
        if not key or isinstance(value, (dict, list)):
            continue
        if value is None or (isinstance(value, float) and not math.isfinite(value)):
            value = "unknown"
        elif isinstance(value, str):
            value = token(value) if normalized else value.strip()
        elif not isinstance(value, (bool, int, float)):
            continue
        result[key] = value
    return result


def unknown(value: object) -> bool:
    return value is None or (isinstance(value, str) and token(value) in {"unknown", "unobserved", "uncertain"})


def abstract_state(raw: dict) -> dict:
    """Conservative fallback for legacy analyses; never interpret null as empty."""
    result = {}
    for key, value in raw.items():
        if re.search(r"(^|_)(clock|battery|cursor|timestamp|keyboard)(_|$)", key):
            continue
        if unknown(value):
            result[key] = "unknown"
        elif isinstance(value, str) and re.search(r"(^|_)(title|name|query|description|notes|text|date|time)(_|$)", key):
            result[key] = "empty" if not value else "filled"
        else:
            result[key] = token(value) if isinstance(value, str) else value
    return result


def local_action_key(action: ExplorationAction) -> str:
    # Position, local element IDs and model-selected input values are not action identity.
    return digest((action.action_type, control_identity(action),
                   action.target_value or (action.element_text if action.action_type != "input" else ""),
                   action.input_mode if action.action_type == "input" else ""))


def surface_signature(actions: list[ExplorationAction]) -> str:
    return digest(sorted({(a.action_type, control_identity(a),
                           a.target_value or (a.element_text if a.action_type != "input" else ""))
                          for a in actions}))


def replayable_route(route):
    return all(step.replay_safe and step.arrival_verified and step.source_guard and step.target_guard
               for step in route)


def observation_profile(page, capture, analysis: dict, actions: list[ExplorationAction], raw_state: dict) -> dict:
    meta = analysis.get("exploration")
    meta = meta if isinstance(meta, dict) else {}
    raw = categorical_values(flat_values(raw_state))
    values = categorical_values(flat_values(meta.get("values")))
    conflicts = [key for key in values if key in raw and not unknown(raw[key])
                 and not unknown(values[key]) and raw[key] != values[key]]
    values = {**raw, **values}
    normalized = {**abstract_state(raw), **flat_values(meta.get("state"), normalized=True)}
    # An explicit field observation is more specific than a screen-type summary.
    # Null/missing field values are not evidence that the editor is empty.
    fields = {}
    for action in actions:
        if action.action_type != "input":
            continue
        identity = control_identity(action)
        key = token(action.field_key or (identity[6:] if identity.startswith("field:") else ""))
        if not key:
            continue
        value = action.field_value
        if value is None and key in values and not unknown(values[key]):
            value = values[key]
        if key in fields and fields[key] is not None and value is not None and fields[key] != value:
            conflicts.append(key)
            continue
        if value is not None:
            fields[key] = value
            values[key] = value
            if re.search(r"(^|_)(query|title|name|description|notes|text)$", key):
                normalized[key] = "empty" if value == "" else "filled"
        else:
            fields.setdefault(key, None)
            values.setdefault(key, "unknown")
            if re.search(r"(^|_)(query|title|name|description|notes|text)$", key):
                normalized.setdefault(key, "unknown")
    # List-window hints describe what happened to be visible, not a prerequisite
    # for every operation. Keep them when an action explicitly depends on them.
    dependencies = {token(key) for action in actions for key in (action.relevant_state_keys or [])}
    dependencies.update(token(key) for key in meta.get("required_state_keys", []) if isinstance(key, str))
    if any(action.relevant_state_keys is None or action.intent in {"commit", "mutation", "unknown"}
           for action in actions):
        dependencies.update({"first_visible_item", "last_visible_item", "total_results"})
    incidental = {"first_visible_item", "last_visible_item", "total_results"} - dependencies
    normalized = {key: value for key, value in normalized.items() if key not in incidental}
    advisory = set(incidental)
    if (analysis.get("page_type") in {"home", "search", "search_results"}
            and "title" not in fields and "title" not in dependencies):
        advisory.add("title")
        normalized.pop("title", None)
    conflicts = [key for key in conflicts if key not in advisory]
    context = flat_values(meta.get("context"))
    required_context = meta.get("required_context_keys", [])
    if analysis.get("page_type") in {"home", "list", "search", "search_results"}:
        context = {key: value for key, value in context.items()
                   if not (key == "item" and unknown(value) and key not in required_context)}
    context = {key: value for key, value in context.items() if token(value) != "not_applicable"}
    # Entity identity must not be lost simply because a model omitted the context map.
    for key, value in raw.items():
        if re.search(r"(^|_)(task_id|item_id|list_id|list_name|account|draft_id|entity_id)$", key):
            context.setdefault(key, value)
    module = token(meta.get("module") or page.page_description or page.page_type)
    surface = surface_signature(actions)
    return {
        "page_id": page.page_id, "activity": page.activity, "module": module,
        "state": normalized, "context": context, "values": values,
        "surface": surface, "layout_id": layout_id(capture), "conflicts": conflicts,
        "controls": sorted({control_identity(a) for a in actions}),
        "input_fields": fields,
        "analysis_error": str(analysis.get("analysis_error") or "") if analysis.get("analysis_status") == "failed" else "",
        "recovery_value_keys": sorted((set(values) - advisory) | set(fields)),
    }


def functional_scope(work: StateWorkItem, action: ExplorationAction, page_id=None) -> dict:
    """Shared conditions for scheduler coverage and functional experience grouping."""
    keys = action.relevant_state_keys
    if keys is None or action.intent in {"commit", "mutation", "unknown"}:
        keys = list(work.normalized_state)
    gates = {key for key in work.normalized_state
             if any(part in key for part in ("mode", "permission", "overlay")) or key == "active_tab"}
    if action.element_type == "tab":
        gates.update(key for key in work.normalized_state if key == "tab" or key.endswith("_tab"))
    keys = sorted({token(key) for key in keys} | gates)
    conditions = {key: work.normalized_state.get(key, "unknown") for key in keys}
    # Mutations/commits never share object-specific progress on a module hint alone.
    shared = action.coverage_scope == "module" and action.action_family and action.intent not in {"commit", "mutation", "unknown"}
    context = {} if shared else {**work.context, "workflow_context": work.context_key}
    uncertain = any(unknown(value) for value in (*conditions.values(), *context.values()))
    return {"page": page_id or work.page_id, "module": work.module,
            "family": control_identity(action), "type": action.action_type,
            "variant": action.target_value or (action.element_text if action.action_type != "input" else ""),
            "control_type": action.element_type, "intent": action.intent, "conditions": conditions,
            "input_mode": action.input_mode if action.action_type == "input" else "",
            "context": context, "surface": work.restore_guard.get("surface", "") if action.relevant_state_keys is None else "",
            "uncertain_work": work.work_id if uncertain else ""}


class WorkRegistry:
    def __init__(self, state):
        self.owner = state
        self.config = state.config
        self.graph = state.graph
        self.transition_counts: dict[str, int] = {}
        self.navigation_used: set[tuple[str, str, str]] = set()
        self.input_failures: dict[tuple, int] = {}

    def resolve(self, work_id: str) -> str:
        while work_id in self.graph.work_aliases:
            work_id = self.graph.work_aliases[work_id]
        return work_id

    def canonical_guard(self, guard: dict) -> dict:
        result = canonical_guard(guard)
        if result:
            result["page_id"] = self.owner.resolve_page_id(result.get("page_id", ""))
        return result

    def reconcile_restored_work(self, requested, observed, *, vlm_verified=False):
        """Accept an equivalent verified arrival with a refreshed current action plan.

        Promotional content or changing list rows can expose a different surface.
        Retain progress for stable action keys, but execute only current controls.
        """
        if requested is observed:
            return requested
        if (not compatible_behavior(requested.restore_guard, observed.restore_guard)
                or (not vlm_verified and not guard_matches(requested.restore_guard, observed.restore_guard))):
            return None
        for key, progress in observed.action_progress.items():
            old = requested.action_progress.get(key)
            if old is None:
                requested.action_progress[key] = deepcopy(progress)
            else:
                old["attempts"] += progress.get("attempts", 0)
                old["evidence_ids"] = list(dict.fromkeys(old["evidence_ids"] + progress.get("evidence_ids", [])))
                if progress.get("status") in {"covered", "external"} and old.get("status") != "covered":
                    old["status"] = progress["status"]
                    if progress.get("coverage_key"):
                        old["coverage_key"] = progress["coverage_key"]
        requested.attempts += observed.attempts
        requested.navigation_steps += observed.navigation_steps
        requested.planned_actions = deepcopy(observed.planned_actions)
        requested.elements = deepcopy(observed.elements)
        requested.state_key, requested.context_key = observed.state_key, observed.context_key
        requested.normalized_state = deepcopy(observed.normalized_state)
        requested.context = deepcopy(observed.context)
        requested.restore_guard = deepcopy(observed.restore_guard)
        old_routes = [requested.entry_path, *requested.entry_routes, *observed.entry_routes]
        requested.entry_path = deepcopy(observed.entry_path)
        requested.entry_routes = self.unique_routes(old_routes, requested.entry_path)
        requested.observed_state_ids = list(dict.fromkeys(requested.observed_state_ids + observed.observed_state_ids))
        observed.status = "merged"
        self.graph.work_aliases[observed.work_id] = requested.work_id
        self.graph.work_queue[:] = list(dict.fromkeys(self.resolve(item) for item in self.graph.work_queue))
        if requested.status in {"recovery_failed", "state_unresolved"}:
            requested.status = "pending"
        self.enqueue(requested)
        self.owner.append_observation({"kind": "state_restore", "work_id": requested.work_id,
            "page_id": requested.page_id, "event": "equivalent_work_reconciled",
            "observed_work_id": observed.work_id, "arrived": True,
            "verification_method": "vlm" if vlm_verified else "observed_metadata"})
        return requested

    def get_or_create(self, page, visual, profile, actions, elements, path, *, workflow_id="", vlm_verified=False):
        page_id = self.owner.resolve_page_id(page.page_id)
        if profile.get("analysis_error"):
            page.exploration_status = "blocked"
            self.owner.append_observation({"kind": "analysis_failed", "page_id": page_id,
                "state_id": visual.state_id, "error": profile["analysis_error"]})
            return None
        state_key = digest((profile["state"], profile["surface"], profile["activity"]))
        context_key = digest((profile["context"], workflow_id))
        found = next((item for item in self.graph.work_items.values()
                      if self.resolve(item.work_id) == item.work_id
                      and item.page_id == page_id and item.state_key == state_key
                      and item.context_key == context_key and item.module == profile["module"]), None)
        if found:
            if visual.state_id not in found.observed_state_ids:
                found.observed_state_ids.append(visual.state_id)
            if (not vlm_verified and any(unknown(value) for values in (profile["state"], profile["context"]) for value in values.values())
                    and not guard_matches(found.restore_guard, profile)):
                found.status = "state_unresolved"
                found.analysis_error = "unknown_conditions_changed"
                self.owner.append_observation({"kind": "state_conflict", "work_id": found.work_id,
                    "state_id": visual.state_id, "reason": found.analysis_error})
            found.visit_count += 1
            # Current plans use current controls; stable keys retain attempts across metadata revisions.
            found.planned_actions = deepcopy(actions)
            found.elements = deepcopy(elements)
            if vlm_verified or guard_matches(found.restore_guard, profile):
                routes = [found.entry_path, *found.entry_routes]
                route_key = digest([asdict(step) for step in path])
                if all(digest([asdict(step) for step in route]) != route_key for route in routes):
                    found.entry_routes.append(deepcopy(path))
                    found.entry_routes.sort(key=lambda route: (not replayable_route(route), len(route)))
                    found.entry_routes = found.entry_routes[:3]
                if found.status in {"recovery_failed", "state_unresolved"}:
                    found.status = "pending"  # It has now been observed at its verified guard.
                if vlm_verified:
                    prior_routes = [found.entry_path, *found.entry_routes]
                    found.restore_guard = deepcopy(profile)
                    found.entry_path = deepcopy(path)
                    found.entry_routes = self.unique_routes(prior_routes, path)
            elif (not profile["conflicts"] and not any(unknown(value)
                  for key in ("state", "context", "values") for value in profile[key].values())):
                # Equivalent abstract work can be reached with different concrete values.
                # Keep progress, but restore only the freshly observed values and path.
                found.restore_guard = deepcopy(profile)
                found.entry_path = deepcopy(path)
                found.entry_routes = []
                if found.status == "recovery_failed":
                    found.status = "pending"
                self.owner.append_observation({"kind": "state_restore", "work_id": found.work_id,
                    "page_id": found.page_id, "event": "observed_guard_rebased", "arrived": True})
            self.enqueue(found)
            return found
        count = sum(item.page_id == page_id and self.resolve(item.work_id) == item.work_id
                    for item in self.graph.work_items.values())
        if count >= self.config.max_states_per_page:
            page.exploration_status = "budget_limited"
            self.owner.append_observation({"kind": "state_budget", "page_id": page_id,
                                           "state_id": visual.state_id, "reason": "max_states_per_page"})
            return None
        work_id = f"work_{len(self.graph.work_items)}"
        found = StateWorkItem(
            work_id=work_id, page_id=page_id, state_key=state_key, context_key=context_key,
            module=profile["module"], normalized_state=deepcopy(profile["state"]),
            context=deepcopy(profile["context"]), observed_state_ids=[visual.state_id],
            planned_actions=deepcopy(actions), elements=deepcopy(elements), entry_path=deepcopy(path),
            restore_guard=deepcopy(profile), workflow_id=workflow_id,
            visit_count=1, analysis_error="conflicting_state_fields" if profile["conflicts"] else "",
        )
        if profile["conflicts"] and not vlm_verified:
            found.status = "state_unresolved"
        self.graph.work_items[work_id] = found
        self.enqueue(found)
        return found

    def coverage_key(self, work: StateWorkItem, action: ExplorationAction) -> str:
        return digest(functional_scope(work, action, self.owner.resolve_page_id(work.page_id)))

    @staticmethod
    def unique_routes(routes, primary):
        seen = {digest([asdict(step) for step in primary])}
        result = []
        for route in routes:
            key = digest([asdict(step) for step in route])
            if key not in seen:
                seen.add(key)
                result.append(deepcopy(route))
        result.sort(key=lambda route: (not replayable_route(route), len(route)))
        return result[:3]

    def candidates(self, work: StateWorkItem, *, allow_navigation=False) -> list[tuple[ExplorationAction, str]]:
        if work.status in {"recovery_failed", "planning_blocked", "state_unresolved", "budget_limited", "merged"}:
            return []
        if work.attempts + work.navigation_steps >= self.config.max_actions_per_state:
            self.limit(work, "max_actions_per_state")
            return []
        if route_depth(work.entry_path) >= self.config.max_depth:
            self.limit(work, "max_depth")
            return []
        page_attempts = sum(w.attempts + w.navigation_steps for w in self.graph.work_items.values()
                            if w.page_id == work.page_id and w.status != "merged")
        if self.config.max_actions_per_page and page_attempts >= self.config.max_actions_per_page:
            self.limit(work, "max_actions_per_page")
            return []
        candidates = []
        seen = set()
        for action in work.planned_actions:
            key = local_action_key(action)
            if key in seen:
                continue
            seen.add(key)
            progress = work.action_progress.setdefault(key, {"status": "pending", "attempts": 0, "evidence_ids": []})
            coverage_key = self.coverage_key(work, action)
            if coverage_key in self.graph.functional_coverage:
                progress.update(status="covered", coverage_key=coverage_key)
                nav_key = (work.workflow_id, work.work_id, key)
                if (allow_navigation and action.workflow_role in {"advance", "return"}
                        and action.replay_safe and nav_key not in self.navigation_used):
                    candidates.append((action, "navigation"))
                continue
            if progress["status"] in {"covered", "external", "blocked", "budget_limited"}:
                continue
            if (action.action_type == "input" and self.input_failures.get(
                    (work.page_id, work.context_key, control_identity(action)), 0) >= 1 + self.config.verification_retries):
                progress["status"] = "blocked"
                continue
            if progress["attempts"] >= 1 + self.config.verification_retries:
                progress["status"] = "blocked"
                continue
            repeat_key = self.repeat_key(work, action)
            if self.transition_counts.get(repeat_key, 0) >= self.config.max_transition_repeats:
                progress["status"] = "budget_limited"
                continue
            candidates.append((action, "exploration"))
        return candidates

    def repeat_key(self, work, action):
        return digest((work.page_id, work.module, work.state_key, self.coverage_key(work, action)))

    def limit(self, work, reason):
        work.status, work.budget_reason = "budget_limited", reason
        self.graph.work_queue[:] = [item for item in self.graph.work_queue if self.resolve(item) != work.work_id]

    def settle(self, work):
        if self.candidates(work):
            if work.status == "complete":
                work.status = "pending"
            return
        if work.status not in {"planning_blocked", "recovery_failed", "state_unresolved", "budget_limited", "merged"}:
            statuses = {value["status"] for value in work.action_progress.values()}
            if "budget_limited" in statuses:
                self.limit(work, "max_transition_repeats")
            else:
                work.status = "blocked" if "blocked" in statuses else "complete"

    def enqueue(self, work, *, first=False):
        self.settle(work)
        work_id = self.resolve(work.work_id)
        if work.status not in {"pending", "exploring"} or work_id in self.graph.work_queue:
            return
        self.graph.work_queue.insert(0 if first else len(self.graph.work_queue), work_id)

    def take(self, work):
        self.graph.work_queue[:] = [item for item in self.graph.work_queue if self.resolve(item) != work.work_id]
        work.status = "exploring"
        self.graph.work_order.append(work.work_id)

    def record_result(self, work, original, action, target, before, after, observation, purpose):
        key = local_action_key(original)
        progress = work.action_progress.setdefault(key, {"status": "pending", "attempts": 0, "evidence_ids": []})
        progress["evidence_ids"].append(observation["observation_id"])
        progress["last_outcome"] = observation["outcome"]
        if purpose == "navigation":
            work.navigation_steps += 1
            self.navigation_used.add((work.workflow_id, work.work_id, key))
        else:
            work.attempts += 1
            progress["attempts"] += 1
            repeat_key = self.repeat_key(work, original)
            self.transition_counts[repeat_key] = self.transition_counts.get(repeat_key, 0) + 1
        effect = bool(target and observation["execution_success"] and observation["destination_verified"]
                      and observation.get("operation_verified") is not False
                      and (behavioral_changes(before, after)
                           or observation.get("operation_verified") is True))
        if action.action_type == "input" and observation.get("operation_verified") is False:
            failure_key = (work.page_id, work.context_key, control_identity(action))
            self.input_failures[failure_key] = self.input_failures.get(failure_key, 0) + 1
        from .evidence import evidence_quality
        if observation["outcome"] == "outside_app":
            # An external destination ends this UI branch. It is not a failed
            # internal arrival and must not be retried or promoted into coverage.
            progress.update(status="external", external_activity=observation.get("external_activity", ""))
        elif (effect and action.source_layout_id and not before.get("conflicts") and not after.get("conflicts")
                and evidence_quality(observation)["target_alignment"] != "conflict"):
            coverage_key = self.coverage_key(work, original)
            progress.update(status="covered", coverage_key=coverage_key)
            coverage = self.graph.functional_coverage.setdefault(coverage_key, {
                "source_work_id": work.work_id, "action_family": action.action_family or action.target_description,
                "intent": action.intent, "evidence_ids": [], "target_work_ids": [],
                "effect": "observed_transition", "business_success": None,
                "scope": functional_scope(work, original, self.owner.resolve_page_id(work.page_id)),
                "description": original.target_description,
            })
            coverage["evidence_ids"].append(observation["observation_id"])
            if target.work_id not in coverage["target_work_ids"]:
                coverage["target_work_ids"].append(target.work_id)
        elif purpose != "navigation":
            # An unobserved commit may already have persisted; never retry it automatically.
            progress["status"] = "blocked" if action.intent in {"commit", "mutation"} else "attempted_unresolved"
        self.settle(work)
        self.enqueue(work)

    def checklist(self, work):
        self.settle(work)
        return [{"work_id": work.work_id, "action_key": local_action_key(action),
                 "action": {"action_type": action.action_type, "target": action.target_description},
                 **deepcopy(work.action_progress.get(local_action_key(action), {"status": "pending", "attempts": 0}))}
                for action in work.planned_actions]

    def page_summary(self, page_id):
        items = [w for w in self.graph.work_items.values() if w.page_id == page_id and w.status != "merged"]
        return {"work_count": len(items), "statuses": {status: sum(w.status == status for w in items)
                for status in sorted({w.status for w in items})}, "attempts": sum(w.attempts for w in items),
                "navigation_steps": sum(w.navigation_steps for w in items)}

    def exploration_summary(self, work, *, limit=24):
        """Readable current-run operation memory; coverage still belongs to the registry."""
        covered, uncertain, visible = [], [], []
        seen = set()
        for item in self.graph.work_items.values():
            if item.page_id != work.page_id or item.status == "merged":
                continue
            for action in item.planned_actions:
                scope = functional_scope(item, action, self.owner.resolve_page_id(item.page_id))
                key = self.coverage_key(item, action)
                progress = item.action_progress.get(local_action_key(action), {})
                evidence = self.graph.functional_coverage.get(key)
                if evidence and key not in seen:
                    seen.add(key)
                    covered.append({"description": action.target_description[:200],
                        "control_key": control_identity(action), "scope": scope,
                        "applicable_to_current_state": key == self.coverage_key(work, action),
                        "evidence_ids": evidence.get("evidence_ids", [])[-3:]})
                elif progress.get("attempts") and progress.get("status") != "covered":
                    uncertain.append({"description": action.target_description[:200],
                        "state": item.normalized_state, "status": progress.get("status"),
                        "attempts": progress.get("attempts", 0),
                        "last_outcome": progress.get("last_outcome", ""),
                        "evidence_ids": progress.get("evidence_ids", [])[-3:]})
        for action in work.planned_actions:
            key = self.coverage_key(work, action)
            visible.append({"description": action.target_description[:200],
                "control_key": control_identity(action),
                "conditions": functional_scope(work, action)["conditions"],
                "status": "covered" if key in self.graph.functional_coverage else
                          work.action_progress.get(local_action_key(action), {}).get("status", "pending")})
        common = [item for item in covered if item["applicable_to_current_state"]
                  and not any(key.endswith("_tab") and key != "active_tab" for key in item["scope"]["conditions"])]
        current = [item for item in covered if item["applicable_to_current_state"] and item not in common]
        summary = {"page_id": work.page_id, "work_id": work.work_id,
            "state": deepcopy(work.normalized_state), "context": deepcopy(work.context),
            "description": f"Observed {len(common)} common operations and {len(current)} state-specific operations; "
                           f"{len(uncertain)} attempted operations remain unconfirmed.",
            "common_covered": common[:limit], "current_state_covered": current[:limit],
            "failed_or_uncertain": uncertain[-limit:], "visible_operations": visible[:limit],
            "truncated": any(len(items) > limit for items in (common, current, uncertain, visible)),
            "coverage_authority": "current_run_functional_registry"}
        # Share the same UI evidence limits as revisit analysis, without placing
        # a dependency on the scheduler during module initialization.
        from .revisit import _bounded
        bounded = _bounded(summary)
        bounded["truncated"] = summary["truncated"] or bounded != summary
        groups = ("common_covered", "current_state_covered", "failed_or_uncertain", "visible_operations")
        while len(json.dumps(bounded, ensure_ascii=False)) > 12000:
            largest = max(groups, key=lambda group: len(json.dumps(bounded[group], ensure_ascii=False)))
            if bounded[largest]:
                bounded[largest].pop()
            elif len(bounded["state"]) > 1 or len(bounded["context"]) > 1:
                for name in ("state", "context"):
                    if len(bounded[name]) > 1:
                        bounded[name].pop(next(reversed(bounded[name])))
            else:
                break
            bounded["truncated"] = True
        return bounded

    def rebase_pages(self):
        """Page merges preserve state progress and only merge equivalent work identities."""
        identities = {}
        for work in self.graph.work_items.values():
            if work.status == "merged":
                continue
            work.page_id = self.owner.resolve_page_id(work.page_id)
            work.restore_guard = self.canonical_guard(work.restore_guard)
            identity = (work.page_id, work.module, work.state_key, work.context_key)
            prior = identities.get(identity)
            if prior is None:
                identities[identity] = work
                continue
            prior.observed_state_ids = list(dict.fromkeys(prior.observed_state_ids + work.observed_state_ids))
            existing = {local_action_key(a) for a in prior.planned_actions}
            prior.planned_actions.extend(a for a in work.planned_actions if local_action_key(a) not in existing)
            for key, value in work.action_progress.items():
                old = prior.action_progress.get(key)
                if old is None:
                    prior.action_progress[key] = deepcopy(value)
                else:
                    old["attempts"] += value["attempts"]
                    old["evidence_ids"] = list(dict.fromkeys(old["evidence_ids"] + value["evidence_ids"]))
                    if value["status"] in {"covered", "external"} and old.get("status") != "covered":
                        old["status"] = value["status"]
                        if value.get("coverage_key"):
                            old["coverage_key"] = value["coverage_key"]
            prior.attempts += work.attempts
            prior.navigation_steps += work.navigation_steps
            prior.entry_routes = self.unique_routes(
                [*prior.entry_routes, work.entry_path, *work.entry_routes], prior.entry_path)
            work.status = "merged"
            self.graph.work_aliases[work.work_id] = prior.work_id
        self.graph.work_queue[:] = list(dict.fromkeys(self.resolve(item) for item in self.graph.work_queue))
        # Re-key evidence against canonical page IDs; never trust labels alone for page merges.
        rekeyed = {}
        for work in self.graph.work_items.values():
            if work.status == "merged":
                continue
            for action in work.planned_actions:
                progress = work.action_progress.get(local_action_key(action), {})
                old_key = progress.get("coverage_key")
                if old_key in self.graph.functional_coverage:
                    key = self.coverage_key(work, action)
                    evidence = deepcopy(self.graph.functional_coverage[old_key])
                    evidence["scope"] = functional_scope(work, action, self.owner.resolve_page_id(work.page_id))
                    evidence["source_work_id"] = self.resolve(evidence["source_work_id"])
                    evidence["target_work_ids"] = list(dict.fromkeys(self.resolve(item) for item in evidence["target_work_ids"]))
                    if key in rekeyed:
                        for field in ("evidence_ids", "target_work_ids"):
                            rekeyed[key][field] = list(dict.fromkeys(rekeyed[key][field] + evidence[field]))
                    else:
                        rekeyed[key] = evidence
                    progress["coverage_key"] = key
            self.enqueue(work)
        self.graph.functional_coverage = rekeyed
