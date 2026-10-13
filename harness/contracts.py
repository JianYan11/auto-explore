"""Data contracts passed between ETCSLV harness components."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class InputFieldObservation:
    """Current screenshot evidence for an editable field, not its planned input."""

    bounds: Dict[str, int]
    value: Optional[str] = None


@dataclass
class ExplorationAction:
    """An action planned by the VLM and executed by a device tool."""

    action_type: str
    target_description: str
    element_id: Optional[str] = None
    element_text: str = ""
    element_type: str = "unknown"
    bounds: Dict[str, int] = field(default_factory=dict)
    input_text: str = ""
    swipe_start: Optional[Dict[str, int]] = None
    swipe_end: Optional[Dict[str, int]] = None
    duration_ms: int = 300
    verification_of: str = ""
    # These identify the screenshot in which the stored coordinates were located.
    source_layout_id: str = ""
    source_screen_size: Optional[Tuple[int, int]] = None
    grounding_method: str = ""
    action_family: str = ""
    intent: str = "unknown"
    coverage_scope: str = "context"
    relevant_state_keys: Optional[List[str]] = None
    workflow_role: str = "explore"
    replay_safe: bool = False
    control_key: str = ""
    field_key: str = ""
    field_value: Optional[str] = None
    target_value: str = ""
    input_mode: str = "replace"
    effect_kind: str = "unknown"


@dataclass
class ActionSequenceStep:
    """A recorded action with coordinates and a semantic relocation fallback."""

    action_type: str
    target_description: str
    element_text: str = ""
    element_type: str = "unknown"
    input_text: str = ""
    duration_ms: int = 300
    bounds: Dict[str, int] = field(default_factory=dict)
    swipe_start: Optional[Dict[str, int]] = None
    swipe_end: Optional[Dict[str, int]] = None
    screen_size: Optional[Tuple[int, int]] = None
    source_layout_id: str = ""
    grounding_method: str = ""
    replay_safe: bool = False
    source_guard: Dict[str, object] = field(default_factory=dict)
    target_guard: Dict[str, object] = field(default_factory=dict)
    control_key: str = ""
    field_key: str = ""
    target_value: str = ""
    input_mode: str = "replace"
    effect_kind: str = "unknown"
    arrival_verified: bool = True


@dataclass
class UIElement:
    id: str
    type: str
    text: str
    description: str
    bounds: Dict[str, int]
    clickable: bool
    resource_id: str
    priority: int = 0
    control_key: str = ""
    field_key: str = ""
    value: Optional[str] = None


@dataclass
class PageVisualState:
    """One observed appearance within a deduplicated page node."""

    state_id: str
    screenshot_path: str
    screenshot_hash: str
    activity: str
    page_description: str
    semantic_state: Dict[str, object]
    action_sequence: List[ActionSequenceStep]
    timestamp: float
    observation_ids: List[str] = field(default_factory=list)
    actionable_signature: str = ""
    behavioral_state: Optional[Dict[str, object]] = None
    context: Dict[str, object] = field(default_factory=dict)
    entry_routes: List[List[ActionSequenceStep]] = field(default_factory=list)


@dataclass
class PageState:
    page_id: str
    screenshot_path: str
    screenshot_hash: str
    activity: str
    page_type: str
    elements: List[UIElement]
    recommended_actions: List[str]
    planned_actions: List[ExplorationAction]
    action_sequence: List[ActionSequenceStep]
    timestamp: float
    next_action_index: int = 0
    exploration_status: str = "pending"
    page_description: str = ""
    semantic_state: Dict[str, object] = field(default_factory=dict)
    description_embedding: List[float] = field(default_factory=list)
    embedding_model: str = ""
    semantic_error: str = ""
    app_package: str = ""
    visit_count: int = 0
    activities: List[str] = field(default_factory=list)
    semantic_state_history: Dict[str, List[object]] = field(default_factory=dict)
    requires_action_relocation: bool = False
    content_hints: List[str] = field(default_factory=list)
    content_inventory: Dict[str, object] = field(default_factory=dict)
    representative_state_id: str = ""
    states: Dict[str, PageVisualState] = field(default_factory=dict)
    control_aliases: Dict[str, str] = field(default_factory=dict)


@dataclass
class Transition:
    from_page: str
    to_page: str
    action: str
    element_id: Optional[str]
    element_text: str
    tap_x: Optional[int]
    tap_y: Optional[int]
    success: bool
    state_changed: bool
    action_details: ExplorationAction
    before_screenshot: str
    after_screenshot: str
    recovery: str
    observation_id: str = ""
    outcome: str = "legacy_unknown"
    destination_verified: bool = False
    from_state_id: str = ""
    to_state_id: str = ""
    from_work_id: str = ""
    to_work_id: str = ""
    purpose: str = "exploration"


@dataclass
class StateWorkItem:
    """A bounded exploration frontier entry, separate from visual evidence."""

    work_id: str
    page_id: str
    state_key: str
    context_key: str
    module: str
    normalized_state: Dict[str, object]
    context: Dict[str, object]
    observed_state_ids: List[str]
    planned_actions: List[ExplorationAction]
    elements: List[UIElement]
    entry_path: List[ActionSequenceStep]
    restore_guard: Dict[str, object]
    workflow_id: str = ""
    action_progress: Dict[str, dict] = field(default_factory=dict)
    status: str = "pending"
    attempts: int = 0
    navigation_steps: int = 0
    restore_failures: int = 0
    visit_count: int = 0
    budget_reason: str = ""
    analysis_error: str = ""
    entry_routes: List[List[ActionSequenceStep]] = field(default_factory=list)


@dataclass
class AppGraph:
    app_package: str
    app_name: str
    pages: Dict[str, PageState]
    transitions: List[Transition]
    exploration_time: float
    root_page_id: Optional[str] = None
    page_queue: List[str] = field(default_factory=list)
    exploration_order: List[str] = field(default_factory=list)
    completed_steps: int = 0
    node_aliases: Dict[str, str] = field(default_factory=dict)
    run_id: str = ""
    memory_scope: str = ""
    observations: List[dict] = field(default_factory=list)
    work_items: Dict[str, StateWorkItem] = field(default_factory=dict)
    work_queue: List[str] = field(default_factory=list)
    work_order: List[str] = field(default_factory=list)
    work_aliases: Dict[str, str] = field(default_factory=dict)
    functional_coverage: Dict[str, dict] = field(default_factory=dict)
    baseline: Dict[str, object] = field(default_factory=dict)
    termination_reason: str = "running"
    timings: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ScreenCapture:
    image_b64: str
    screenshot_hash: str
    screen_size: Tuple[int, int]


@dataclass(frozen=True)
class RunContext:
    """Stable context shared by every step in one run."""

    app_package: str
    app_activity: str
    max_pages: int
    max_depth: int


@dataclass(frozen=True)
class StepContext:
    """Bounded context for one page exploration decision."""

    page_id: str
    depth: int
    next_action_index: int
    action_sequence: Tuple[ActionSequenceStep, ...]
