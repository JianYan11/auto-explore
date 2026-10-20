"""Persistent and in-memory state for an exploration run."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import time
from uuid import uuid4
from collections import deque
from copy import deepcopy
from dataclasses import asdict, replace
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path
from typing import Deque, Dict, Set, Tuple

from .actions import ActionCodec
from .config import ExplorerConfig
from .evidence import sanitize_inventory
from .contracts import (ActionSequenceStep, AppGraph, ExplorationAction, PageState,
                        PageVisualState, ScreenCapture, Transition, UIElement)
from .knowledge import (action_descriptor, action_key, classify_outcome, derive_experiences,
                        edge_evidence, exploration_checklist, page_descriptor, route_evidence)
from .visual import compare_screenshot_b64


class ExplorationState:
    """Single owner of graph state, queues, caches, and output artifacts."""

    def __init__(self, config: ExplorerConfig):
        self.config = config
        self.graph = AppGraph(
            app_package=config.app_package,
            app_name=config.app_package.split(".")[-1] if config.app_package else "unknown",
            pages={},
            transitions=[],
            exploration_time=0,
            run_id=uuid4().hex,
            memory_scope=config.memory_scope,
        )
        self.visited_hashes: Set[str] = set()
        self.hash_to_page_id: Dict[str, str] = {}
        self.page_counter = 0
        self.pending_pages: Deque[str] = deque()
        self.page_images: Dict[str, Tuple[str, Tuple[int, int]]] = {}
        self.state_images: Dict[str, str] = {}
        self.recovery_images: Dict[str, ScreenCapture] = {}
        self.transition_counter = 0
        Path(config.output_dir).mkdir(parents=True, exist_ok=True)
        # Evidence and its images survive later runs in the same output directory.
        self.run_dir = (Path(config.output_dir) / "runs" / self.graph.run_id).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.effective_config = {key: value for key, value in asdict(config).items()
                                 if not re.search(r"key|token|secret|password", key, re.I)}
        for key, value in self.effective_config.items():
            if "url" in key and isinstance(value, str) and value:
                url = urlsplit(value)
                self.effective_config[key] = urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
        source = hashlib.sha256()
        for file in sorted(Path(__file__).parent.rglob("*")):
            if file.suffix in {".py", ".md"}:
                source.update(str(file.relative_to(Path(__file__).parent)).encode())
                source.update(file.read_bytes())
        self.source_revision = source.hexdigest()
        (self.run_dir / "config.json").write_text(json.dumps({"run_id": self.graph.run_id,
            "source_revision": self.source_revision, "config": self.effective_config}, indent=2), encoding="utf-8")
        self.observation_log_path = self.run_dir / "observations.jsonl"
        self.historical_observations: list[dict] = []
        self.capability_service = None
        self.vlm_comparison_counter = 0
        self.vlm_comparison_log_path = Path(config.output_dir) / "vlm_comparisons.jsonl"
        # app_graph.json is replaced for every run, so its companion decision log
        # must also start clean instead of mixing records from an older run.
        self.vlm_comparison_log_path.write_text("", encoding="utf-8")
        self.coverage_checkpoint_log_path = Path(config.output_dir) / "coverage_checkpoints.jsonl"
        self.coverage_checkpoint_log_path.write_text("", encoding="utf-8")
        self.coverage_checkpoint_count = 0
        self.memory_planning_log_path = Path(config.output_dir) / "memory_planning.jsonl"
        self.memory_planning_log_path.write_text("", encoding="utf-8")
        self.memory_sources: list[str] = []
        self.work_registry = None  # Activated by the state exploration driver.

    def resolve_page_id(self, page_id: str) -> str:
        while page_id in self.graph.node_aliases:
            page_id = self.graph.node_aliases[page_id]
        return page_id

    def note_visit(self, page_id: str) -> None:
        self.graph.pages[self.resolve_page_id(page_id)].visit_count += 1

    def remaining_actions(self, page: PageState) -> list[ExplorationAction]:
        end = len(page.planned_actions)
        if self.config.max_actions_per_page > 0:
            end = min(end, self.config.max_actions_per_page)
        return page.planned_actions[page.next_action_index:end]

    def can_explore(self, page: PageState) -> bool:
        return (
            len(page.action_sequence) < self.config.max_depth
            and page.exploration_status not in {"recovery_failed", "planning_blocked"}
            and bool(self.remaining_actions(page))
        )

    def enqueue(self, page_id: str, *, first: bool = False) -> None:
        page_id = self.resolve_page_id(page_id)
        if page_id in self.pending_pages:
            self.pending_pages.remove(page_id)
        if first:
            self.pending_pages.appendleft(page_id)
        else:
            self.pending_pages.append(page_id)

    @staticmethod
    def page_order(page_id: str) -> tuple[int, str]:
        suffix = page_id.rsplit("_", 1)[-1]
        return (int(suffix) if suffix.isdigit() else 10**12, page_id)

    @staticmethod
    def _element_identity(element: UIElement) -> tuple:
        # IDs are local to a screenshot. Do not equate two unrelated elem_0's.
        # Repeated list rows may also share an Android resource_id.
        label = " ".join(element.text.casefold().split())
        bounds = tuple(sorted(element.bounds.items()))
        return (element.type.casefold(), element.resource_id, label, bounds)

    @staticmethod
    def _action_identity(action: ExplorationAction) -> tuple:
        return (
            action.element_id or action.target_description, action.action_type,
            action.input_text, tuple(sorted((action.swipe_start or {}).items())),
            tuple(sorted((action.swipe_end or {}).items())), action.duration_ms, action.verification_of,
        )

    def merge_pages(self, first_id: str, second_id: str) -> str:
        """Merge screenshot-verified duplicates without losing queued work or evidence."""
        first_id, second_id = self.resolve_page_id(first_id), self.resolve_page_id(second_id)
        if first_id == second_id:
            return first_id
        keep_id, remove_id = sorted((first_id, second_id), key=self.page_order)
        keep, removed = self.graph.pages[keep_id], self.graph.pages[remove_id]
        element_ids = {self._element_identity(element): element.id for element in keep.elements}
        used_ids = {element.id for element in keep.elements}
        remap = {}
        for element in removed.elements:
            identity = self._element_identity(element)
            element_id = element_ids.get(identity)
            if element_id is None:
                element_id = element.id
                while element_id in used_ids:
                    element_id = f"{remove_id}_{element_id}"
                keep.elements.append(replace(element, id=element_id))
                element_ids[identity] = element_id
                used_ids.add(element_id)
            remap[element.id] = element_id

        # Original observations keep their local UI IDs after node merging.
        # Preserve their mapping rather than interpreting two elem_0's as one UI.
        keep.control_aliases.update({f"{keep_id}:{e.id}": e.id for e in keep.elements})
        old_aliases = {f"{remove_id}:{e.id}": e.id for e in removed.elements}
        old_aliases.update(removed.control_aliases)
        keep.control_aliases.update({key: remap.get(value, value) for key, value in old_aliases.items()})

        # A processed duplicate must not become pending again after the merge.
        completed, pending = {}, {}
        for page in (keep, removed):
            for index, action in enumerate(page.planned_actions):
                if page is removed:
                    action = replace(action, element_id=remap.get(action.element_id, action.element_id))
                key = self._action_identity(action)
                if index < page.next_action_index:
                    completed.setdefault(key, action)
                else:
                    pending.setdefault(key, action)
        keep.planned_actions = list(completed.values()) + [
            action for key, action in pending.items() if key not in completed
        ]
        keep.next_action_index = len(completed)
        keep.recommended_actions = list(dict.fromkeys(
            keep.recommended_actions + [remap.get(item, item) for item in removed.recommended_actions]
        ))
        keep.visit_count += removed.visit_count
        # State IDs remain stable when their owning page is aliased. Raw evidence
        # and transitions can still resolve every original before/after state.
        keep.states.update(removed.states)
        keep.content_hints = list(dict.fromkeys(keep.content_hints + removed.content_hints))[:20]
        keep.activities = list(dict.fromkeys(
            item for item in keep.activities + removed.activities + [keep.activity, removed.activity] if item
        ))
        for page in (keep, removed):
            for key, value in page.semantic_state.items():
                values = keep.semantic_state_history.setdefault(key, [])
                if value not in values:
                    values.append(value)
        for key, values in removed.semantic_state_history.items():
            target = keep.semantic_state_history.setdefault(key, [])
            for value in values:
                if value not in target:
                    target.append(value)
        if len(removed.action_sequence) < len(keep.action_sequence):
            keep.action_sequence = list(removed.action_sequence)
        # Coordinates from another observation must be grounded again before use.
        keep.requires_action_relocation = True
        keep.exploration_status = "pending" if self.remaining_actions(keep) else "complete"

        for transition in self.graph.transitions:
            if transition.from_page == remove_id:
                transition.from_page = keep_id
                transition.element_id = remap.get(transition.element_id, transition.element_id)
                transition.action_details = replace(
                    transition.action_details,
                    element_id=remap.get(transition.action_details.element_id, transition.action_details.element_id),
                )
            if transition.to_page == remove_id:
                transition.to_page = keep_id
        self.graph.node_aliases[remove_id] = keep_id
        for alias in self.graph.node_aliases:
            self.graph.node_aliases[alias] = self.resolve_page_id(alias)
        if self.graph.root_page_id == remove_id:
            self.graph.root_page_id = keep_id
        self.graph.page_queue = list(dict.fromkeys(self.resolve_page_id(item) for item in self.graph.page_queue))
        self.graph.exploration_order = [self.resolve_page_id(item) for item in self.graph.exploration_order]
        queued = list(dict.fromkeys(self.resolve_page_id(item) for item in self.pending_pages))
        self.pending_pages.clear()  # Keep the deque object used by the loop's compatibility alias.
        self.pending_pages.extend(queued)
        for fingerprint, page_id in self.hash_to_page_id.items():
            self.hash_to_page_id[fingerprint] = self.resolve_page_id(page_id)
        self.page_images.pop(remove_id, None)
        del self.graph.pages[remove_id]
        if self.work_registry is not None:
            self.work_registry.rebase_pages()
        if self.can_explore(keep) and keep_id not in self.pending_pages:
            self.pending_pages.append(keep_id)
        self.save_graph(announce=False)
        return keep_id

    def record_coverage_checkpoint(self, record: dict) -> None:
        with self.coverage_checkpoint_log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.coverage_checkpoint_count += 1

    def record_memory_planning(self, record: dict) -> None:
        with self.memory_planning_log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def allocate_page_id(self) -> str:
        page_id = f"page_{self.page_counter}"
        self.page_counter += 1
        return page_id

    def save_page_screenshot(self, page_id: str, capture: ScreenCapture) -> str:
        if not self.config.save_screenshots:
            return ""
        screenshot_path = str(self.run_dir / f"{page_id}.png")
        Path(screenshot_path).write_bytes(base64.b64decode(capture.image_b64))
        return screenshot_path

    def save_analysis(self, page_id: str, analysis: dict) -> None:
        if not self.config.save_analysis:
            return
        analysis_path = Path(self.config.output_dir) / f"{page_id}.analysis.json"
        analysis_path.write_text(
            json.dumps(analysis, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        shutil.copyfile(analysis_path, self.run_dir / analysis_path.name)

    def register_page(self, page: PageState, capture: ScreenCapture) -> None:
        page.control_aliases.update({f"{page.page_id}:{e.id}": e.id for e in page.elements})
        page.visit_count += 1
        page.activities = [page.activity] if page.activity else []
        page.semantic_state_history = {key: [value] for key, value in page.semantic_state.items()}
        self.visited_hashes.add(capture.screenshot_hash)
        self.graph.pages[page.page_id] = page
        self.graph.page_queue.append(page.page_id)
        self.hash_to_page_id[capture.screenshot_hash] = page.page_id
        self.page_images[page.page_id] = (capture.image_b64, capture.screen_size)
        self.pending_pages.append(page.page_id)
        initial = self.observe_page_state(
            page, capture, semantic_state=page.semantic_state,
            page_description=page.page_description, activity=page.activity,
            action_sequence=page.action_sequence, screenshot_path=page.screenshot_path,
            observed_at=page.timestamp,
        )
        page.representative_state_id = initial.state_id

    def observe_page_state(
        self, page: PageState, capture: ScreenCapture, *,
        semantic_state: dict, page_description: str, activity: str,
        action_sequence: list[ActionSequenceStep], screenshot_path: str = "",
        observed_at: float | None = None,
        actionable_signature: str = "",
        behavioral_state: dict | None = None, context: dict | None = None,
    ) -> PageVisualState:
        """Retain model-observed state changes without tightening page identity."""
        for key, value in semantic_state.items():
            history = page.semantic_state_history.setdefault(key, [])
            if value not in history:
                history.append(deepcopy(value))
        if activity and activity not in page.activities:
            page.activities.append(activity)
        for state in page.states.values():
            if behavioral_state is not None and state.behavioral_state is not None:
                from .recovery import categorical_values, unknown_value
                if (state.activity == activity and state.context == (context or {})
                        and state.actionable_signature == actionable_signature
                        and categorical_values(state.behavioral_state) == categorical_values(behavioral_state)
                        and behavioral_state and not any(unknown_value(v) for v in behavioral_state.values())):
                    if self.work_registry is not None:
                        state.entry_routes = self.work_registry.unique_routes(
                            [*state.entry_routes, action_sequence], state.action_sequence)
                    return state
                continue
            if actionable_signature and state.actionable_signature != actionable_signature:
                continue
            if state.activity != activity or state.semantic_state != semantic_state:
                continue
            # The model already describes selected tags, fields and other values.
            # Equal known values reuse a state regardless of screenshot noise.
            if semantic_state:
                return state
            # Legacy analyzers may provide no semantic values. Reuse the existing
            # tolerant visual comparison, with its original thresholds, as fallback.
            image = self.state_images.get(state.state_id)
            if image is not None:
                comparison = compare_screenshot_b64(
                    capture.image_b64, image,
                    crop_top_ratio=self.config.fingerprint_crop_top_ratio,
                    crop_bottom_ratio=self.config.fingerprint_crop_bottom_ratio,
                    hash_distance_threshold=self.config.visual_hash_distance_threshold,
                    mean_difference_threshold=self.config.visual_mean_difference_threshold,
                )
                if comparison.same_screen or (
                    not comparison.available and capture.screenshot_hash == state.screenshot_hash
                ):
                    return state
        state_id = f"{page.page_id}_state_{len(page.states)}"
        state = PageVisualState(
            state_id=state_id,
            screenshot_path=screenshot_path or self.save_page_screenshot(state_id, capture),
            screenshot_hash=capture.screenshot_hash,
            activity=activity, page_description=page_description,
            semantic_state=deepcopy(semantic_state), action_sequence=deepcopy(action_sequence),
            timestamp=observed_at if observed_at is not None else time.time(),
            actionable_signature=actionable_signature,
            behavioral_state=deepcopy(behavioral_state), context=deepcopy(context or {}),
        )
        page.states[state_id] = state
        self.state_images[state_id] = capture.image_b64
        return state

    def record_vlm_comparison(
        self,
        *,
        context: str,
        candidate_fingerprint: str,
        existing_page: PageState,
        fast_visual: dict,
        vlm_result: dict,
        decision: str,
        rejection_reason: str = "",
        actionable_surface: dict | None = None,
        semantic_retrieval: dict | None = None,
    ) -> None:
        """Append one auditable VLM page-comparison decision as JSON Lines."""

        comparison_id = f"comparison_{self.vlm_comparison_counter:06d}"
        self.vlm_comparison_counter += 1
        record = {
            "comparison_id": comparison_id,
            "timestamp": time.time(),
            "context": context,
            "candidate_fingerprint": candidate_fingerprint,
            "existing_page_id": existing_page.page_id,
            "existing_fingerprint": existing_page.screenshot_hash,
            "fast_visual": fast_visual,
            "vlm_result": vlm_result,
            "actionable_surface": actionable_surface,
            "semantic_retrieval": semantic_retrieval,
            "decision": decision,
            "rejection_reason": rejection_reason,
        }
        with self.vlm_comparison_log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def save_transition_screens(
        self,
        before_capture: ScreenCapture,
        after_capture: ScreenCapture,
    ) -> Tuple[str, str]:
        transition_id = self.transition_counter
        self.transition_counter += 1
        if not self.config.save_screenshots:
            return "", ""
        transition_dir = self.run_dir / "transitions"
        transition_dir.mkdir(parents=True, exist_ok=True)
        prefix = transition_dir / f"transition_{transition_id:04d}"
        before_path = str(prefix.with_name(f"{prefix.name}_before.png"))
        after_path = str(prefix.with_name(f"{prefix.name}_after.png"))
        Path(before_path).write_bytes(base64.b64decode(before_capture.image_b64))
        Path(after_path).write_bytes(base64.b64decode(after_capture.image_b64))
        return before_path, after_path

    def record_transition(
        self,
        *,
        from_page: str,
        to_page: str,
        action: ExplorationAction,
        success: bool,
        state_changed: bool,
        before_screenshot: str,
        after_screenshot: str,
        recovery: str,
        observation_valid: bool = True,
        destination_verified: bool | None = None,
        visual_changed: bool | None = None,
        error_stage: str = "",
        error: str = "",
        outside_app: bool = False,
        external_activity: str = "",
        source_snapshot: dict | None = None,
        target_snapshot: dict | None = None,
        before_fingerprint: str = "",
        after_fingerprint: str = "",
        observed_at: float | None = None,
        from_state_id: str = "",
        to_state_id: str = "",
        from_work_id: str = "",
        to_work_id: str = "",
        purpose: str = "exploration",
        arrival_path: list[ActionSequenceStep] | None = None,
        current_layout_id: str = "",
        operation_verified: bool | None = None,
        operation_reason: str = "",
    ) -> dict:
        if destination_verified is None:
            destination_verified = success and observation_valid and to_page in self.graph.pages
        destination_verified = bool(success and observation_valid and destination_verified
                                    and to_page in self.graph.pages)
        source_page = self.graph.pages.get(from_page)
        target_page = self.graph.pages.get(to_page) if destination_verified else None
        # Unresolved/failed destinations must never acquire a fabricated state edge.
        from_state_id = from_state_id if source_page and from_state_id in source_page.states else ""
        to_state_id = to_state_id if target_page and to_state_id in target_page.states else ""
        if from_state_id and to_state_id and not from_work_id:
            state_changed = from_page != to_page or from_state_id != to_state_id
        outcome = classify_outcome(
            execution_success=success, observation_valid=observation_valid,
            destination_verified=destination_verified, same_page=from_page == to_page,
            visual_changed=state_changed if visual_changed is None else visual_changed or state_changed,
            error_stage=error_stage, outside_app=outside_app,
        )
        observed_source = source_snapshot if source_snapshot is not None else (
            page_descriptor(source_page) if source_page else {})
        work = self.graph.work_items.get(from_work_id)
        target_work = self.graph.work_items.get(to_work_id)
        from .work import functional_scope
        scope = functional_scope(work, action) if work else None
        observation = self.append_observation({
            "kind": "action", "from_page": from_page, "to_page": to_page,
            "from_state_id": from_state_id, "to_state_id": to_state_id,
            "from_work_id": from_work_id, "to_work_id": to_work_id, "purpose": purpose,
            "exploration_step": self.graph.completed_steps,
            "source": observed_source,
            "target": (target_snapshot or page_descriptor(target_page)) if target_page else None,
            "action": action_descriptor(action), "action_key": action_key(action),
            "element_id": action.element_id,
            "source_control": {"id": action.element_id, "type": action.element_type,
                               "text": action.element_text, "description": action.target_description},
            "grounding": {"source_layout_id": action.source_layout_id, "method": action.grounding_method,
                          "matches_current_layout": bool(current_layout_id and action.source_layout_id == current_layout_id)},
            "action_semantics": {"family": action.action_family, "intent": action.intent,
                                 "workflow_role": action.workflow_role, "coverage_scope": action.coverage_scope,
                                 "control_key": action.control_key, "target_value": action.target_value,
                                 "input_mode": action.input_mode, "effect_kind": action.effect_kind},
            "functional_scope": scope,
            "target_scope": ({"module": target_work.module, "state": target_work.normalized_state,
                              "context": target_work.context} if target_work else None),
            "work_context": ({"work_id": work.work_id, "state_key": work.state_key,
                              "context_key": work.context_key, "normalized_state": work.normalized_state,
                              "workflow_id": work.workflow_id} if work else {}),
            "execution_path": [asdict(step) for step in (arrival_path or [])],
            "conditions": {"source_state": observed_source.get("state", {}), "arrival_inputs": [
                {"target": step.target_description, "input_text": step.input_text}
                for step in (arrival_path if arrival_path is not None else
                             source_page.action_sequence if source_page else []) if step.input_text
            ]},
            "execution_success": success, "observation_valid": observation_valid,
            "execution_attempted": error_stage != "invalid_action",
            "destination_verified": destination_verified, "business_success": None,
            "operation_verified": operation_verified, "operation_reason": operation_reason,
            "outcome": outcome, "state_changed": state_changed,
            "outside_app": outside_app, "external_activity": external_activity,
            "visual_changed": visual_changed, "recovery": recovery,
            "error_stage": error_stage, "error": error,
            "before_screenshot": before_screenshot, "after_screenshot": after_screenshot,
            "before_fingerprint": before_fingerprint, "after_fingerprint": after_fingerprint,
            "verification_of": action.verification_of,
            "timestamp": observed_at if observed_at is not None else time.time(),
        })
        for page, state_id in ((source_page, from_state_id), (target_page, to_state_id)):
            if page and state_id:
                evidence = page.states[state_id].observation_ids
                if observation["observation_id"] not in evidence:
                    evidence.append(observation["observation_id"])
        # Invalid-coordinate skips are evidence, but were never executed transitions.
        if error_stage == "invalid_action":
            return observation
        tap_x = None
        tap_y = None
        if action.bounds.get("width", 0) > 0 and action.bounds.get("height", 0) > 0:
            tap_x, tap_y = ActionCodec.tap_point_for_bounds(action.bounds)
        self.graph.transitions.append(
            Transition(
                from_page=from_page,
                to_page=to_page,
                action=action.action_type,
                element_id=action.element_id,
                element_text=action.element_text,
                tap_x=tap_x,
                tap_y=tap_y,
                success=success,
                state_changed=state_changed,
                action_details=action,
                before_screenshot=before_screenshot,
                after_screenshot=after_screenshot,
                recovery=recovery,
                observation_id=observation["observation_id"], outcome=outcome,
                destination_verified=destination_verified,
                from_state_id=from_state_id, to_state_id=to_state_id,
                from_work_id=from_work_id, to_work_id=to_work_id, purpose=purpose,
            )
        )
        return observation

    def append_observation(self, record: dict) -> dict:
        record = {
            **record, "schema_version": 1, "run_id": self.graph.run_id,
            "observation_id": f"{self.graph.run_id}:{len(self.graph.observations)}",
            "app_package": self.config.app_package, "memory_scope": self.config.memory_scope,
            "evidence_log": str(self.observation_log_path),
            "timestamp": record.get("timestamp", time.time()),
        }
        # Serialize before appending: caller-owned dictionaries cannot mutate evidence.
        encoded = json.dumps(record, ensure_ascii=False)
        with self.observation_log_path.open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
        self.graph.observations.append(json.loads(encoded))
        return record

    def checklist(self, page: PageState) -> list[dict]:
        return exploration_checklist(self.graph, page)

    def schedule_verification(self, page: PageState, action: ExplorationAction) -> bool:
        if not self.config.graph_memory or not self.config.verification_retries:
            return False
        if self.config.max_actions_per_page and len(page.planned_actions) >= self.config.max_actions_per_page:
            return False
        key = action_key(action)
        matching = [obs for obs in self.graph.observations if obs.get("kind") == "action"
                    and self.resolve_page_id(obs.get("from_page", "")) == page.page_id
                    and obs.get("action_key") == key]
        if not matching or matching[-1]["outcome"] not in {
            "execution_failed", "observation_failed", "destination_unresolved",
            "no_visible_change", "same_page_changed",
        }:
            return False
        origin = action.verification_of or matching[-1]["observation_id"]
        if sum(obs.get("verification_of") == origin for obs in self.graph.observations) >= self.config.verification_retries:
            return False
        if any(action_key(pending) == key for pending in page.planned_actions[page.next_action_index:]):
            return False
        page.planned_actions.append(replace(action, verification_of=origin))
        return True

    def experiences(self) -> list[dict]:
        return derive_experiences(self.historical_observations + self.graph.observations)

    def save_graph(self, *, announce: bool = True) -> None:
        output_path = Path(self.config.output_dir) / "app_graph.json"
        works = [work for work in self.graph.work_items.values() if work.status != "merged"]
        progress = [value for work in works for value in work.action_progress.values()]
        termination = {"reason": self.graph.termination_reason,
            "work_status_counts": {status: sum(w.status == status for w in works) for status in sorted({w.status for w in works})},
            "action_status_counts": {status: sum(p["status"] == status for p in progress) for status in sorted({p["status"] for p in progress})},
            "current_run_covered_branches": len(self.graph.functional_coverage)}
        graph_dict = {
            "termination": termination, "timings": dict(self.graph.timings),
            "timings_are_inclusive": True,
            "effective_config": self.effective_config, "source_revision": self.source_revision,
            "schema_version": 4 if self.work_registry is not None else 3,
            "identity_contract": "template_state_v1",
            "coverage_contract": "action_dependencies_v1",
            "exploration_mode": "state" if self.work_registry is not None else "page",
            "work_items": {key: asdict(item) for key, item in self.graph.work_items.items()},
            "work_queue": self.graph.work_queue,
            "work_order": self.graph.work_order,
            "work_aliases": self.graph.work_aliases,
            "functional_coverage": self.graph.functional_coverage,
            "baseline": self.graph.baseline,
            "state_budgets": {key: getattr(self.config, key) for key in (
                "max_states_per_page", "max_actions_per_state", "continuous_steps", "max_transition_repeats")},
            "model_roles": {"execution": self.config.model,
                            "experience_summary": self.config.summary_model.strip() or None},
            "summary_kind": "app_capabilities" if self.capability_service else "evidence_only",
            "app_capabilities": self.capability_service.snapshot() if self.capability_service else None,
            "run_id": self.graph.run_id,
            "memory_scope": self.config.memory_scope,
            "observation_log": str(self.observation_log_path),
            "observations": self.graph.observations,
            "inherited_observations": list({obs["observation_id"]: obs
                                            for obs in self.historical_observations}.values()),
            "edge_evidence": edge_evidence(self.graph),
            "route_evidence": route_evidence(self.graph),
            "experiences": [] if self.capability_service else self.experiences(),
            "app_package": self.graph.app_package,
            "app_name": self.graph.app_name,
            "root_page_id": self.graph.root_page_id,
            "page_queue": self.graph.page_queue,
            "exploration_order": self.graph.exploration_order,
            "completed_steps": self.graph.completed_steps,
            "node_aliases": self.graph.node_aliases,
            "coverage_checkpoint_log": str(self.coverage_checkpoint_log_path),
            "coverage_checkpoint_count": self.coverage_checkpoint_count,
            "memory_planning_log": str(self.memory_planning_log_path),
            "memory_sources": self.memory_sources,
            "vlm_comparison_log": str(self.vlm_comparison_log_path),
            "vlm_comparison_count": self.vlm_comparison_counter,
            "pages": {
                page_id: {
                    "page_id": page.page_id,
                    "screenshot_path": page.screenshot_path,
                    "screenshot_hash": page.screenshot_hash,
                    "activity": page.activity,
                    "page_type": page.page_type,
                    "app_package": page.app_package,
                    "page_description": page.page_description,
                    "semantic_state": page.semantic_state,
                    "semantic_state_history": page.semantic_state_history,
                    "representative_state_id": page.representative_state_id,
                    "states": {state_id: asdict(state) for state_id, state in page.states.items()},
                    "activities": page.activities,
                    "visit_count": page.visit_count,
                    "requires_action_relocation": page.requires_action_relocation,
                    "content_hints": page.content_hints,
                    "content_inventory": sanitize_inventory(page.content_inventory),
                    "control_aliases": page.control_aliases,
                    "description_embedding": page.description_embedding,
                    "embedding_model": page.embedding_model,
                    "semantic_error": page.semantic_error,
                    "elements": [asdict(element) for element in page.elements],
                    "recommended_actions": page.recommended_actions,
                    "planned_actions": [asdict(action) for action in page.planned_actions],
                    "action_sequence": [asdict(step) for step in page.action_sequence],
                    "timestamp": page.timestamp,
                    "next_action_index": page.next_action_index,
                    "exploration_status": page.exploration_status,
                    "exploration_checklist": self.checklist(page),
                    "state_exploration": self.work_registry.page_summary(page_id) if self.work_registry else None,
                }
                for page_id, page in self.graph.pages.items()
            },
            "transitions": [asdict(transition) for transition in self.graph.transitions],
            "state_transition_triplets": [
                {
                    "before_screenshot": transition.before_screenshot,
                    "after_screenshot": transition.after_screenshot,
                    "action": {key: value for key, value in asdict(transition.action_details).items()
                               if key not in {"verification_of", "source_layout_id",
                                              "source_screen_size", "grounding_method", "action_family", "intent",
                                              "coverage_scope", "relevant_state_keys", "workflow_role", "replay_safe",
                                              "control_key", "field_key", "field_value", "target_value", "input_mode", "effect_kind"}},
                }
                for transition in self.graph.transitions
            ],
            "exploration_time": self.graph.exploration_time,
        }
        encoded = json.dumps(graph_dict, indent=2, ensure_ascii=False)
        output_path.write_text(encoded, encoding="utf-8")
        (self.run_dir / "app_graph.json").write_text(encoded, encoding="utf-8")
        if self.work_registry is not None:
            topology = {"schema_version": 1, "identity_contract": "template_state_v1",
                "templates": {page.page_id: page.page_description for page in self.graph.pages.values()},
                "nodes": [{"id": w.work_id, "template_id": w.page_id, "state": w.normalized_state,
                    "context": w.context, "visual_state_ids": w.observed_state_ids,
                    "entry_path": [asdict(step) for step in w.entry_path],
                    "entry_routes": [[asdict(step) for step in route] for route in w.entry_routes],
                    "status": w.status} for w in works],
                "edges": [{"from": self.work_registry.resolve(t.from_work_id),
                    "to": self.work_registry.resolve(t.to_work_id),
                    "action": t.action_details.target_description, "purpose": t.purpose,
                    "observation_id": t.observation_id} for t in self.graph.transitions
                    if t.destination_verified and t.from_work_id and t.to_work_id],
                "shared_coverage": self.graph.functional_coverage}
            state_encoded = json.dumps(topology, indent=2, ensure_ascii=False)
            (output_path.parent / "state_graph.json").write_text(state_encoded, encoding="utf-8")
            (self.run_dir / "state_graph.json").write_text(state_encoded, encoding="utf-8")
        for log_path in (self.vlm_comparison_log_path, self.coverage_checkpoint_log_path, self.memory_planning_log_path):
            shutil.copyfile(log_path, self.run_dir / log_path.name)
        if self.capability_service:
            self.capability_service.save()
        if announce:
            print(f"\n图谱已保存: {output_path}")
            print(f"  - 页面数: {len(self.graph.pages)}")
            print(f"  - 转移数: {len(self.graph.transitions)}")
            print(f"  - 耗时: {self.graph.exploration_time:.1f}s")
            print(f"  - 停止原因: {self.graph.termination_reason}；工作项状态: {termination['work_status_counts']}")
