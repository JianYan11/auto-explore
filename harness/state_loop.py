"""State-oriented exploration with bounded live continuation and guarded recovery."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import time

from .actions import ActionCodec
from .contracts import ScreenCapture
from .grounding import matches_source, state_image_id, with_source
from .controls import input_value, overlay_id
from .knowledge import digest
from .metrics import timed_phase
from .work import WorkRegistry, guard_matches, local_action_key, observation_profile, unknown
from .identity import behavioral_changes


@dataclass
class Position:
    page: object
    visual: object
    work: object
    capture: ScreenCapture
    guard: dict
    path: list


class StateExplorer:
    def __init__(self, explorer):
        self.explorer = explorer
        self.state = explorer.state
        self.graph = explorer.graph
        self.config = explorer.config
        self.registry = WorkRegistry(self.state)
        self.state.work_registry = self.registry
        self.current: Position | None = None
        self.round_pages: set[str] = set()
        self.rotated = False
        self.app_return_failed = False
        self.external_source: Position | None = None

    @timed_phase("analysis_seconds")
    def profile(self, page, capture, path, *, force=False):
        activity = self.explorer.controller.get_current_activity()
        analysis = self.explorer._analyze_screen(capture, path, activity, force=force, page=page)
        analysis = deepcopy(analysis)
        elements = self.explorer._elements_from_analysis(analysis)
        actions = self.explorer._planned_actions_from_analysis(analysis, elements, analysis.get("recommended_actions", []))
        actions = [with_source(action, capture, "analysis") for action in actions]
        semantic = self.explorer.verifier.semantic.describe(capture)
        profile = observation_profile(replace(page, activity=activity), capture, analysis, actions,
                                      semantic.state if not semantic.error else {})
        # Pairwise image verification can see a selected local tab omitted by the
        # standalone description. Carry that evidence into state identity.
        for key, value in semantic.comparison_state.items():
            if key == "mode" or key == "overlay" or key.endswith(("_tab", "_mode", "_overlay")):
                from .recovery import normalized_label
                profile["state"][key] = normalized_label(value) if value is not None else "unknown"
                profile["values"][key] = profile["state"][key]
                if key not in profile["recovery_value_keys"]:
                    profile["recovery_value_keys"].append(key)
        profile["state_image_id"] = state_image_id(capture, self.config.fingerprint_crop_top_ratio,
                                                  self.config.fingerprint_crop_bottom_ratio)
        self.state.recovery_images[profile["layout_id"]] = capture
        established = next((w for w in self.graph.work_items.values() if w.page_id == page.page_id), None)
        if established:
            profile["module"] = established.module
        if profile["conflicts"] and not force:
            # One bounded re-observation; unresolved conflicts remain blocked, not merged away.
            self.explorer.verifier.semantic.cache.clear()
            return self.profile(page, capture, path, force=True)
        return profile, actions, elements, analysis

    def observe(self, page, capture, path, *, workflow_id="", finish_step=False, vlm_verified=False):
        profile, actions, elements, analysis = self.profile(page, capture, path)
        if any(unknown(value) for values in (profile["state"], profile["context"]) for value in values.values()):
            ambiguous = any(work.page_id == page.page_id and work.normalized_state == profile["state"]
                            and work.context == profile["context"]
                            and not guard_matches(work.restore_guard, profile)
                            for work in self.graph.work_items.values())
            if ambiguous:
                profile, actions, elements, analysis = self.profile(page, capture, path, force=True)
        path = deepcopy(path)
        if finish_step and path:
            path[-1] = replace(path[-1], target_guard=deepcopy(profile))
        # Draft identity survives excursions into pickers. It does not depend on typed text.
        if analysis.get("page_type") == "form" or profile["state"].get("mode") in {"create", "edit"}:
            workflow_id = digest((self.graph.app_package, profile["module"], profile["context"]))
        elif analysis.get("page_type") in {"home", "list"}:
            workflow_id = ""
        visual = self.state.observe_page_state(
            page, capture, semantic_state=profile["values"] or profile["state"],
            page_description=page.page_description, activity=profile["activity"], action_sequence=path,
            actionable_signature=profile["surface"],
            behavioral_state=profile["state"], context=profile["context"],
        )
        work = self.registry.get_or_create(page, visual, profile, actions, elements, path,
                                           workflow_id=workflow_id, vlm_verified=vlm_verified)
        if work is not None:
            self.graph.exploration_order.append(page.page_id)
        return Position(page, visual, work, capture, profile, path)

    @timed_phase("recovery_seconds")
    def restore(self, work):
        restored = self.explorer.lifecycle.restore_to_work_item(work)
        if restored is None:
            if not self.explorer._is_target_activity(self.explorer.controller.get_current_activity()):
                self.app_return_failed = True
                self.current = None
                self.registry.enqueue(work, first=True)
                return False
            work.restore_failures += 1
            work.status = "recovery_failed"
            self.current = None
            last = getattr(self.explorer.lifecycle, "last_restore_position", None)
            if last:
                capture, guard, path = last
                page = self.graph.pages.get(guard.get("page_id"))
                if page is None:
                    page, _ = self.explorer._capture_or_get_page(capture, path)
                if page is not None:
                    self.current = self.observe(page, capture, path, finish_step=bool(path))
                    if self.current.path and not self.current.path[-1].arrival_verified:
                        actual_path = deepcopy(self.current.path)
                        try:
                            matched, reason = self.explorer.verifier.verify_replay_state(
                                capture, self.current.guard, action=actual_path[-1])
                        except Exception as exc:
                            matched, reason = False, str(exc)
                        actual_path[-1] = replace(actual_path[-1], arrival_verified=matched)
                        self.current.path = actual_path
                        if self.current.work:
                            self.current.work.entry_path = deepcopy(actual_path)
                        self.state.append_observation({"kind": "state_restore", "event": "observed_branch_verification",
                            "work_id": self.current.work.work_id if self.current.work else "",
                            "page_id": self.current.page.page_id, "arrived": matched, "reason": reason})
                    if self.current.work:
                        self.state.append_observation({"kind": "state_restore", "event": "observed_branch",
                            "work_id": work.work_id, "observed_work_id": self.current.work.work_id,
                            "page_id": self.current.page.page_id, "arrived": False,
                            "reason": "requested_route_rejected_actual_branch_retained"})
            return False
        capture, path = restored
        page = self.graph.pages[self.state.resolve_page_id(work.page_id)]
        position = self.observe(page, capture, path, workflow_id=work.workflow_id,
                                finish_step=bool(path), vlm_verified=True)
        if position.work is not None and position.work is not work:
            reconciled = self.registry.reconcile_restored_work(work, position.work, vlm_verified=True)
            if reconciled is not None:
                position.work = reconciled
        if position.work is None or self.registry.resolve(position.work.work_id) != self.registry.resolve(work.work_id):
            work.status = "state_unresolved"
            self.current = position  # Preserve the observed branch and its independently guarded route.
            return False
        self.current = position
        return True

    def pick(self, burst):
        self.rotated = False
        if self.app_return_failed:
            return None
        self.refresh_current_aliases()
        if self.current is None and not self.graph.work_queue:
            return None
        if not self.explorer._is_target_activity(self.explorer.controller.get_current_activity()):
            self.current = None
            if not self.explorer.lifecycle.return_to_target_app():
                self.app_return_failed = True
                return None
        if self.external_source:
            source = self.external_source
            capture = self.explorer._capture_screen()
            matched, _ = self.explorer.verifier.verify_replay_state(capture, source.guard)
            if matched:
                self.current = self.observe(source.page, capture, source.path,
                                            workflow_id=source.work.workflow_id if source.work else "",
                                            vlm_verified=True)
            self.external_source = None
        if self.current and self.current.work:
            work = self.graph.work_items[self.registry.resolve(self.current.work.work_id)]
            self.current.work = work
            candidates = self.registry.candidates(work, allow_navigation=bool(self.current.path))
            if candidates and burst < self.config.continuous_steps:
                return work, candidates
            # Only a real draft can borrow two completion steps, and only its completion actions.
            finishing = [(action, purpose) for action, purpose in candidates
                         if action.intent == "commit" or action.workflow_role == "return"]
            if work.workflow_id and finishing and burst < self.config.continuous_steps + 2:
                return work, finishing
            self.registry.enqueue(work)
            if work.work_id in self.graph.work_queue:
                self.graph.work_queue.remove(work.work_id)
                self.graph.work_queue.append(work.work_id)
        while self.graph.work_queue:
            work = self.graph.work_items[self.registry.resolve(self.graph.work_queue.pop(0))]
            candidates = self.registry.candidates(work)
            if not candidates:
                self.registry.settle(work)
                continue
            if not self.current or self.current.work is None or work.work_id != self.current.work.work_id:
                if not self.restore(work):
                    if self.app_return_failed:
                        return None
                    continue
            self.rotated = True
            return work, self.registry.candidates(work, allow_navigation=bool(self.current.path))
        return None

    def refresh_current_aliases(self):
        if not self.current:
            return
        self.current.page = self.graph.pages[self.state.resolve_page_id(self.current.page.page_id)]
        self.current.guard = self.registry.canonical_guard(self.current.guard)
        if self.current.work:
            self.current.work = self.graph.work_items[self.registry.resolve(self.current.work.work_id)]

    def run(self, root_page, initial_capture):
        self.current = self.observe(root_page, initial_capture, [])
        self.graph.baseline = deepcopy(self.current.guard)
        burst = 0
        while not self.explorer._step_budget_exhausted():
            choice = self.pick(burst)
            if choice is None:
                break
            work, candidates = choice
            if self.rotated:
                burst = 0
            self.registry.take(work)
            self.round_pages.add(work.page_id)
            if not self.step(work, candidates):
                self.finish_round()
                burst = 0
            else:
                burst += 1
                if burst >= self.config.continuous_steps:
                    self.finish_round()
            self.update_page_summaries()
            self.state.save_graph(announce=False)
            if self.explorer.coverage_checkpoint.should_run(self.graph.completed_steps):
                current_id = self.current.work.work_id if self.current and self.current.work else work.work_id
                chosen = self.explorer.coverage_checkpoint.run_work_checkpoint(current_id)
                self.refresh_current_aliases()
                if chosen and (not self.current or not self.current.work or chosen != self.current.work.work_id):
                    self.restore(self.graph.work_items[self.registry.resolve(chosen)])
                self.finish_round()
                burst = 0
        for work in self.graph.work_items.values():
            self.registry.settle(work)
            if self.explorer._step_budget_exhausted() and self.registry.candidates(work):
                self.registry.limit(work, "max_steps")
        self.graph.work_queue[:] = [key for key in dict.fromkeys(self.registry.resolve(key) for key in self.graph.work_queue)
                                   if self.graph.work_items[key].status in {"pending", "exploring"}]
        self.update_page_summaries()
        self.state.pending_pages.clear()  # Page queue is a compatibility discovery index only.
        statuses = {w.status for w in self.graph.work_items.values()}
        page_limited = (self.graph.termination_reason == "budget_limited" or
                        any(page.exploration_status == "budget_limited" for page in self.graph.pages.values()))
        self.graph.termination_reason = ("blocked" if self.app_return_failed else
                                         "budget_limited" if "budget_limited" in statuses or page_limited else
                                         "blocked" if statuses - {"complete", "merged"} else "frontier_exhausted")
        self.state.save_graph(announce=False)
        self.finish_round()

    def finish_round(self):
        self.explorer.capability_service.update(reason="state_round")
        self.round_pages.clear()

    def step(self, work, candidates):
        before = self.current
        actions = [action for action, _ in candidates]
        selection = self.explorer.memory_planner.select(before.page, before.capture, actions, work=work,
                                                        current_guard=before.guard)
        if selection is None:
            work.status = "planning_blocked"
            return False
        index, action = selection
        original, purpose = candidates[index]
        if not matches_source(action, before.capture) or not self.explorer._has_executable_action(action, before.capture.screen_size):
            work.status = "planning_blocked"
            return False
        for i, planned in enumerate(work.planned_actions):
            if local_action_key(planned) == local_action_key(original):
                work.planned_actions[i] = action
                break
        after = None
        capture = None
        executed = observation_valid = outside = False
        stage, error = "execution", ""
        before_path = after_path = ""
        operation_verified = None
        operation_reason = ""
        external_activity = ""
        observed_at = time.time()
        previous = input_value(action, before.guard, work.planned_actions)
        try:
            started = time.monotonic()
            try:
                executed = bool(self.explorer.controller.execute_action(action, before.capture.screen_size))
            finally:
                self.graph.timings["execution_seconds"] = self.graph.timings.get("execution_seconds", 0) + time.monotonic() - started
            stage = "observation"
            capture = self.explorer._capture_screen()
            observation_valid = True
            stage = "identification"
            activity = self.explorer.controller.get_current_activity()
            if not activity:
                raise ValueError("foreground_activity_unavailable")
            outside = not self.explorer._is_target_activity(activity)
            external_activity = activity if outside else ""
            if executed or (action.action_type == "input" and callable(getattr(self.explorer.controller, "input_locator", None))):
                stage = "identification"
                step = replace(ActionCodec.to_sequence_step(action, before.capture.screen_size),
                               source_guard=deepcopy(before.guard), replay_safe=executed and action.replay_safe)
                path = before.path + [step]
                page, created = (None, False) if outside else self.explorer._capture_or_get_page(
                    capture, path, source_action=action)
                if outside:
                    operation_reason = "external_app_boundary"
                if page is not None:
                    after = self.observe(page, capture, path, workflow_id=work.workflow_id, finish_step=True)
                    if created:
                        page.action_sequence = deepcopy(after.path)
                    if action.action_type == "input":
                        actual = input_value(action, after.guard, after.work.planned_actions if after.work else [])
                        expected = ("" if action.input_mode == "clear" else action.input_text)
                        if action.input_mode == "append":
                            expected = previous + action.input_text if isinstance(previous, str) else None
                        operation_verified = actual is not None and expected is not None and actual == expected
                        operation_reason = "input_value_verified" if operation_verified else "input_value_mismatch_or_unobserved"
                        self.state.append_observation({"kind": "input_verification", "control_key": action.control_key,
                            "field_key": action.field_key, "expected": expected, "actual": actual,
                            "verified": operation_verified, "from_work_id": work.work_id})
                    elif action.effect_kind == "query":
                        submitted = after.guard["values"].get("submitted_query")
                        draft = before.guard["values"].get("draft_query")
                        operation_verified = (draft is not None and submitted == draft
                                              and not overlay_id({"exploration": after.guard}))
                        operation_reason = "query_verified" if operation_verified else "query_submission_unverified"
            stage = ""
        except Exception as exc:
            error = str(exc)
        finally:
            if self.config.save_screenshots:
                before_path, saved_after = self.explorer._save_transition_screens(before.capture, capture or before.capture)
                after_path = saved_after if observation_valid else ""
            target = after.work if after else None
            observation = self.state.record_transition(
                from_page=before.page.page_id, to_page=after.page.page_id if after else "",
                action=action, success=executed, state_changed=bool(after and behavioral_changes(before.guard, after.guard)),
                before_screenshot=before_path, after_screenshot=after_path, recovery="continued" if after else "pending_recovery",
                observation_valid=observation_valid, destination_verified=after is not None,
                error_stage=stage, error=error, outside_app=outside,
                external_activity=external_activity,
                source_snapshot=self.snapshot(before), target_snapshot=self.snapshot(after) if after else None,
                before_fingerprint=before.capture.screenshot_hash,
                after_fingerprint=capture.screenshot_hash if capture else "", observed_at=observed_at,
                from_state_id=before.visual.state_id, to_state_id=after.visual.state_id if after else "",
                from_work_id=work.work_id, to_work_id=target.work_id if target else "",
                purpose=purpose, arrival_path=before.path,
                current_layout_id=before.guard["layout_id"],
                operation_verified=operation_verified, operation_reason=operation_reason,
            )
            self.graph.completed_steps += 1
            self.registry.record_result(work, original, action, target, before.guard,
                                        after.guard if after else {}, observation, purpose)
            if action.intent == "commit" or action.workflow_role == "return":
                self.explorer.capability_service.update(reason="workflow_boundary")
        # No automatic Back after an effect: the next state retains its live draft and full trace.
        self.current = after
        if outside:
            self.external_source = before
        if after and after.work:
            self.registry.enqueue(after.work)
        return after is not None

    @staticmethod
    def snapshot(position):
        return {"description": position.page.page_description or position.page.page_type,
                "activity": position.guard["activity"], "state": deepcopy(position.guard["values"]),
                "controls": [action.action_family or action.target_description for action in position.work.planned_actions]
                            if position.work else [], "context": deepcopy(position.guard["context"])}

    def update_page_summaries(self):
        for page in self.graph.pages.values():
            works = [work for work in self.graph.work_items.values() if work.page_id == page.page_id and work.status != "merged"]
            statuses = {work.status for work in works}
            page.next_action_index = sum(work.attempts for work in works)
            if page.exploration_status == "budget_limited" or "budget_limited" in statuses:
                page.exploration_status = "budget_limited"
            elif not works:
                page.exploration_status = "blocked"
            elif statuses & {"pending", "exploring"}:
                page.exploration_status = "pending"
            elif statuses - {"complete"}:
                page.exploration_status = "blocked"
            else:
                page.exploration_status = "complete"
