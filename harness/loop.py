"""Execution loop for breadth-first Android app exploration."""

from __future__ import annotations

import time
from collections import OrderedDict
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from .actions import ActionCodec
from .config import ExplorerConfig
from .context import ExplorationContextBuilder
from .coverage import CoverageCheckpoint
from .contracts import (
    ActionSequenceStep,
    ExplorationAction,
    InputFieldObservation,
    PageState,
    PageVisualState,
    ScreenCapture,
    UIElement,
)
from .lifecycle import ExplorationLifecycle
from .memory import GraphMemory
from .knowledge import page_descriptor
from .planning import MemoryGuidedPlanner
from .grounding import bind_location, layout_id, matches_source, with_source
from .revisit import build_revisit_history
from .state import ExplorationState
from .tools import AndroidController, VLMAnalyzer
from .summary import model_identity, validate_summary_config
from .evidence import sanitize_inventory
from .capabilities import AppCapabilityCatalog
from .verify import ExistingPageMatch, ExplorationVerifier


class AndroidExplorer:
    """Harness execution core using a FIFO page queue and replay recovery."""

    def __init__(
        self,
        config: ExplorerConfig,
        controller: Optional[AndroidController] = None,
        analyzer: Optional[VLMAnalyzer] = None,
        state: Optional[ExplorationState] = None,
        context_builder: Optional[ExplorationContextBuilder] = None,
        summary_client: Any = None,
    ):
        self.config = config
        validate_summary_config(config)
        if summary_client is not None and not config.summary_model.strip():
            raise ValueError("An injected summary client requires --summary-model")
        # Read old output before creating run state; memory never imports its queue or progress.
        self.memory = GraphMemory(config)
        self.context_builder = context_builder or ExplorationContextBuilder()
        self.controller = controller or AndroidController(config)
        self.analyzer = analyzer or VLMAnalyzer(config, self.context_builder)
        if summary_client is not None and summary_client is self.analyzer:
            raise ValueError("Execution and summary clients must be separate instances")
        execution_model = getattr(self.analyzer, "model", config.model)
        if config.summary_model.strip() and model_identity(execution_model) == model_identity(config.summary_model):
            raise ValueError("Experience summary model must differ from the actual execution model")
        self.state = state or ExplorationState(config)
        self.memory_planner = MemoryGuidedPlanner(self.memory, self.state, self.analyzer)
        # One automatic summary pipeline; optional node/replica products are explicit consumers.
        self.capability_service = AppCapabilityCatalog(config, self.state, summary_client)
        self.state.capability_service = self.memory.capability_service = self.capability_service
        for historical_graph in self.memory.capability_graphs:
            self.capability_service.ingest_graph(historical_graph)
        self.memory.capability_graphs.clear()
        self.verifier = ExplorationVerifier(config, self.controller, self.analyzer, self.state)
        self.coverage_checkpoint = CoverageCheckpoint(config, self.state, self.analyzer, self.verifier, self.memory)

        self.lifecycle = ExplorationLifecycle(
            config,
            self.controller,
            self.analyzer,
            self.verifier,
            state=self.state, memory=self.memory,
        )
        ##这里真正开始实例化一个生命周期，可以开始探索的生命周期

        self.run_context = self.context_builder.run_context(config)
        self._analysis_cache = OrderedDict()
        if isinstance(self.controller, AndroidController):
            self.controller.input_locator = self._locate_focused_input
            self.controller.input_empty_checker = self._verify_input_empty

        # Compatibility aliases make the initial split low-risk for callers and tests.
        self.graph = self.state.graph
        self.visited_hashes = self.state.visited_hashes
        self.hash_to_page_id = self.state.hash_to_page_id
        self._pending_pages = self.state.pending_pages
        self._page_images = self.state.page_images

    @property
    def page_counter(self) -> int:
        return self.state.page_counter

    def _locate_focused_input(self, raw_capture, action):
        capture = ScreenCapture(*raw_capture)
        raw = self.analyzer.locate_replay_action(
            capture.image_b64, capture.screen_size, ActionCodec.to_sequence_step(action))
        grounded = (bind_location(action, raw, capture, "focused_input")
                    if isinstance(raw, dict) and raw.get("editable") is True else None)
        value = raw.get("value") if isinstance(raw, dict) else None
        value = value if isinstance(value, str) else None
        self.state.append_observation({"kind": "input_focus", "target": action.target_description,
                                       "located": grounded is not None, "layout_id": layout_id(capture),
                                       "phase": "before_input",
                                       "field_key": action.field_key, "observed_value": value,
                                       "value_observed": value is not None})
        return InputFieldObservation(grounded.bounds, value) if grounded else None

    def _verify_input_empty(self, raw_capture, action, bounds):
        capture = ScreenCapture(*raw_capture)
        result = self.analyzer.verify_input_empty(
            capture.image_b64, capture.screen_size, bounds, action.target_description)
        empty = (isinstance(result, dict) and result.get("editable") is True
                 and result.get("empty") is True)
        self.state.append_observation({"kind": "input_focus", "target": action.target_description,
                                       "phase": "clear_check", "field_key": action.field_key,
                                       "layout_id": layout_id(capture), "bounds": dict(bounds),
                                       "verified_empty": empty, "vlm_result": result})
        return empty

    @property
    def _transition_counter(self) -> int:
        return self.state.transition_counter

    def explore(self) -> None:
        """启动探索，分派状态/页面调度流程，并在结束时保存证据和功能目录。"""

        # 1. 初始化本轮计时，并说明是否启用了独立总结模型。
        # start_time 用于统计探索耗时，以及包含结束处理的总耗时。
        start_time = time.monotonic()
        if self.capability_service.client:
            print(f"[Summary] 执行模型：{self.config.model}；独立总结模型：{self.config.summary_model}")
        else:
            print("[Summary] 未配置 summary 模型，保留功能证据，不生成模型功能目录")
        initial_capture: Optional[ScreenCapture] = None
        try:
            # 2. 启动设备/App 生命周期，用首张截图建立探索根页面。
            # 根页面的到达路径为空；无法识别根页面时，抛出异常进入统一收尾。
            self.lifecycle.start()
            initial_capture = self._capture_screen()##这里截屏，作为初始节点
            root_page, _ = self._capture_or_get_page(initial_capture, action_sequence=[])
            if root_page is None:
                raise RuntimeError("无法建立探索根页面")
            self.graph.root_page_id = root_page.page_id

            # 3. 选择探索模式：默认交给 StateExplorer 按状态工作项调度。
            # 只有关闭 state_exploration 时，才继续执行下方旧版 FIFO 页面队列。
            # 此处 return 仍会执行本函数的 finally，保存结果并处理剩余总结。
            if self.config.state_exploration:
                from .state_loop import StateExplorer
                StateExplorer(self).run(root_page, initial_capture)
                ##这里要注意，默认是执行StateExplorer(self).run()的
                return

            # 4. 旧版页面调度：有待探索页面、未耗尽步数预算且未阻塞时继续。
            # 每轮先检查前台是否属于目标 App；无法返回目标 App 时停止调度。
            while (self._pending_pages and not self._step_budget_exhausted()
                   and self.graph.termination_reason != "blocked"):
                if not self._is_target_activity(self.controller.get_current_activity()):
                    if not self.lifecycle.return_to_target_app():
                        self.graph.termination_reason = "blocked"
                        break

                # 4.1 按先进先出顺序取出页面，并记录本轮实际探索顺序。
                page_id = self._pending_pages.popleft()
                page_state = self.graph.pages[page_id]
                self.graph.exploration_order.append(page_id)

                # 4.2 首次探索根页面直接复用启动截图；其他页面通过路径回放到达。
                # initial_capture 用过即清空，避免以后把启动时的旧截图当成当前界面。
                if initial_capture is not None and page_id == root_page.page_id:
                    current_capture = initial_capture
                    initial_capture = None
                else:
                    current_capture = self._replay_to_page(page_state)

                # 4.3 到达失败时区分两种情况：
                # 已离开目标 App：把当前页面放回队首，标记本轮阻塞并停止；
                # 仍在目标 App：只标记该页面恢复失败，保存后继续处理其他页面。
                if current_capture is None:
                    if not self._is_target_activity(self.controller.get_current_activity()):
                        self.state.enqueue(page_id, first=True)
                        self.graph.termination_reason = "blocked"
                        break
                    page_state.exploration_status = "recovery_failed"
                    self._save_graph(announce=False)
                    continue

                # 4.4 已到达目标页面，执行该页本轮动作探索。
                # 具体的动作选择、执行、结果记录和轮次保存封装在下层方法中。
                self._explore_page_state(page_state, current_capture)

            # 5. 页面循环退出后补齐终止原因，保留此前已经设置的阻塞等状态。
            # 优先判预算耗尽；否则有未完成页面则为阻塞，全部完成则为队列耗尽。
            # 默认状态模式的终止原因由 StateExplorer 设置，不经过这一段。
            if self.graph.termination_reason == "running":
                self.graph.termination_reason = "budget_limited" if self._step_budget_exhausted() else (
                    "blocked" if any(p.exploration_status not in {"complete", "completed"} for p in self.graph.pages.values()) else "frontier_exhausted")

        # 6. 区分用户中断和运行异常，记录原因后继续向外抛出。
        # 两种情况都会进入 finally；这里不吞掉异常，也不把失败视作正常完成。
        except KeyboardInterrupt:
            self.graph.termination_reason = "interrupted"
            raise
        except Exception:
            self.graph.termination_reason = "failed"
            raise
        finally:
            # 7. 统一收尾：正常返回、中断或异常进入此处时，先记录探索耗时并保存当前图谱。
            # 这不是超时判断；结束阶段的总结请求耗时尚未计入 exploration_seconds。
            self.graph.timings["exploration_seconds"] = time.monotonic() - start_time
            self._save_graph(announce=False)
            print(f"[Explore] 探索停止：{self.graph.termination_reason}；正在保存和处理剩余总结证据")
            try:
                # 8. 停止生命周期；即使 stop() 抛出异常，也进入下层 finally 尝试总结。
                self.lifecycle.stop()
            finally:
                final_started = time.monotonic()
                try:
                    # 8.1 在本轮剩余总结预算内处理待总结证据，更新 App 功能目录。
                    # final=True 表示进入结束处理，并不绕过累计预算或保证所有批次完成。
                    self.capability_service.update(reason="exploration_end", final=True)
                finally:
                    # 8.2 即使总结失败，也记录结束处理和总耗时，再保存最终图谱。
                    # exploration_time / total_seconds 包含探索、生命周期停止及结束总结。
                    self.graph.timings["finalization_seconds"] = time.monotonic() - final_started
                    self.graph.exploration_time = time.monotonic() - start_time
                    self.graph.timings["total_seconds"] = self.graph.exploration_time
                    self._save_graph()

    def _explore_page_state(
        self,
        page_state: PageState,
        current_capture: ScreenCapture,
    ) -> None:
        try:
            self._explore_page_actions(page_state, current_capture)
        finally:
            # A round can end through exhaustion, limits, recovery failure or a
            # scheduler switch. All of its recorded attempts inform this node.
            self.capability_service.update(reason="page_round")
            self._save_graph(announce=False)

    def _explore_page_actions(
        self, page_state: PageState, current_capture: ScreenCapture,
    ) -> None:
        step_context = self.context_builder.step_context(page_state)
        depth = step_context.depth
        actions = self._select_actions(page_state)
        print(f"\n[{page_state.page_id}] 深度={depth}")
        print(f"  发现 {len(page_state.elements)} 个可交互元素，规划 {len(actions)} 个动作")

        if depth >= self.config.max_depth:
            page_state.exploration_status = "depth_limit"
            return

        page_state.exploration_status = "exploring"
        while page_state.next_action_index < len(actions):
            if self._step_budget_exhausted():
                page_state.exploration_status = "step_limit"
                return
            selection = self.memory_planner.select(page_state, current_capture, actions)
            if selection is None:
                page_state.exploration_status = "planning_blocked"
                self._save_graph(announce=False)
                return
            selected_index, action = selection
            index = page_state.next_action_index
            actions[selected_index], actions[index] = actions[index], action
            page_state.planned_actions[selected_index], page_state.planned_actions[index] = (
                page_state.planned_actions[index], action,
            )
            if not matches_source(action, current_capture):
                # Defense in depth: page identity alone cannot authorize old coordinates.
                page_state.exploration_status = "planning_blocked"
                self._save_graph(announce=False)
                return
            if not self._has_executable_action(action, current_capture.screen_size):
                print(f"  跳过无有效坐标的动作: {action.target_description}")
                self._record_transition(
                    from_page=page_state.page_id, to_page="", action=action, success=False,
                    state_changed=False, before_screenshot="", after_screenshot="",
                    recovery="not_needed", error_stage="invalid_action",
                    destination_verified=False,
                )
                page_state.next_action_index += 1
                self._save_graph(announce=False)
                continue

            label = action.element_text or action.target_description or action.element_id or "unknown"
            print(f"  {action.action_type}: {label}")
            before_capture = current_capture
            observed_at = time.time()
            source_state = self._observe_page_state(page_state, before_capture, page_state.action_sequence)
            source_snapshot = page_descriptor(page_state)
            source_snapshot["state"] = dict(source_state.semantic_state)
            executed = False
            observation_valid = False
            state_changed = visual_changed = False
            after_capture = before_capture
            before_path = after_path = ""
            recovery = "not_attempted"
            error_stage = error = ""
            next_page: Optional[PageState] = None
            target_state: Optional[PageVisualState] = None
            target_snapshot = None
            outside_app = False
            external_activity = ""
            try:
                error_stage = "execution"
                executed = bool(self.controller.execute_action(action, before_capture.screen_size))
                error_stage = "observation"
                try:
                    after_capture = self._capture_screen()
                    observation_valid = True
                except Exception as exc:
                    error = str(exc)
                    print(f"  动作后截图失败: {exc}")
                before_path, after_path = self._save_transition_screens(before_capture, after_capture)
                if not observation_valid:
                    # The fallback image is not an observation of the result.
                    after_path = ""
                else:
                    visual_changed = not self.verifier.captures_are_visually_same(after_capture, before_capture)
                if executed and observation_valid:
                    error_stage = "identification"
                    activity = self.controller.get_current_activity()
                    outside_app = not self._is_target_activity(activity)
                    external_activity = activity if outside_app else ""
                    next_page, _ = (None, False) if outside_app else self._capture_or_get_page(
                        after_capture, page_state.action_sequence + [self._to_sequence_step(action, before_capture.screen_size)],
                        source_action=action,
                    )
                    if next_page is not None:
                        target_state = self._observe_page_state(
                            next_page, after_capture,
                            page_state.action_sequence + [self._to_sequence_step(action, before_capture.screen_size)],
                        )
                        target_snapshot = page_descriptor(next_page)
                        target_snapshot["state"] = dict(target_state.semantic_state)
                state_changed = source_state.state_id != target_state.state_id if target_state else visual_changed
                error_stage = "recovery"
                if observation_valid:
                    current_capture, recovery = self._restore_after_action(
                        page_state, before_capture, after_capture, next_page,
                    )
                else:
                    current_capture = self._replay_to_page(page_state)
                    recovery = "replay" if current_capture is not None else "failed"
                error_stage = "observation" if not observation_valid else ""
            except Exception as exc:
                error = str(exc)
                if error_stage == "recovery":
                    recovery = "failed"
                raise
            finally:
                # Even failed identification/recovery must leave an auditable action attempt.
                if not before_path and self.config.save_screenshots:
                    before_path, saved_after = self._save_transition_screens(before_capture, after_capture)
                    after_path = saved_after if observation_valid else ""
                self._record_transition(
                    from_page=page_state.page_id,
                    to_page=next_page.page_id if next_page is not None else "",
                    action=action, success=executed, state_changed=state_changed,
                    before_screenshot=before_path, after_screenshot=after_path, recovery=recovery,
                    observation_valid=observation_valid, destination_verified=next_page is not None,
                    visual_changed=visual_changed, error_stage=error_stage, error=error,
                    outside_app=outside_app, source_snapshot=source_snapshot,
                    external_activity=external_activity,
                    target_snapshot=target_snapshot,
                    before_fingerprint=before_capture.screenshot_hash,
                    after_fingerprint=after_capture.screenshot_hash if observation_valid else "",
                    observed_at=observed_at,
                    from_state_id=source_state.state_id,
                    to_state_id=target_state.state_id if target_state else "",
                )
                self.graph.completed_steps += 1
            page_state.next_action_index += 1
            if (current_capture is not None and callable(getattr(self.analyzer, "choose_next_action", None))
                    and self.state.schedule_verification(page_state, action)):
                actions = self._select_actions(page_state)
            if current_capture is None:
                page_state.exploration_status = "pending" if recovery == "app_return_failed" else "recovery_failed"
            self._save_graph(announce=False)

            if self._apply_coverage_checkpoint_after_step(page_state.page_id):
                return

            if current_capture is None:
                page_state.exploration_status = "pending" if recovery == "app_return_failed" else "recovery_failed"
                if recovery == "app_return_failed":
                    self.graph.termination_reason = "blocked"
                    self.state.enqueue(page_state.page_id, first=True)
                return
        page_state.exploration_status = "complete"

    def _step_budget_exhausted(self) -> bool:
        return self.config.max_steps > 0 and self.graph.completed_steps >= self.config.max_steps

    def _apply_coverage_checkpoint_after_step(self, current_page_id: str) -> bool:
        result = self.coverage_checkpoint.run(self.graph.completed_steps, current_page_id)
        if result is None or (result.target_page_id is None and result.merged_count == 0):
            return False
        current = self.graph.pages[result.current_page_id]
        if self.state.can_explore(current):
            current.exploration_status = "pending"
            self.state.enqueue(current.page_id, first=True)
        elif current.exploration_status not in {"recovery_failed", "planning_blocked"}:
            current.exploration_status = "complete"
        if result.target_page_id is not None:
            self.state.enqueue(result.target_page_id, first=True)
            print(f"[Coverage] 下一个探索目标：{result.target_page_id}")
        self._save_graph(announce=False)
        # Return to the scheduler so it replays and verifies the canonical target.
        # Never continue with the pre-merge PageState/action list/capture.
        return True

    def _capture_screen(self) -> ScreenCapture:
        return self.lifecycle.capture_screen()

    def _observe_page_state(
        self, page: PageState, capture: ScreenCapture, action_sequence: List[ActionSequenceStep],
        *, activity: str | None = None,
    ) -> PageVisualState:
        semantic = self.verifier.semantic.describe(capture)
        return self.state.observe_page_state(
            page, capture, semantic_state=semantic.state if not semantic.error else {},
            page_description=semantic.description or page.page_description,
            activity=activity if activity is not None else self.controller.get_current_activity(),
            action_sequence=action_sequence,
        )

    def _reuse_page(
        self, page: PageState, capture: ScreenCapture,
        action_sequence: List[ActionSequenceStep], activity: str,
    ) -> Tuple[PageState, bool]:
        self.hash_to_page_id[capture.screenshot_hash] = page.page_id
        self.state.note_visit(page.page_id)
        self._observe_page_state(page, capture, action_sequence, activity=activity)
        return page, False

    def _capture_or_get_page(
        self,
        capture: ScreenCapture,
        action_sequence: List[ActionSequenceStep],
        source_action: Optional[ExplorationAction] = None,
    ) -> Tuple[Optional[PageState], bool]:
        activity = self.controller.get_current_activity()
        if not self._is_target_activity(activity):
            print(f"  检测到目标 App 之外的 Activity，跳过入队并触发恢复: {activity}")
            return None, False

        current_package = activity.split("/", 1)[0].strip() or self.config.app_package
        existing_match = self._find_existing_page(capture, app_package=current_package)
        if existing_match is not None:
            existing_page = existing_match.page
            if (
                self.state.work_registry is not None
                or
                existing_match.source in {"fingerprint_exact", "visual_similarity"}
                or source_action is None
                or source_action.action_type != "swipe"
            ):
                return self._reuse_page(existing_page, capture, action_sequence, activity)

            analysis = self._analyze_screen(capture, action_sequence, activity)
            elements = self._elements_from_analysis(analysis)
            if not elements or self._same_actionable_surface(elements, existing_page.elements):
                return self._reuse_page(existing_page, capture, action_sequence, activity)
            surface_match = self._find_existing_page(
                capture,
                candidate_elements=elements,
                app_package=current_package,
            )
            if surface_match is not None:
                matched_page = surface_match.page
                return self._reuse_page(matched_page, capture, action_sequence, activity)
            print(f"  {existing_page.page_id} 操作后出现新控件，登记为新的可见状态")
        else:
            analysis = None
            elements = None

        if len(self.graph.pages) >= self.config.max_pages:
            print("  已达到最大页面数，候选页面不再入队")
            self.graph.termination_reason = "budget_limited"
            return None, False

        page_id = self.state.allocate_page_id()
        screenshot_path = self.state.save_page_screenshot(page_id, capture)
        if analysis is None:
            print(f"  VLM 分析新页面 {page_id}...")
            analysis = self._analyze_screen(capture, action_sequence, activity)
        if elements is None:
            elements = self._elements_from_analysis(analysis)
        self.state.save_analysis(page_id, analysis)

        raw_recommended_actions = analysis.get("recommended_actions", [])
        if not isinstance(raw_recommended_actions, list):
            raw_recommended_actions = []
        recommended_actions = [
            str(element_id)
            for element_id in raw_recommended_actions
            if element_id
        ]
        planned_actions = self._planned_actions_from_analysis(
            analysis,
            elements,
            recommended_actions,
        )
        planned_actions = [with_source(action, capture, "analysis") for action in planned_actions]
        semantic = self.verifier.semantic.describe(capture)
        page_state = PageState(
            page_id=page_id,
            screenshot_path=screenshot_path,
            screenshot_hash=capture.screenshot_hash,
            activity=activity,
            page_type=str(analysis.get("page_type") or "unknown"),
            elements=elements,
            recommended_actions=recommended_actions,
            planned_actions=planned_actions,
            action_sequence=list(action_sequence),
            timestamp=time.time(),
            page_description=semantic.description,
            semantic_state=semantic.state,
            description_embedding=semantic.embedding,
            embedding_model=semantic.model,
            semantic_error=semantic.error,
            app_package=current_package,
            content_hints=[value[:160] for value in analysis.get("content_hints", [])[:20]
                           if isinstance(value, str)] if isinstance(analysis.get("content_hints"), list) else [],
            content_inventory=analysis["content_inventory"],
        )
        self.state.register_page(page_state, capture)
        return page_state, True

    def _analyze_screen(self, capture: ScreenCapture, action_sequence: List[ActionSequenceStep],
                        activity: str, *, force: bool = False, page: Optional[PageState] = None) -> Dict[str, Any]:
        analyze_history = getattr(self.analyzer, "analyze_screen_with_history", None)
        history = (build_revisit_history(self.state, page, capture)
                   if page is not None and callable(analyze_history) else None)
        key = (layout_id(capture), activity)
        if history is not None:
            # Identical pixels can have new exploration results. Do not reuse a stale plan.
            key += (history.revision,)
        if not force and key in self._analysis_cache:
            self._analysis_cache.move_to_end(key)
            return deepcopy(self._analysis_cache[key])
        analyze = getattr(self.analyzer, "analyze_screen_with_memory", None)
        context = None
        if self.config.graph_memory and (callable(analyze) or history is not None):
            semantic = self.verifier.semantic.describe(capture)
            context = self.memory.context(
                self.graph, query=semantic.description, activity=activity, action_sequence=action_sequence,
                purpose="analyze",
            )
        if history is not None:
            analysis = analyze_history(capture.image_b64, capture.screen_size,
                                       reference=history.reference, history=history.evidence, memory=context)
        elif context is not None:
            analysis = analyze(capture.image_b64, capture.screen_size, context)
        else:
            analysis = self.analyzer.analyze_screen(capture.image_b64, capture.screen_size)
        if context is not None or history is not None:
            self.state.record_memory_planning({
                "phase": "analyze_screen", "step": self.graph.completed_steps,
                "memory": context, "planned_actions": analysis.get("actions", []),
                "revisit_history": history.evidence if history is not None else None,
            })
        # Keep even observations that match an existing topology node. Sanitize
        # before saving the analysis or passing the new inventory to any model.
        analysis = dict(analysis)
        analysis["content_inventory"] = sanitize_inventory(analysis.get("content_inventory"))
        if analysis.get("analysis_status") != "failed":
            self._analysis_cache[key] = deepcopy(analysis)
        if len(self._analysis_cache) > 128:
            self._analysis_cache.popitem(last=False)
        return analysis

    @staticmethod
    def _elements_from_analysis(analysis: Dict[str, Any]) -> List[UIElement]:
        raw_elements = analysis.get("elements", [])
        if not isinstance(raw_elements, list):
            return []
        return [
            element
            for raw in raw_elements
            if isinstance(raw, dict)
            for element in [ActionCodec.element_from_model(raw)]
            if element is not None
        ]

    def _is_target_activity(self, activity: str) -> bool:
        return self.verifier.is_target_activity(activity)

    @staticmethod
    def _same_actionable_surface(
        candidate_elements: List[UIElement],
        existing_elements: List[UIElement],
    ) -> bool:
        return ExplorationVerifier.same_actionable_surface(candidate_elements, existing_elements)

    def _find_existing_page(
        self,
        capture: ScreenCapture,
        candidate_elements: Optional[List[UIElement]] = None,
        app_package: Optional[str] = None,
    ) -> Optional[ExistingPageMatch]:
        return self.verifier.find_existing_page(capture, candidate_elements, app_package)

    def _screen_matches_page(self, capture: ScreenCapture, page_state: PageState) -> bool:
        return self.verifier.screen_matches_page(capture, page_state)

    def _restore_after_action(
        self,
        parent_page: PageState,
        before_capture: ScreenCapture,
        after_capture: ScreenCapture,
        next_page: Optional[PageState],
    ) -> Tuple[Optional[ScreenCapture], str]:
        return self.lifecycle.restore_after_action(
            parent_page,
            before_capture,
            after_capture,
            next_page,
        )

    def _replay_to_page(self, target_page: PageState) -> Optional[ScreenCapture]:
        capture = self.lifecycle.replay_to_page(target_page)
        if capture is not None:
            self.state.note_visit(target_page.page_id)
        return capture

    def _select_actions(self, page_state: PageState) -> List[ExplorationAction]:
        actions = list(page_state.planned_actions)
        if self.config.max_actions_per_page > 0:
            return actions[: self.config.max_actions_per_page]
        return actions

    def _record_transition(self, **kwargs: Any) -> None:
        self.state.record_transition(**kwargs)

    def _save_transition_screens(
        self,
        before_capture: ScreenCapture,
        after_capture: ScreenCapture,
    ) -> Tuple[str, str]:
        return self.state.save_transition_screens(before_capture, after_capture)

    def _save_graph(self, *, announce: bool = True) -> None:
        self.state.save_graph(announce=announce)

    @staticmethod
    def _planned_actions_from_analysis(
        analysis: Dict[str, Any],
        elements: List[UIElement],
        recommended_ids: List[str],
    ) -> List[ExplorationAction]:
        return ActionCodec.planned_actions_from_analysis(
            analysis,
            elements,
            recommended_ids,
        )

    @staticmethod
    def _action_from_model(
        raw: Any,
        *,
        element: Optional[UIElement] = None,
        step: Optional[ActionSequenceStep] = None,
    ) -> ExplorationAction:
        return ActionCodec.action_from_model(raw, element=element, step=step)

    @staticmethod
    def _normalize_point(raw_point: Any) -> Optional[Dict[str, int]]:
        return ActionCodec.normalize_point(raw_point)

    @staticmethod
    def _has_executable_action(
        action: ExplorationAction,
        screen_size: Tuple[int, int],
    ) -> bool:
        return ExplorationVerifier.has_executable_action(action, screen_size)

    @staticmethod
    def _to_sequence_step(
        action: ExplorationAction, screen_size: Optional[Tuple[int, int]] = None,
    ) -> ActionSequenceStep:
        return ActionCodec.to_sequence_step(action, screen_size)

    @staticmethod
    def _element_from_model(raw: Dict[str, Any]) -> Optional[UIElement]:
        return ActionCodec.element_from_model(raw)

    @staticmethod
    def _normalize_bounds(raw_bounds: Any) -> Dict[str, int]:
        return ActionCodec.normalize_bounds(raw_bounds)

    @staticmethod
    def _tap_point_for_bounds(bounds: Dict[str, int]) -> Tuple[int, int]:
        return ActionCodec.tap_point_for_bounds(bounds)

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        return ActionCodec.to_int(value, default)
