"""Domain action parsing and normalization."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from .contracts import ActionSequenceStep, ExplorationAction, UIElement


class ActionCodec:
    """Converts untrusted model payloads into harness action contracts."""

    @classmethod
    def planned_actions_from_analysis(
        cls,
        analysis: Dict[str, Any],
        elements: List[UIElement],
        recommended_ids: List[str],
    ) -> List[ExplorationAction]:
        id_to_element = {element.id: element for element in elements}
        raw_elements = {
            str(raw.get("id")): raw
            for raw in analysis.get("elements", [])
            if isinstance(raw, dict) and raw.get("id")
        }
        actions: List[ExplorationAction] = []
        covered_ids: Set[str] = set()

        raw_actions = analysis.get("actions", [])
        if not isinstance(raw_actions, list):
            raw_actions = []
        for raw_action in raw_actions:
            if isinstance(raw_action, str):
                element_id = raw_action
                raw_action = {}
            elif isinstance(raw_action, dict):
                element_id = str(raw_action.get("element_id") or "")
            else:
                continue
            if not element_id or element_id not in id_to_element:
                print(f"  忽略元素列表之外的 VLM 动作: {element_id or '<missing>'}")
                continue
            if element_id in covered_ids:
                continue
            action = cls.action_from_model(raw_action, element=id_to_element[element_id])
            if action.element_id:
                covered_ids.add(action.element_id)
            actions.append(action)

        ordered_fallback_ids = list(recommended_ids)
        ordered_fallback_ids.extend(element.id for element in elements)
        for element_id in ordered_fallback_ids:
            if element_id in covered_ids or element_id not in id_to_element:
                continue
            element = id_to_element[element_id]
            raw_element = raw_elements.get(element_id, {})
            nested_action = raw_element.get("action")
            if isinstance(nested_action, dict):
                action_payload = dict(nested_action)
            elif isinstance(nested_action, str):
                action_payload = {"action_type": nested_action}
            else:
                action_payload = {
                    key: raw_element[key]
                    for key in (
                        "action_type",
                        "input_text",
                        "swipe_start",
                        "swipe_end",
                        "duration_ms",
                        "control_key", "field_key", "field_value", "target_value", "input_mode",
                        "effect_kind", "intent", "action_family", "workflow_role", "replay_safe",
                        "coverage_scope", "relevant_state_keys",
                    )
                    if key in raw_element
                }
            action_payload.setdefault("element_id", element_id)
            actions.append(cls.action_from_model(action_payload, element=element))
            covered_ids.add(element_id)
        return actions

    @classmethod
    def action_from_model(
        cls,
        raw: Any,
        *,
        element: Optional[UIElement] = None,
        step: Optional[ActionSequenceStep] = None,
    ) -> ExplorationAction:
        if isinstance(raw, ExplorationAction):
            return raw
        if not isinstance(raw, dict):
            raw = {}

        raw_action_type = (
            step.action_type
            if step is not None
            else raw.get("action_type") or raw.get("action") or raw.get("type")
        )
        if isinstance(raw_action_type, dict):
            raw_action_type = raw_action_type.get("type")
        action_type = str(raw_action_type or ("input" if element and element.type == "input" else "click")).lower()
        action_type = {
            "tap": "click",
            "press": "click",
            "type": "input",
            "text": "input",
            "scroll": "swipe",
        }.get(action_type, action_type)
        if action_type not in {"click", "swipe", "input"}:
            action_type = "click"

        raw_bounds = (
            raw.get("bbox_2d")
            or raw.get("bounds_1000")
            or raw.get("bounds_999")
            or raw.get("bbox_999")
            or raw.get("bounds")
            or raw.get("box")
            or raw.get("bbox")
            or raw.get("start_box")
            or raw.get("coordinate")
        )
        bounds = cls.normalize_bounds(raw_bounds)
        if element is not None and action_type in {"click", "input"}:
            bounds = dict(element.bounds)
        elif bounds.get("width", 0) <= 0 and element is not None:
            bounds = dict(element.bounds)

        target_description = str(raw.get("target_description") or "").strip()
        if not target_description and element is not None:
            target_description = element.description or element.text or element.id
        if not target_description and step is not None:
            target_description = step.target_description

        element_text = str(raw.get("element_text") or "")
        if not element_text and element is not None:
            element_text = element.text
        if not element_text and step is not None:
            element_text = step.element_text

        element_type = str(raw.get("element_type") or "")
        if not element_type and element is not None:
            element_type = element.type
        if not element_type and step is not None:
            element_type = step.element_type

        input_text = step.input_text if step is not None else str(raw.get("input_text") or "")
        intent = raw.get("intent", "unknown")
        effect_kind = "query" if intent == "query" else raw.get("effect_kind", "unknown")
        replay_safe = (raw.get("replay_safe") is True
                       and intent in {"navigate", "input", "local", "query", "commit"}
                       and (intent != "commit" or effect_kind == "query")
                       and effect_kind != "persistent")
        return ExplorationAction(
            action_type=action_type,
            target_description=target_description,
            element_id=str(raw.get("element_id") or (element.id if element else "")) or None,
            element_text=element_text,
            element_type=element_type or "unknown",
            bounds=bounds,
            input_text=input_text,
            action_family=str(raw.get("action_family") or "").strip()[:160],
            intent=intent if intent in {"navigate", "input", "local", "query", "commit", "mutation"} else "unknown",
            coverage_scope="module" if raw.get("coverage_scope") == "module" else "context",
            relevant_state_keys=([str(key) for key in raw["relevant_state_keys"] if isinstance(key, str)]
                                 if isinstance(raw.get("relevant_state_keys"), list) else None),
            workflow_role=raw.get("workflow_role") if raw.get("workflow_role") in {"advance", "return"} else "explore",
            replay_safe=replay_safe,
            control_key=str(raw.get("control_key") or (element.control_key if element else "")),
            field_key=str(raw.get("field_key") or (element.field_key if element else "")),
            field_value=(element.value if element and element.value is not None else
                         raw.get("field_value") if isinstance(raw.get("field_value"), str) else None),
            target_value=str(raw.get("target_value") or ""),
            input_mode=raw.get("input_mode") if raw.get("input_mode") in {"replace", "append", "clear"} else "replace",
            effect_kind=effect_kind if effect_kind in {"query", "local", "persistent"} else "unknown",
            swipe_start=cls.normalize_point(raw.get("swipe_start") or raw.get("start")),
            swipe_end=cls.normalize_point(raw.get("swipe_end") or raw.get("end")),
            duration_ms=max(
                100,
                min(
                    5000,
                    cls.to_int(
                        step.duration_ms if step is not None else raw.get("duration_ms"),
                        default=300,
                    ),
                ),
            ),
        )

    @classmethod
    def element_from_model(cls, raw: Dict[str, Any]) -> Optional[UIElement]:
        element_id = str(raw.get("id") or "").strip()
        if not element_id:
            return None
        raw_bounds = (
            raw.get("bbox_2d")
            or raw.get("bounds_1000")
            or raw.get("bounds_999")
            or raw.get("bbox_999")
            or raw.get("bounds")
            or raw.get("box")
            or raw.get("bbox")
            or raw.get("start_box")
            or raw.get("coordinate")
        )
        return UIElement(
            id=element_id,
            type=str(raw.get("type") or "unknown"),
            text=str(raw.get("text") or raw.get("label") or raw.get("ref") or ""),
            description=str(raw.get("description") or ""),
            bounds=cls.normalize_bounds(raw_bounds),
            clickable=True,
            resource_id=str(raw.get("resource_id") or ""),
            priority=cls.to_int(raw.get("priority"), default=999),
            control_key=str(raw.get("control_key") or ""),
            field_key=str(raw.get("field_key") or ""),
            value=raw.get("value") if isinstance(raw.get("value"), str) else None,
        )

    @staticmethod
    def normalize_point(raw_point: Any) -> Optional[Dict[str, int]]:
        if isinstance(raw_point, dict) and "x" in raw_point and "y" in raw_point:
            try:
                return {"x": int(float(raw_point["x"])), "y": int(float(raw_point["y"]))}
            except (TypeError, ValueError):
                return None
        if isinstance(raw_point, (list, tuple)) and len(raw_point) >= 2:
            try:
                return {"x": int(float(raw_point[0])), "y": int(float(raw_point[1]))}
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def normalize_bounds(raw_bounds: Any) -> Dict[str, int]:
        if isinstance(raw_bounds, str):
            raw_bounds = raw_bounds.strip()
            for char in "[]()<>":
                raw_bounds = raw_bounds.replace(char, " ")
            parts = [part for part in raw_bounds.replace(",", " ").split() if part]
            try:
                raw_bounds = [float(part) for part in parts]
            except ValueError:
                return {"x": 0, "y": 0, "width": 0, "height": 0}

        if isinstance(raw_bounds, (list, tuple)):
            try:
                nums = [int(float(value)) for value in raw_bounds]
            except (TypeError, ValueError):
                return {"x": 0, "y": 0, "width": 0, "height": 0}
            if len(nums) >= 4:
                x1, y1, x2, y2 = nums[:4]
                return {
                    "x": min(x1, x2),
                    "y": min(y1, y2),
                    "width": abs(x2 - x1),
                    "height": abs(y2 - y1),
                }
            if len(nums) >= 2:
                x, y = nums[:2]
                return {"x": x - 1, "y": y - 1, "width": 2, "height": 2}

        if not isinstance(raw_bounds, dict):
            return {"x": 0, "y": 0, "width": 0, "height": 0}

        def value(name: str, default: int = 0) -> int:
            try:
                return int(float(raw_bounds.get(name, default)))
            except (TypeError, ValueError):
                return default

        if all(name in raw_bounds for name in ("x", "y", "width", "height")):
            return {
                "x": value("x"),
                "y": value("y"),
                "width": max(0, value("width")),
                "height": max(0, value("height")),
            }
        if all(name in raw_bounds for name in ("x1", "y1", "x2", "y2")):
            x1, y1, x2, y2 = (value(name) for name in ("x1", "y1", "x2", "y2"))
            return {
                "x": min(x1, x2),
                "y": min(y1, y2),
                "width": abs(x2 - x1),
                "height": abs(y2 - y1),
            }
        return {"x": 0, "y": 0, "width": 0, "height": 0}

    @staticmethod
    def tap_point_for_bounds(bounds: Dict[str, int]) -> Tuple[int, int]:
        return (
            int(bounds["x"]) + int(bounds["width"]) // 2,
            int(bounds["y"]) + int(bounds["height"]) // 2,
        )

    @staticmethod
    def to_sequence_step(
        action: ExplorationAction, screen_size: Optional[Tuple[int, int]] = None,
    ) -> ActionSequenceStep:
        start, end = action.swipe_start, action.swipe_end
        if action.action_type == "swipe" and screen_size is not None:
            # Record the same default gesture used by AndroidController.
            width, height = screen_size
            start = start or {"x": width // 2, "y": int(height * 0.75)}
            end = end or {"x": width // 2, "y": int(height * 0.25)}
        return ActionSequenceStep(
            action_type=action.action_type,
            target_description=action.target_description,
            element_text=action.element_text,
            element_type=action.element_type,
            input_text=action.input_text,
            duration_ms=action.duration_ms,
            bounds=dict(action.bounds),
            swipe_start=dict(start) if start is not None else None,
            swipe_end=dict(end) if end is not None else None,
            screen_size=screen_size or action.source_screen_size,
            source_layout_id=action.source_layout_id,
            grounding_method=action.grounding_method,
            replay_safe=action.replay_safe,
            control_key=action.control_key, field_key=action.field_key,
            target_value=action.target_value, input_mode=action.input_mode, effect_kind=action.effect_kind,
        )

    @staticmethod
    def action_from_sequence_step(step: ActionSequenceStep) -> ExplorationAction:
        """Reuse the recorded operation without asking the model to locate it."""
        return ExplorationAction(
            action_type=step.action_type,
            target_description=step.target_description,
            element_text=step.element_text,
            element_type=step.element_type,
            bounds=dict(step.bounds),
            input_text=step.input_text,
            swipe_start=dict(step.swipe_start) if step.swipe_start is not None else None,
            swipe_end=dict(step.swipe_end) if step.swipe_end is not None else None,
            duration_ms=step.duration_ms,
            source_layout_id=step.source_layout_id,
            source_screen_size=step.screen_size,
            grounding_method=step.grounding_method,
            replay_safe=step.replay_safe,
            control_key=step.control_key, field_key=step.field_key,
            target_value=step.target_value, input_mode=step.input_mode, effect_kind=step.effect_kind,
        )

    @staticmethod
    def to_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
