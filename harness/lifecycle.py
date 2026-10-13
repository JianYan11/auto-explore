"""Startup, recovery, replay, and shutdown policies."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Optional, Tuple

from .actions import ActionCodec
from .config import ExplorerConfig
from .contracts import ActionSequenceStep, PageState, ScreenCapture, StateWorkItem
from .verify import ExplorationVerifier
from .state import ExplorationState
from .memory import GraphMemory
from .grounding import bind_location, matches_source
from .work import replayable_route


class ExplorationLifecycle:
    """Owns device lifecycle and recovery without scheduling exploration work."""

    def __init__(
        self,
        config: ExplorerConfig,
        controller: object,
        analyzer: object,
        verifier: ExplorationVerifier,
        *, state: ExplorationState | None = None, memory: GraphMemory | None = None,
    ):
        self.config = config
        self.controller = controller
        self.analyzer = analyzer
        self.verifier = verifier
        self.state = state
        self.memory = memory

    def start(self) -> None:
        self.controller.connect()

    def stop(self) -> None:
        self.controller.quit()

    def capture_screen(self) -> ScreenCapture:
        screenshot_b64, screenshot_hash, screen_size = self.controller.capture_screenshot()
        return ScreenCapture(screenshot_b64, screenshot_hash, screen_size)

    def return_to_target_app(self) -> bool:
        """Leave external screens without exploring them; recovery is globally bounded."""
        activity = self.controller.get_current_activity()
        if self.verifier.is_target_activity(activity):
            return True

        def log(method, arrived, **details):
            if self.state:
                self.state.append_observation({"kind": "app_return", "method": method,
                    "outside_activity": activity, "arrived": arrived,
                    "current_activity": self.controller.get_current_activity(), **details})

        for attempt in range(2):
            if not activity:
                break  # Do not press Back blindly when foreground ownership is unknown.
            try:
                if not self.controller.go_back():
                    break
                arrived = self.verifier.is_target_activity(self.controller.get_current_activity())
                log("back", arrived, attempt=attempt + 1)
                if arrived:
                    return True
            except Exception as exc:
                log("back", False, attempt=attempt + 1, error=str(exc))
                break
        try:
            self.controller.restart_app()
            arrived = self.verifier.is_target_activity(self.controller.get_current_activity())
            log("restart", arrived)
            return arrived
        except Exception as exc:
            log("restart", False, error=str(exc))
            return False

    def restore_after_action(
        self,
        parent_page: PageState,
        before_capture: ScreenCapture,
        after_capture: ScreenCapture,
        next_page: Optional[PageState],
    ) -> Tuple[Optional[ScreenCapture], str]:
        if not self.verifier.is_target_activity(self.controller.get_current_activity()):
            if not self.return_to_target_app():
                return None, "app_return_failed"
            returned = self.capture_screen()
            if self.verifier.screen_matches_page(returned, parent_page):
                return returned, "external_return"
            replay_capture = self.replay_to_page(parent_page)
            return (replay_capture, "replay") if replay_capture is not None else (None, "failed")
        if self.verifier.captures_are_visually_same(after_capture, before_capture):
            return after_capture, "not_needed"

        if next_page is not None and next_page.page_id == parent_page.page_id:
            replay_capture = self.replay_to_page(parent_page)
            return (replay_capture, "replay") if replay_capture is not None else (None, "failed")
        if next_page is None and self.verifier.screen_matches_page(after_capture, parent_page):
            replay_capture = self.replay_to_page(parent_page)
            return (replay_capture, "replay") if replay_capture is not None else (None, "failed")

        if self.config.back_after_action and self.controller.go_back():
            try:
                back_capture = self.capture_screen()
                if self.verifier.screen_matches_page(back_capture, parent_page):
                    return back_capture, "back"
                print("  Back 未恢复目标页面，改用重启回放")
            except Exception as exc:
                print(f"  Back 后验证失败: {exc}")

        replay_capture = self.replay_to_page(parent_page)
        return (replay_capture, "replay") if replay_capture is not None else (None, "failed")

    def replay_to_page(self, target_page: PageState) -> Optional[ScreenCapture]:
        # Legacy semantic-only paths have nothing to execute without relocation.
        has_coordinates = all(
            (step.swipe_start is not None and step.swipe_end is not None)
            if step.action_type == "swipe" else bool(step.bounds)
            for step in target_page.action_sequence
        )
        modes = (["coordinates"] if has_coordinates else []) + ["retry"] * max(1, self.config.replay_attempts)
        for attempt, mode in enumerate(modes, start=1):
            label = "回放" if mode == "coordinates" else "重试回放"
            print(f"  重启并回放到 {target_page.page_id}（{label}，{attempt}/{len(modes)}）")
            arrived = False
            current_capture = None
            failed_step = None
            reason = "restart_failed"
            relocated_steps = []
            relocated_indices = []
            try:
                self.controller.restart_app()
                current_capture = self.capture_screen()
                if not self.verifier.is_target_activity(self.controller.get_current_activity()):
                    reason = "outside_app_after_restart"
                    break
                replay_ok = True
                for index, step in enumerate(target_page.action_sequence):
                    failed_step = index
                    action = ActionCodec.action_from_sequence_step(step)
                    if not matches_source(action, current_capture):
                        # Reordering/scrolling/restoration can keep the page identity while
                        # changing every target position. Unknown legacy sources also relocate.
                        reason = "target_not_found"
                        locate = getattr(self.analyzer, "locate_replay_action_with_memory", None)
                        if callable(locate) and self.memory and self.state and self.config.graph_memory:
                            context = self.memory.context(
                                self.state.graph, query=step.target_description,
                                target_page_id=target_page.page_id, activity=target_page.activity,
                                action_sequence=target_page.action_sequence[:index], purpose="replay",
                            )
                            raw_action = locate(current_capture.image_b64, current_capture.screen_size, step, context)
                        else:
                            raw_action = self.analyzer.locate_replay_action(
                                current_capture.image_b64, current_capture.screen_size, step,
                            )
                        if raw_action is None:
                            replay_ok = False
                            print(f"    找不到回放目标: {step.target_description}")
                            break
                        action = bind_location(action, raw_action, current_capture, "replay_relocation")
                        if action is None:
                            reason = "invalid_action"
                            replay_ok = False
                            break
                        relocated_indices.append(index)
                    reason = "invalid_action"
                    if not self.verifier.has_executable_action(action, current_capture.screen_size):
                        replay_ok = False
                        print(f"    回放动作坐标无效: {step.target_description}")
                        break
                    reason = "execution_failed"
                    if not self.controller.execute_action(action, current_capture.screen_size):
                        replay_ok = False
                        break
                    recorded = ActionCodec.to_sequence_step(action, current_capture.screen_size)
                    relocated_steps.append(replace(
                        step, bounds=recorded.bounds, swipe_start=recorded.swipe_start,
                        swipe_end=recorded.swipe_end, screen_size=recorded.screen_size,
                        source_layout_id=recorded.source_layout_id,
                        grounding_method=recorded.grounding_method,
                    ))
                    reason = "observation_failed"
                    current_capture = self.capture_screen()
                    if not self.verifier.is_target_activity(self.controller.get_current_activity()):
                        reason = "outside_app_during_replay"
                        replay_ok = False
                        break
                    reason = "destination_mismatch" if index == len(target_page.action_sequence) - 1 else "target_state_mismatch"
                    expected = step.target_guard or ({"page_id": target_page.page_id}
                        if index == len(target_page.action_sequence) - 1 else {})
                    matched, vlm_reason = self.verifier.verify_replay_state(
                        current_capture, expected, action=step,
                        next_action=target_page.action_sequence[index + 1]
                        if index + 1 < len(target_page.action_sequence) else None)
                    if not matched:
                        replay_ok = False
                        break

                if replay_ok:
                    failed_step = None
                if replay_ok and not target_page.action_sequence:
                    reason = "destination_mismatch"
                    replay_ok, vlm_reason = self.verifier.verify_replay_state(
                        current_capture, {"page_id": target_page.page_id})
                if replay_ok:
                    arrived = True
                    reason = "destination_verified"
                    if relocated_indices:
                        # Only promote coordinates after the whole route is verified.
                        # Replacing steps also leaves shared prefixes and past visual states intact.
                        target_page.action_sequence = relocated_steps
                    return current_capture
                print(f"    回放后未到达 {target_page.page_id}")
            except Exception as exc:
                reason = f"{reason}: {exc}"
                print(f"    回放失败: {exc}")
            finally:
                if self.state:
                    last_screenshot = self.state.save_page_screenshot(
                        f"replay_{len(self.state.graph.observations)}", current_capture,
                    ) if current_capture is not None else ""
                    self.state.append_observation({
                        "kind": "replay", "target_page": target_page.page_id,
                        "target_description": target_page.page_description or target_page.page_type,
                        "attempt": attempt, "arrived": arrived, "failed_step": failed_step,
                        "reason": reason, "mode": mode, "verification_method": "vlm",
                        "relocated_step_indices": relocated_indices,
                        "last_observed_screenshot": last_screenshot,
                        "last_observed_fingerprint": current_capture.screenshot_hash if current_capture else "",
                        "reference_screenshot": target_page.screenshot_path,
                        "path": [{"action_type": step.action_type, "target": step.target_description,
                                  "input_text": step.input_text} for step in target_page.action_sequence],
                    })
            if reason in {"outside_app_after_restart", "outside_app_during_replay"}:
                break
            if mode == "coordinates":
                print("    回放失败，重启后按当前截图重试回放")
        return None

    def restore_to_work_item(self, work: StateWorkItem):
        """Restart, replay with layout-bound coordinates, and let the VLM verify each arrival."""
        registry = self.state.work_registry
        expected = registry.canonical_guard(work.restore_guard)
        self.last_restore_position = None
        route_for_log = work.entry_path

        def log(event, **details):
            self.state.append_observation({"kind": "state_restore", "work_id": work.work_id,
                "page_id": work.page_id, "target_page": work.page_id,
                "target_state_key": work.state_key, "target_guard": expected,
                "path": [asdict(step) for step in route_for_log], "event": event, **details})

        routes = [work.entry_path, *work.entry_routes]
        safe_routes = [route for route in routes if len(route) <= self.config.max_replay_steps
                       and replayable_route(route)]
        if not safe_routes:
            log("blocked", arrived=False, reason="non_replayable_path")
            return None
        for route_index, route in enumerate(safe_routes):
            route_for_log = route
            baseline = route[0].source_guard if route else expected
            if not baseline:
                continue
            for attempt in range(max(1, self.config.replay_attempts)):
                reason = "restart_failed"
                path = []
                failed_step = None
                verdict_reason = ""
                try:
                    self.controller.restart_app()
                    capture = self.capture_screen()
                    self.last_restore_position = capture, {}, []
                    if not self.verifier.is_target_activity(self.controller.get_current_activity()):
                        log("app_return_failed", arrived=False, reason="outside_app_after_restart",
                            current_activity=self.controller.get_current_activity())
                        return None
                    reason = "baseline_mismatch"
                    matched, verdict_reason = self.verifier.verify_replay_state(
                        capture, baseline, next_action=route[0] if route else None)
                    if not matched:
                        raise ValueError(reason)
                    current_guard = registry.canonical_guard(baseline)
                    self.last_restore_position = capture, current_guard, []
                    if not route:
                        log("replay", route=route_index, attempt=attempt + 1, arrived=True,
                            verification_method="vlm", reason=verdict_reason)
                        return capture, path
                    for index, step in enumerate(route):
                        failed_step = index
                        if step.action_type == "back":
                            reason = "back_failed"
                            success = self.controller.go_back()
                            recorded = step
                        else:
                            action = ActionCodec.action_from_sequence_step(step)
                            if not matches_source(action, capture):
                                reason = "target_not_found"
                                raw = self.analyzer.locate_replay_action(capture.image_b64, capture.screen_size, step)
                                action = bind_location(action, raw, capture, "state_replay")
                            if action is None or not self.verifier.has_executable_action(action, capture.screen_size):
                                raise ValueError(reason)
                            reason = "execution_failed"
                            success = self.controller.execute_action(action, capture.screen_size)
                            recorded = replace(ActionCodec.to_sequence_step(action, capture.screen_size),
                                               source_guard=current_guard, target_guard=step.target_guard)
                        log("replay_step", route=route_index, attempt=attempt + 1, step=index,
                            execution_success=bool(success), action_type=step.action_type, target=step.target_description)
                        if not success:
                            raise ValueError(reason)
                        reason = "observation_failed"
                        capture = self.capture_screen()
                        path.append(recorded)
                        self.last_restore_position = capture, {}, list(path)
                        if not self.verifier.is_target_activity(self.controller.get_current_activity()):
                            path[-1] = replace(recorded, target_guard={}, replay_safe=False)
                            self.last_restore_position = capture, {}, list(path)
                            log("app_return_failed", arrived=False, reason="outside_app_during_replay",
                                current_activity=self.controller.get_current_activity(), failed_step=index)
                            return None
                        reason = "target_state_mismatch"
                        target = expected if index == len(route) - 1 else registry.canonical_guard(step.target_guard)
                        matched, verdict_reason = self.verifier.verify_replay_state(
                            capture, target, action=recorded,
                            next_action=route[index + 1] if index + 1 < len(route) else None)
                        if not matched:
                            # A mismatch rejects this target, not the safety of the
                            # read-only operation. Observe the actual branch next.
                            path[-1] = replace(recorded, target_guard={}, arrival_verified=False)
                            self.last_restore_position = capture, {}, list(path)
                            raise ValueError(f"{reason} at step {index}")
                        current_guard = target
                        self.last_restore_position = capture, current_guard, list(path)
                    log("replay", route=route_index, attempt=attempt + 1, arrived=True,
                        verification_method="vlm", reason=verdict_reason)
                    return capture, path
                except Exception as exc:
                    if path and reason == "target_state_mismatch":
                        path[-1] = replace(path[-1], target_guard={}, arrival_verified=False)
                        if self.last_restore_position:
                            self.last_restore_position = self.last_restore_position[0], {}, list(path)
                    screenshot = self.state.save_page_screenshot(
                        f"restore_{len(self.state.graph.observations)}", self.last_restore_position[0]
                    ) if self.last_restore_position else ""
                    log("replay", arrived=False, route=route_index, attempt=attempt + 1,
                        failed_step=failed_step, reason=f"{reason}: {exc}", vlm_reason=verdict_reason,
                        verification_method="vlm", last_observed_screenshot=screenshot)
        return None
