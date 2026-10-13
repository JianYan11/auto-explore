"""Memory-assisted selection among existing pending actions, without inventing controls."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .contracts import ExplorationAction, PageState, ScreenCapture, StateWorkItem
from .actions import ActionCodec
from .grounding import bind_location, layout_id, matches_source
from .memory import GraphMemory, usable_input
from .state import ExplorationState
from .knowledge import action_key
from .work import local_action_key
from .metrics import timed_phase


class MemoryGuidedPlanner:
    def __init__(self, memory: GraphMemory, state: ExplorationState, analyzer: Any):
        self.memory = memory
        self.state = state
        self.analyzer = analyzer
        self.state.memory_sources = list(memory.sources)
        self.state.historical_observations = list(memory.observations)

    @timed_phase("planning_seconds")
    def select(self, page: PageState, capture: ScreenCapture,
               actions: list[ExplorationAction], *, work: StateWorkItem | None = None,
               current_guard: dict | None = None) -> tuple[int, ExplorationAction] | None:
        start = 0 if work is not None else page.next_action_index
        if start >= len(actions):
            return None
        choose = getattr(self.analyzer, "choose_next_action", None)
        context = self.memory.context(
            self.state.graph,
            query=" ".join([
                page.page_description, *page.content_hints[:8],
                *(action.target_description + " " + action.element_text for action in actions[start:start + 12]),
            ]),
            activity=page.activity,
            current_page_id=page.page_id, action_sequence=work.entry_path if work else page.action_sequence,
            purpose="action",
            current_work_id=work.work_id if work else "",
        ) if self.memory.config.graph_memory else {}
        checklist = {item["action_key"]: item for item in self.state.checklist(page)}
        if work is not None:
            checklist = {action_key(action): work.action_progress.get(local_action_key(action), {}) for action in actions}
            context = {**context, "current_work": {
                "work_id": work.work_id, "state": work.normalized_state, "context": work.context,
                "values": (current_guard or work.restore_guard).get("values", {}),
            }}
            if self.state.work_registry is not None:
                context["node_exploration_summary"] = self.state.work_registry.exploration_summary(work)
        candidates = [{
            "action_index": index, "action_type": action.action_type,
            "target_description": action.target_description, "element_text": action.element_text,
            "element_type": action.element_type, "input_text": action.input_text,
            "exploration_status": checklist.get(action_key(action), {}).get("status", "untried"),
            "prior_attempts": checklist.get(action_key(action), {}).get("attempts", 0),
            "verification_of": action.verification_of,
            "source_layout_id": action.source_layout_id,
            "workflow_role": action.workflow_role,
            "input_mode": action.input_mode, "field_key": action.field_key,
            "control_key": action.control_key, "effect_kind": action.effect_kind,
            "intent": action.intent, "relevant_state_keys": action.relevant_state_keys,
        } for index, action in enumerate(actions) if index >= start]
        record = {
            "phase": "select_action", "step": self.state.graph.completed_steps,
            "page_id": page.page_id, "memory": context, "candidates": candidates,
            "status": "fallback",
            "current_layout_id": layout_id(capture), "screen_size": capture.screen_size,
            "work_id": work.work_id if work else "",
        }
        selected = None
        declined = False
        try:
            result = choose(capture.image_b64, capture.screen_size, candidates, context) if callable(choose) else None
            record["model_result"] = result
            if isinstance(result, dict):
                index = result.get("action_index")
                declined = (("action_index" in result and index is None)
                            or ("found" in result and result["found"] is not True))
                if not declined and type(index) is int and start <= index < len(actions):
                    action = actions[index]
                    if action.action_type == "input":
                        text = action.input_text if action.verification_of else result.get("input_text")
                        if action.input_mode == "clear" or usable_input(text):
                            selected = index, replace(action, input_text="" if action.input_mode == "clear" else text.strip())
                        else:
                            record["reason"] = "Model input was empty, generic or unsupported by ADB"
                    else:
                        selected = index, action
                    if selected is not None:
                        if "action" in result:
                            grounded = bind_location(selected[1], result["action"], capture, "selection")
                            selected = (index, grounded) if grounded is not None else None
                            if grounded is None:
                                declined = True  # Never fall back to the old box after bad grounding.
                                record["reason"] = "Selected target has missing or invalid current coordinates"
                        else:
                            # Compatibility for older injected selectors: explicitly locate now.
                            grounded = self._relocate(selected[1], capture, record)
                            selected = (index, grounded) if grounded is not None else None
                            declined = grounded is None
                        if selected is not None:
                            record["status"] = "model"
        except Exception as exc:
            selected = None
            record["reason"] = str(exc)
        if selected is None and not declined:
            usable = [index for index in range(start, len(actions))
                      if actions[index].action_type != "input" or actions[index].input_mode == "clear"
                      or usable_input(actions[index].input_text)]
            # Prefer fresh branches on model failure; never exclude a target based on memory.
            usable.sort(key=lambda index: (
                checklist.get(action_key(actions[index]), {}).get("attempts", 0), index,
            ))
            if usable:
                index = usable[0]
                action = actions[index]
                if (not callable(choose) and not page.requires_action_relocation
                        and not action.verification_of and matches_source(action, capture)):
                    # A legacy analyzer may reuse geometry from this exact initial analysis.
                    grounded = action
                else:
                    grounded = self._relocate(action, capture, record)
                selected = (index, grounded) if grounded is not None else None
        if selected is None:
            record["status"] = "blocked"
            record.setdefault("reason", "No suitable action or grounded supported input")
        else:
            record["selected_action_index"] = selected[0]
            record["selected_input_text"] = selected[1].input_text
            record["grounding"] = {
                "source_layout_id": selected[1].source_layout_id,
                "method": selected[1].grounding_method, "bounds": selected[1].bounds,
                "swipe_start": selected[1].swipe_start, "swipe_end": selected[1].swipe_end,
            }
        self.state.record_memory_planning(record)
        return selected

    def _relocate(self, action: ExplorationAction, capture: ScreenCapture, record: dict) -> ExplorationAction | None:
        locate = getattr(self.analyzer, "locate_replay_action", None)
        if not callable(locate):
            record["reason"] = "Analyzer cannot locate the target in the current screenshot"
            return None
        try:
            raw = locate(capture.image_b64, capture.screen_size, ActionCodec.to_sequence_step(action))
            grounded = bind_location(action, raw, capture, "relocation")
            if grounded is None:
                record["reason"] = "Target could not be located with valid current coordinates"
            return grounded
        except Exception as exc:
            record["reason"] = f"Target relocation failed: {exc}"
            return None
