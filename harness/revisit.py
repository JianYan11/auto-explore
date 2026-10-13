"""Bounded, current-run UI evidence for comparative screen analysis."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json

from .contracts import PageState, ScreenCapture
from .grounding import layout_id
from .work import functional_scope, local_action_key


@dataclass(frozen=True)
class RevisitHistory:
    reference: ScreenCapture
    evidence: dict
    revision: str


def _bounded(value, depth=0):
    """Keep UI labels/flat conditions, never historical geometry or image data."""
    if depth > 5:
        return None
    if isinstance(value, dict):
        return {str(key)[:80]: _bounded(item, depth + 1)
                for key, item in list(value.items())[:32]}
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth + 1) for item in value[:16]]
    return value[:240] if isinstance(value, str) else value


def build_revisit_history(state, page: PageState, current: ScreenCapture) -> RevisitHistory | None:
    """Use observed coverage as evidence; this function never grants coverage."""
    page_id = state.resolve_page_id(page.page_id)
    graph = state.graph
    observations = [obs for obs in graph.observations
                    if obs.get("kind") == "action" and obs.get("run_id") == graph.run_id
                    and state.resolve_page_id(obs.get("from_page", "")) == page_id]
    works = [work for work in graph.work_items.values()
             if state.resolve_page_id(work.page_id) == page_id and work.status != "merged"]
    # A newly registered page has no exploration history. Reuse its initial analysis.
    if not observations and not works:
        return None

    reference = None
    reference_observation = ""
    for obs in reversed(observations):
        source_layout = (obs.get("grounding") or {}).get("source_layout_id", "")
        reference = state.recovery_images.get(source_layout)
        if reference is not None:
            reference_observation = obs["observation_id"]
            break
    if reference is None:
        image = state.page_images.get(page_id)
        if image is None:
            return None
        reference = ScreenCapture(image[0], page.screenshot_hash, image[1])
    if not observations and layout_id(reference) == layout_id(current):
        return None

    covered_ids = {obs_id for record in graph.functional_coverage.values()
                   for obs_id in record.get("evidence_ids", [])}
    attempts, seen = [], set()
    for obs in reversed(observations):
        semantics = obs.get("action_semantics") or {}
        scope = obs.get("functional_scope") or {}
        covered = obs.get("observation_id") in covered_ids
        identity = json.dumps([scope, semantics, covered], sort_keys=True, ensure_ascii=False)
        if identity in seen:
            continue
        seen.add(identity)
        attempts.append(_bounded({
            "observation_id": obs["observation_id"],
            "source_layout_id": (obs.get("grounding") or {}).get("source_layout_id", ""),
            "control": obs.get("source_control", {}), "semantics": semantics,
            "scope": scope, "status": "covered" if covered else "attempted_not_covered",
            "execution_success": obs.get("execution_success", False),
            "destination_verified": obs.get("destination_verified", False),
            "operation_verified": obs.get("operation_verified"),
            "outcome": obs.get("outcome", ""),
        }))
        if len(attempts) >= 32:
            break

    known_controls, seen = [], set()
    for work in reversed(works):
        for action in work.planned_actions:
            scope = functional_scope(work, action, page_id=page_id)
            identity = json.dumps(scope, sort_keys=True, ensure_ascii=False)
            if identity in seen:
                continue
            seen.add(identity)
            progress = work.action_progress.get(local_action_key(action), {})
            covered = bool(state.work_registry and
                           state.work_registry.coverage_key(work, action) in graph.functional_coverage)
            known_controls.append(_bounded({
                "control_key": action.control_key, "action_family": action.action_family,
                "action_type": action.action_type, "control_type": action.element_type,
                "text": action.element_text, "description": action.target_description,
                "relevant_state_keys": action.relevant_state_keys,
                "target_value": action.target_value, "scope": scope,
                "status": "covered" if covered else "not_covered",
                "attempt_status": progress.get("status", "pending"),
            }))
            if len(known_controls) >= 32:
                break
        if len(known_controls) >= 32:
            break

    evidence = {
        "page_id": page_id, "page_description": page.page_description[:240],
        "reference_layout_id": layout_id(reference),
        "reference_observation_id": reference_observation,
        "attempts": attempts, "known_controls": known_controls,
        "history_is_partial": len(observations) > len(attempts) or
                              sum(len(work.planned_actions) for work in works) > len(known_controls),
        "coverage_meaning": "observed functional effect in this run; not proof of business success",
    }
    # A fixed bound keeps large list/state histories out of model context.
    while len(json.dumps(evidence, ensure_ascii=False)) > 24000:
        evidence["history_is_partial"] = True
        if known_controls:
            known_controls.pop()
        elif attempts:
            attempts.pop()
        else:
            break
    revision = sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return RevisitHistory(reference, evidence, revision)
