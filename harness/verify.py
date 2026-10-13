"""Precondition and state-identity verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .actions import ActionCodec
from .config import ExplorerConfig
from .contracts import ActionSequenceStep, ExplorationAction, PageState, ScreenCapture, UIElement
from .state import ExplorationState
from .semantic import SemanticRetriever
from .visual import VisualComparison, compare_screenshot_b64
from .grounding import layout_id


class VLMComparisonFailed(RuntimeError):
    """Raised when bounded VLM comparison retries are exhausted."""


class SemanticDedupFailed(RuntimeError):
    """Raised when a page description or its embedding cannot be produced."""


@dataclass(frozen=True)
class ExistingPageMatch:
    page: PageState
    source: str
    reason: str


class ExplorationVerifier:
    """Deterministic and semantic checks kept outside the execution loop."""

    def __init__(
        self,
        config: ExplorerConfig,
        controller: object,
        analyzer: object,
        state: ExplorationState,
    ):
        self.config = config
        self.controller = controller
        self.analyzer = analyzer
        self.state = state
        self.semantic = SemanticRetriever(config, analyzer)

    def is_target_activity(self, activity: str) -> bool:
        if not self.config.app_package:
            return True
        if not activity:
            return False
        foreground_package = activity.split("/", 1)[0].strip()
        return foreground_package == self.config.app_package

    @staticmethod
    def _normalized_text(value: str) -> str:
        return " ".join(value.casefold().split())

    @staticmethod
    def _bounds_match(candidate: UIElement, existing: UIElement) -> bool:
        first = candidate.bounds
        second = existing.bounds
        if min(
            first.get("width", 0),
            first.get("height", 0),
            second.get("width", 0),
            second.get("height", 0),
        ) <= 0:
            return True
        first_x = first["x"] + first["width"] / 2
        first_y = first["y"] + first["height"] / 2
        second_x = second["x"] + second["width"] / 2
        second_y = second["y"] + second["height"] / 2
        x_tolerance = max(40.0, max(first["width"], second["width"]) * 0.35)
        y_tolerance = max(40.0, max(first["height"], second["height"]) * 0.35)
        return abs(first_x - second_x) <= x_tolerance and abs(first_y - second_y) <= y_tolerance

    @classmethod
    def actionable_surface_comparison(
        cls,
        candidate_elements: List[UIElement],
        existing_elements: List[UIElement],
    ) -> Tuple[bool, Dict[str, Any]]:
        """Compare stable control identity without using free-form VLM descriptions."""

        used_existing: set[int] = set()
        matched = 0
        unmatched_candidate: List[str] = []

        def stable_label(element: UIElement) -> str:
            return cls._normalized_text(element.resource_id or element.text)

        for candidate in candidate_elements:
            candidate_type = candidate.type.casefold()
            candidate_label = stable_label(candidate)
            best_index: Optional[int] = None
            best_score = -1
            for index, existing in enumerate(existing_elements):
                if index in used_existing or existing.type.casefold() != candidate_type:
                    continue
                existing_label = stable_label(existing)
                if candidate.resource_id and existing.resource_id:
                    if cls._normalized_text(candidate.resource_id) != cls._normalized_text(
                        existing.resource_id
                    ):
                        continue
                    score = 4
                elif candidate_label and existing_label:
                    if candidate_label != existing_label:
                        continue
                    score = 3
                else:
                    # A model may emit "+" for an icon in one pass and no text in
                    # another. Its type and stable position are safer than prose.
                    score = 2
                if not cls._bounds_match(candidate, existing):
                    continue
                if score > best_score:
                    best_index = index
                    best_score = score
            if best_index is None:
                unmatched_candidate.append(
                    f"{candidate.type}:{candidate.resource_id or candidate.text or candidate.id}"
                )
                continue
            used_existing.add(best_index)
            matched += 1

        candidate_count = len(candidate_elements)
        existing_count = len(existing_elements)
        maximum_count = max(candidate_count, existing_count)
        coverage = matched / maximum_count if maximum_count else 1.0
        count_delta = abs(candidate_count - existing_count)
        required_coverage = 1.0 if maximum_count <= 4 else 0.9
        same_surface = count_delta <= 1 and coverage >= required_coverage
        details: Dict[str, Any] = {
            "same_surface": same_surface,
            "candidate_count": candidate_count,
            "existing_count": existing_count,
            "matched_count": matched,
            "coverage": round(coverage, 4),
            "required_coverage": required_coverage,
            "unmatched_candidate": unmatched_candidate,
            "reason": (
                f"stable actionable controls matched {matched}/{maximum_count}; "
                f"count_delta={count_delta}, required_coverage={required_coverage:.2f}"
            ),
        }
        return same_surface, details

    @classmethod
    def same_actionable_surface(
        cls,
        candidate_elements: List[UIElement],
        existing_elements: List[UIElement],
    ) -> bool:
        same_surface, _ = cls.actionable_surface_comparison(
            candidate_elements,
            existing_elements,
        )
        return same_surface

    def fast_visual_compare(
        self,
        candidate: ScreenCapture,
        existing_b64: str,
    ) -> VisualComparison:
        return compare_screenshot_b64(
            candidate.image_b64,
            existing_b64,
            crop_top_ratio=self.config.fingerprint_crop_top_ratio,
            crop_bottom_ratio=self.config.fingerprint_crop_bottom_ratio,
            hash_distance_threshold=self.config.visual_hash_distance_threshold,
            mean_difference_threshold=self.config.visual_mean_difference_threshold,
        )

    def captures_are_visually_same(
        self,
        first: ScreenCapture,
        second: ScreenCapture,
    ) -> bool:
        if first.screenshot_hash == second.screenshot_hash:
            comparison = self.fast_visual_compare(first, second.image_b64)
            return comparison.same_screen if comparison.available else True
        comparison = self.fast_visual_compare(first, second.image_b64)
        return comparison.available and comparison.same_screen

    def _vlm_result_metadata(
        self,
        same_page: bool,
        reason: str,
    ) -> Dict[str, Any]:
        metadata = getattr(self.analyzer, "last_comparison", None)
        if isinstance(metadata, dict) and metadata.get("status") in {"success", "failed"}:
            return dict(metadata)
        return {
            "status": "success",
            "same_page": same_page,
            "reason": reason,
            "attempt_count": 1,
            "max_attempts": 1,
            "attempts": [
                {
                    "attempt": 1,
                    "status": "success",
                    "same_page": same_page,
                    "reason": reason,
                }
            ],
        }

    def _compare_with_vlm(
        self,
        capture: ScreenCapture,
        page_state: PageState,
        existing_b64: str,
        existing_size: Tuple[int, int],
        *,
        context: str,
        fast_visual: VisualComparison,
        candidate_elements: Optional[List[UIElement]] = None,
        semantic_retrieval: Optional[Dict[str, Any]] = None,
    ) -> bool:
        contextual_compare = getattr(self.analyzer, "compare_screens_with_description", None)
        if callable(contextual_compare):
            same_page, reason = contextual_compare(
                capture.image_b64,
                capture.screen_size,
                existing_b64,
                existing_size,
                existing_description=page_state.page_description,
            )
        else:
            same_page, reason = self.analyzer.compare_screens(
                capture.image_b64,
                capture.screen_size,
                existing_b64,
                existing_size,
            )
        vlm_result = self._vlm_result_metadata(same_page, reason)
        if vlm_result["status"] == "failed":
            failure_reason = str(vlm_result.get("error") or vlm_result.get("reason") or reason)
            self.state.record_vlm_comparison(
                context=context,
                candidate_fingerprint=capture.screenshot_hash,
                existing_page=page_state,
                fast_visual=fast_visual.to_dict(),
                vlm_result=vlm_result,
                decision="failed",
                rejection_reason=failure_reason,
                semantic_retrieval=semantic_retrieval,
            )
            raise VLMComparisonFailed(
                f"候选页面与 {page_state.page_id} 的 VLM 判重失败: {failure_reason}"
            )

        actionable_surface: Optional[Dict[str, Any]] = None
        decision = "rejected"
        rejection_reason = ""
        accepted = bool(same_page)
        if not same_page:
            status = ("vlm_insufficient_evidence" if vlm_result.get("identity_contract") == "template_state_v1"
                      and vlm_result.get("same_template") is None else "vlm_different_page")
            rejection_reason = f"{status}: {reason or 'no reason returned'}"
        elif candidate_elements is not None and vlm_result.get("identity_contract") != "template_state_v1":
            accepted, actionable_surface = self.actionable_surface_comparison(
                candidate_elements,
                page_state.elements,
            )
            if not accepted:
                rejection_reason = (
                    "actionable_surface_mismatch: "
                    + str(actionable_surface.get("reason") or "stable controls differ")
                )
        if accepted:
            decision = "accepted"
            if self.semantic.enabled and vlm_result.get("identity_contract") == "template_state_v1":
                semantic = self.semantic.describe(capture)
                semantic.state.update(vlm_result.get("new_state", {}))
                # The VLM's local-state disagreement is evidence for the state frontier;
                # it does not reject reuse of the page template.
                semantic.comparison_state = dict(vlm_result.get("new_state", {}))

        self.state.record_vlm_comparison(
            context=context,
            candidate_fingerprint=capture.screenshot_hash,
            existing_page=page_state,
            fast_visual=fast_visual.to_dict(),
            vlm_result=vlm_result,
            actionable_surface=actionable_surface,
            decision=decision,
            rejection_reason=rejection_reason,
            semantic_retrieval=semantic_retrieval,
        )
        if accepted and reason:
            print(f"    复用 {page_state.page_id}: {reason}")
        return accepted

    def verify_merge_pair(self, first: PageState, second: PageState) -> bool:
        """Always verify proposed audit merges with reference images and controls."""
        first_image, first_size = self.state.page_images[first.page_id]
        second_image, second_size = self.state.page_images[second.page_id]
        capture = ScreenCapture(second_image, second.screenshot_hash, second_size)
        return self._compare_with_vlm(
            capture, first, first_image, first_size,
            context="coverage_checkpoint_merge",
            fast_visual=self.fast_visual_compare(capture, first_image),
            candidate_elements=second.elements,
        )

    def find_existing_page(
        self,
        capture: ScreenCapture,
        candidate_elements: Optional[List[UIElement]] = None,
        app_package: Optional[str] = None,
    ) -> Optional[ExistingPageMatch]:
        if self.semantic.enabled:
            candidate = self.semantic.describe(capture)
            if candidate.error:
                raise SemanticDedupFailed(
                    f"页面语义描述或 Embedding 计算失败: {candidate.error}"
                )
            pages = [self.state.graph.pages[page_id] for page_id in self.state.graph.page_queue]
            current_package = app_package or self.config.app_package
            best = self.semantic.best_match(candidate, pages, current_package)
            if best is None:
                print(f"  同 package ({current_package}) 内没有可比较的语义节点")
                return None
            print(
                f"  最佳语义候选: {best.page.page_id}, similarity={best.similarity:.4f}, "
                f"threshold={self.config.semantic_similarity_threshold:.4f}"
            )
            if best.similarity < self.config.semantic_similarity_threshold:
                # Text embeddings can drift even when only the system clock changed.
                # A visual match recalls a candidate; the VLM still decides identity.
                for page in pages:
                    if page.app_package != current_package or page.page_id not in self.state.page_images:
                        continue
                    existing_b64, existing_size = self.state.page_images[page.page_id]
                    visual = self.fast_visual_compare(capture, existing_b64)
                    if visual.available and visual.same_screen and self._compare_with_vlm(
                        capture, page, existing_b64, existing_size,
                        context="visual_candidate_recall", fast_visual=visual,
                        candidate_elements=candidate_elements,
                    ):
                        return ExistingPageMatch(page, "vlm", "VLM confirmed visually recalled candidate")
                return None
            page = best.page
            existing_b64, existing_size = self.state.page_images[page.page_id]
            print(f"  相似度达到阈值: 候选页面 vs {page.page_id}，交由 VLM 二次验证")
            if self._compare_with_vlm(
                capture, page, existing_b64, existing_size,
                context="find_existing_page",
                fast_visual=self.fast_visual_compare(capture, existing_b64),
                candidate_elements=candidate_elements,
                semantic_retrieval={
                    "rank": 1,
                    "similarity": best.similarity,
                    "threshold": self.config.semantic_similarity_threshold,
                    "same_package_compared_count": best.compared_count,
                    "candidate_package": current_package,
                    "existing_package": page.app_package,
                    "candidate_description": candidate.description,
                    "existing_description": page.page_description,
                    "embedding_model": candidate.model,
                    "fallback_error": candidate.error,
                },
            ):
                return ExistingPageMatch(page, "vlm", "VLM confirmed best semantic candidate")
            verification = getattr(self.analyzer, "last_comparison", {})
            if isinstance(verification, dict) and verification.get("same_page") is False:
                try:
                    self.semantic.apply_refined_descriptions(
                        candidate,
                        page,
                        str(verification.get("existing_description") or "").strip(),
                        str(verification.get("new_description") or "").strip(),
                        existing_b64,
                    )
                except Exception as exc:
                    raise SemanticDedupFailed(
                        f"VLM 拒绝匹配后更新页面描述 Embedding 失败: {type(exc).__name__}: {exc}"
                    ) from exc
            # Rejection of the first candidate must not suppress another exact visual match.
            alternatives = []
            remaining = [other for other in self.state.graph.pages.values()
                         if other.page_id != page.page_id and other.app_package == current_package]
            for other in remaining:
                other_b64, _ = self.state.page_images[other.page_id]
                visual = self.fast_visual_compare(capture, other_b64)
                if visual.available and visual.same_screen:
                    alternatives.append((other, visual, "visual_recall_after_rejection"))
            if verification.get("identity_contract") == "template_state_v1":
                for _ in range(2):
                    ranked = self.semantic.best_match(candidate, remaining, current_package)
                    if ranked is None or ranked.similarity < self.config.semantic_similarity_threshold:
                        break
                    remaining = [other for other in remaining if other.page_id != ranked.page.page_id]
                    if all(other.page_id != ranked.page.page_id for other, _, _ in alternatives):
                        other_b64, _ = self.state.page_images[ranked.page.page_id]
                        alternatives.append((ranked.page, self.fast_visual_compare(capture, other_b64),
                                             "semantic_alternative_after_rejection"))
            for rank, (other, visual, source) in enumerate(alternatives[:2], start=2):
                other_b64, other_size = self.state.page_images[other.page_id]
                if self._compare_with_vlm(capture, other, other_b64, other_size,
                        context="find_existing_page", fast_visual=visual, candidate_elements=candidate_elements,
                        semantic_retrieval={"rank": rank, "source": source,
                                            "candidate_description": candidate.description}):
                    return ExistingPageMatch(other, "vlm", "VLM confirmed alternative candidate")
            return None

        mapped_id = self.state.hash_to_page_id.get(capture.screenshot_hash)
        if mapped_id is not None:
            page_state = self.state.graph.pages[mapped_id]
            existing_b64, _ = self.state.page_images[mapped_id]
            fast_visual = self.fast_visual_compare(capture, existing_b64)
            if fast_visual.available and fast_visual.same_screen:
                return ExistingPageMatch(page_state, "visual_similarity", fast_visual.reason)
            if not fast_visual.available:
                # Compatibility fallback for synthetic/non-image captures in tests.
                return ExistingPageMatch(page_state, "fingerprint_exact", fast_visual.reason)

        for page_id in self.state.graph.page_queue:
            existing_b64, existing_size = self.state.page_images[page_id]
            fast_visual = self.fast_visual_compare(capture, existing_b64)
            if fast_visual.available and fast_visual.same_screen:
                page_state = self.state.graph.pages[page_id]
                print(f"  快速视觉判重: 复用 {page_id} ({fast_visual.reason})")
                return ExistingPageMatch(page_state, "visual_similarity", fast_visual.reason)

            print(f"  VLM 语义判重: 候选页面 vs {page_id}")
            page_state = self.state.graph.pages[page_id]
            if self._compare_with_vlm(
                capture,
                page_state,
                existing_b64,
                existing_size,
                context="find_existing_page",
                fast_visual=fast_visual,
                candidate_elements=candidate_elements,
            ):
                return ExistingPageMatch(page_state, "vlm", "VLM semantic match")
        return None

    def screen_matches_page(self, capture: ScreenCapture, page_state: PageState) -> bool:
        if not self.is_target_activity(self.controller.get_current_activity()):
            return False
        existing_b64, existing_size = self.state.page_images[page_state.page_id]
        fast_visual = self.fast_visual_compare(capture, existing_b64)
        mapped_page_id = self.state.hash_to_page_id.get(capture.screenshot_hash)
        if not self.semantic.enabled and mapped_page_id == page_state.page_id:
            if not fast_visual.available or fast_visual.same_screen:
                return True
        if not self.semantic.enabled and fast_visual.available and fast_visual.same_screen:
            self.state.hash_to_page_id[capture.screenshot_hash] = page_state.page_id
            return True
        same_page = self._compare_with_vlm(
            capture,
            page_state,
            existing_b64,
            existing_size,
            context="screen_matches_page",
            fast_visual=fast_visual,
        )
        if same_page:
            self.state.hash_to_page_id[capture.screenshot_hash] = page_state.page_id
        return same_page

    def verify_replay_state(self, capture: ScreenCapture, expected: dict, *,
                            action: ActionSequenceStep | None = None,
                            next_action: ActionSequenceStep | None = None) -> tuple[bool, str]:
        """Always ask the VLM; structured observations are context, not vetoes."""
        page = self.state.graph.pages.get(self.state.resolve_page_id(str(expected.get("page_id", ""))))
        reference = self.state.recovery_images.get(str(expected.get("layout_id", "")))
        reference_is_template = reference is None
        if reference is None and page is not None and page.page_id in self.state.page_images:
            image, size = self.state.page_images[page.page_id]
            reference = ScreenCapture(image, page.screenshot_hash, size)

        def operation(step):
            if step is None:
                return None
            return {"action_type": step.action_type, "target_description": step.target_description,
                    "element_text": step.element_text, "input_text": step.input_text,
                    "input_mode": step.input_mode, "field_key": step.field_key,
                    "target_value": step.target_value,
                    "previous_values": step.source_guard.get("values", {})}

        result = self.analyzer.verify_replay_state(
            capture.image_b64, capture.screen_size,
            expected={**expected, "current_activity": self.controller.get_current_activity()},
            page_description=(page.page_description or page.page_type) if page else "",
            reference_b64=reference.image_b64 if reference else None,
            reference_size=reference.screen_size if reference else None,
            reference_is_template=reference_is_template,
            action=operation(action), next_action=operation(next_action),
        )
        valid = (isinstance(result, dict) and result.get("status") == "success"
                 and type(result.get("matched")) is bool)
        matched = valid and result["matched"]
        reason = str(result.get("reason") or "") if isinstance(result, dict) else "invalid VLM response"
        self.state.append_observation({"kind": "state_restore", "event": "vlm_verification",
            "page_id": expected.get("page_id", ""), "arrived": matched,
            "verification_method": "vlm", "action": operation(action),
            "expected_observation": {key: expected.get(key) for key in ("state", "context", "values")},
            "expected_guard": expected, "next_action": operation(next_action),
            "reference_is_template": reference_is_template,
            "current_layout_id": layout_id(capture),
            "vlm_result": result, "reason": reason,
            "current_fingerprint": capture.screenshot_hash,
            "last_observed_screenshot": self.state.save_page_screenshot(
                f"restore_vlm_{len(self.state.graph.observations)}", capture)})
        if not valid:
            raise VLMComparisonFailed(f"VLM 回放校验失败: {reason}")
        return matched, reason

    @staticmethod
    def has_executable_action(
        action: ExplorationAction,
        screen_size: Tuple[int, int],
    ) -> bool:
        screen_width, screen_height = screen_size

        def point_is_visible(point: Optional[dict]) -> bool:
            if point is None:
                return True
            return 0 <= point.get("x", -1) < screen_width and 0 <= point.get("y", -1) < screen_height

        if action.action_type == "swipe":
            return point_is_visible(action.swipe_start) and point_is_visible(action.swipe_end)
        if action.bounds.get("width", 0) <= 0 or action.bounds.get("height", 0) <= 0:
            return False
        tap_x, tap_y = ActionCodec.tap_point_for_bounds(action.bounds)
        return 0 <= tap_x < screen_width and 0 <= tap_y < screen_height
