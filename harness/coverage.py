"""Synchronous topology checkpoints: text audit, visual merge verification, scheduling.

Scores estimate exploration priority, never the fraction of all App functionality.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .config import ExplorerConfig
from .contracts import PageState
from .state import ExplorationState
from .verify import ExplorationVerifier
from .memory import GraphMemory


@dataclass(frozen=True)
class CheckpointResult:
    current_page_id: str
    target_page_id: str | None
    merged_count: int


class CoverageCheckpoint:
    def __init__(self, config: ExplorerConfig, state: ExplorationState,
                 analyzer: Any, verifier: ExplorationVerifier, memory: GraphMemory | None = None):
        self.config = config
        self.state = state
        self.analyzer = analyzer
        self.verifier = verifier
        self.memory = memory
        self.last_step = 0

    def should_run(self, step: int) -> bool:
        interval = self.config.coverage_checkpoint_steps
        return (
            interval > 0 and step > 0 and step > self.last_step
            and (self.config.max_steps <= 0 or step < self.config.max_steps)
            and step % interval == 0
        )

    @staticmethod
    def _package(page: PageState) -> str:
        return page.app_package or page.activity.split("/", 1)[0].strip()

    def _adjacency(self) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        outgoing = {page_id: set() for page_id in self.state.graph.pages}
        incoming = {page_id: set() for page_id in outgoing}
        for edge in self.state.graph.transitions:
            if edge.destination_verified and edge.from_page in outgoing and edge.to_page in incoming:
                outgoing[edge.from_page].add(edge.to_page)
                incoming[edge.to_page].add(edge.from_page)
        return outgoing, incoming

    def format_for_audit(self) -> str:
        outgoing, incoming = self._adjacency()
        nodes = []
        for page_id, page in self.state.graph.pages.items():
            processed = {action.element_id for action in page.planned_actions[:page.next_action_index]}
            nodes.append({
                "node_id": page_id, "app_package": self._package(page),
                "description": page.page_description or page.page_type,
                "activities": page.activities or [page.activity],
                "state": page.semantic_state, "state_history": page.semantic_state_history,
                "state_count": len(page.states),
                "visit_count": page.visit_count, "status": page.exploration_status,
                "in_degree": len(incoming[page_id]), "out_degree": len(outgoing[page_id]),
                "elements": [{
                    "id": element.id, "text": element.text, "description": element.description,
                    "type": element.type, "processed": element.id in processed if self.state.work_registry is None else None,
                } for element in page.elements],
                "exploration_checklist": self.state.checklist(page),
                "state_work": [{"work_id": work.work_id, "state": work.normalized_state,
                                "context": work.context, "status": work.status}
                               for work in self.state.graph.work_items.values()
                               if work.page_id == page_id and work.status != "merged"],
            })
        return json.dumps({
            "root_page_id": self.state.graph.root_page_id, "nodes": nodes,
            "edges": [{
                "from": edge.from_page, "to": edge.to_page, "action": edge.action,
                "from_state_id": edge.from_state_id, "to_state_id": edge.to_state_id,
                "instruction": edge.action_details.target_description,
                "success": edge.success, "state_changed": edge.state_changed,
                "outcome": edge.outcome, "destination_verified": edge.destination_verified,
            } for edge in self.state.graph.transitions],
        }, ensure_ascii=False)

    def run_node_merge_audit(self) -> dict:
        audit = getattr(self.analyzer, "audit_graph_nodes", None)
        if not callable(audit):
            return {"results": [], "merged_count": 0, "status": "unsupported_analyzer"}
        try:
            response = audit(self.format_for_audit())
            if not isinstance(response, dict) or not isinstance(response.get("issues"), list):
                raise ValueError("Graph audit must return an issues list")
        except Exception as exc:
            return {"results": [], "merged_count": 0, "status": "failed", "error": str(exc)}

        results = []
        seen = set()
        for issue in response["issues"]:
            record = {"issue": issue, "status": "skipped"}
            results.append(record)
            if not isinstance(issue, dict) or not all(
                isinstance(issue.get(key), str) for key in ("node_a", "node_b")
            ):
                record["reason"] = "invalid node pair"
                continue
            first_id = self.state.resolve_page_id(issue["node_a"])
            second_id = self.state.resolve_page_id(issue["node_b"])
            pair = tuple(sorted((first_id, second_id), key=self.state.page_order))
            if first_id == second_id or pair in seen:
                record["reason"] = "already merged or duplicate proposal"
                continue
            seen.add(pair)
            if any(page_id not in self.state.graph.pages for page_id in pair):
                record["reason"] = "node missing"
                continue
            first, second = (self.state.graph.pages[page_id] for page_id in pair)
            if self._package(first) != self._package(second) or (
                self.config.app_package and self._package(first) != self.config.app_package
            ):
                record["reason"] = "different or non-target App package"
                continue
            if any(not self.state.page_images.get(page_id, ("",))[0] for page_id in pair):
                record["reason"] = "missing reference screenshot"
                continue
            try:
                verified = self.verifier.verify_merge_pair(first, second)
            except Exception as exc:
                record.update(status="verification_failed", reason=str(exc))
                continue
            if not verified:
                record.update(status="kept_separate", reason="screenshot or actionable surface differs")
                continue
            kept = self.state.merge_pages(*pair)
            record.update(status="merged", kept=kept, removed=pair[1])
        return {
            "status": "complete", "results": results,
            "merged_count": sum(record["status"] == "merged" for record in results),
            "summary": str(response.get("summary") or ""),
        }

    def get_exploration_target_candidates(self, current_page_id: str) -> list[dict]:
        if self.state.work_registry is not None:
            return self.get_state_target_candidates(current_page_id)
        outgoing, _ = self._adjacency()
        root = self.state.graph.root_page_id
        if root not in outgoing:
            return []
        reachable, frontier = set(), [root]
        while frontier:
            page_id = frontier.pop()
            if page_id in reachable:
                continue
            page = self.state.graph.pages[page_id]
            if self.config.app_package and self._package(page) != self.config.app_package:
                continue
            reachable.add(page_id)
            frontier.extend(outgoing[page_id] - reachable)
        candidates = []
        for page_id in reachable:
            page = self.state.graph.pages[page_id]
            # This harness has finite per-page plans: revisiting exhausted pages adds no work.
            if not self.state.can_explore(page):
                continue
            actions = self.state.remaining_actions(page)
            unexplored = {action.element_id or action.target_description for action in actions}
            out_degree = len(outgoing[page_id])
            score = len(unexplored) * 3 + max(0, 3 - out_degree) + max(0, 3 - page.visit_count)
            checklist = self.state.checklist(page)
            unresolved = sum(item["status"] in {"attempted_unresolved", "needs_verification"}
                             and item["pending_in_plan"] for item in checklist)
            # Only pending, budget-eligible work can be prioritized. Do not reopen exhausted loops.
            score += 2 * unresolved
            candidates.append({
                "node_id": page_id, "page_description": page.page_description or page.page_type,
                "visit_count": page.visit_count, "out_degree": out_degree,
                "unexplored_elements": len(unexplored), "total_elements": len(page.elements),
                "unexplored_element_descriptions": [action.target_description for action in actions[:8]],
                "score": score,
                "unresolved_pending_actions": unresolved,
                "exploration_checklist": checklist[:8],
            })
        candidates.sort(key=lambda item: (
            -item["score"], -item["unexplored_elements"], item["out_degree"],
            item["visit_count"], self.state.page_order(item["node_id"]),
        ))
        return [item for item in candidates[:self.config.coverage_checkpoint_top_k]
                if item["node_id"] != current_page_id]

    def get_state_target_candidates(self, current_work_id: str) -> list[dict]:
        registry = self.state.work_registry
        candidates = []
        for work in self.state.graph.work_items.values():
            if work.status == "merged" or work.work_id == registry.resolve(current_work_id):
                continue
            actions = registry.candidates(work)
            if not actions:
                continue
            page = self.state.graph.pages[work.page_id]
            if self.config.app_package and self._package(page) != self.config.app_package:
                continue
            candidates.append({
                "node_id": work.work_id, "work_id": work.work_id, "page_id": work.page_id,
                "page_description": page.page_description or page.page_type,
                "state": work.normalized_state, "context": work.context,
                "unexplored_elements": len(actions), "visit_count": work.visit_count,
                "restore_failures": work.restore_failures,
                "route_requires_live_access": any(not step.replay_safe or not step.arrival_verified
                                                   or not step.source_guard or not step.target_guard
                                                   for step in work.entry_path),
                "score": len(actions) * 3 - len(work.entry_path) - work.restore_failures * 5,
                "exploration_checklist": registry.checklist(work),
            })
        candidates.sort(key=lambda item: (-item["score"], item["work_id"]))
        return candidates[:self.config.coverage_checkpoint_top_k]

    def run_work_checkpoint(self, current_work_id: str) -> str | None:
        step = self.state.graph.completed_steps
        if not self.should_run(step):
            return None
        self.last_step = step
        audit = self.run_node_merge_audit()
        registry = self.state.work_registry
        current_work_id = registry.resolve(current_work_id)
        candidates = self.get_state_target_candidates(current_work_id)
        selected = candidates[0]["work_id"] if candidates else None
        choose = getattr(self.analyzer, "select_exploration_target", None)
        selection = {"status": "fallback" if candidates else "no_candidates"}
        if candidates and callable(choose):
            try:
                response = choose(candidates)
                work_id = response.get("work_id", response.get("node_id")) if isinstance(response, dict) else response
                if work_id in {item["work_id"] for item in candidates}:
                    selected = work_id
                    selection = {"status": "model", "response": response}
            except Exception as exc:
                selection["reason"] = str(exc)
        self.state.record_coverage_checkpoint({
            "step": step, "current_work_id": current_work_id, "audit": audit,
            "candidates": candidates, "selected_work_id": selected, "selection": selection,
        })
        return selected

    def run(self, step: int, current_page_id: str) -> CheckpointResult | None:
        if not self.should_run(step):
            return None
        self.last_step = step
        print(f"\n[Coverage] 第 {step} 步：审计重复节点并调整探索目标")
        audit = self.run_node_merge_audit()
        current_page_id = self.state.resolve_page_id(current_page_id)
        candidates = self.get_exploration_target_candidates(current_page_id)
        selected = None
        selection = {"status": "no_candidates"}
        if candidates:
            if self.memory and self.config.graph_memory:
                # Bounded scheduling-specific context, with no extra model request.
                context = self.memory.context(
                    self.state.graph, current_page_id=current_page_id,
                    query=" ".join(item["page_description"] for item in candidates), purpose="schedule",
                )
                for candidate in candidates:
                    candidate["node_evidence"] = next((
                        page["node_evidence"] for page in context["pages"]
                        if page["source"] == "live" and page["page_id"] == candidate["node_id"]
                        and "node_evidence" in page
                    ), None)
                    candidate["relevant_experiences"] = [
                        item for item in context["experiences"]
                        if item["source"].get("description") == candidate["page_description"]
                    ]
            selected = candidates[0]["node_id"]
            selection = {"status": "fallback", "reason": "invalid or unavailable model selection"}
            choose = getattr(self.analyzer, "select_exploration_target", None)
            if callable(choose):
                try:
                    response = choose(candidates)
                    node_id = response.get("node_id") if isinstance(response, dict) else response
                    if isinstance(node_id, str) and node_id in {item["node_id"] for item in candidates}:
                        selected = node_id
                        selection = {"status": "model", "response": response}
                except Exception as exc:
                    selection["reason"] = str(exc)
        self.state.record_coverage_checkpoint({
            "step": step, "current_page_id": current_page_id, "audit": audit,
            "candidates": candidates, "selected_page_id": selected, "selection": selection,
        })
        self.state.save_graph(announce=False)
        return CheckpointResult(current_page_id, selected, audit["merged_count"])
