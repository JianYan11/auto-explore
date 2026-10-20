"""Shared summary model adapter and optional per-node interpretation.

App-level incremental summaries are owned by capabilities.AppCapabilityCatalog.
Per-node summaries are retained for consumers needing exact state/input cases;
the exploration loop does not start a second automatic summary pipeline.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from .config import ExplorerConfig
from .evidence import redact_evidence_text, live_node_evidence
from .knowledge import digest
from .model_api import chat_request_options, completion_token_budget

NODE_PROMPT_PATH = Path(__file__).parent / "prompts" / "node_function_memory.md"


def model_identity(name: str) -> str:
    return name.strip().rstrip("/").rsplit("/", 1)[-1].casefold()


def validate_summary_config(config: ExplorerConfig) -> None:
    if not math.isfinite(config.summary_budget_seconds) or config.summary_budget_seconds < 0:
        raise ValueError("--summary-budget-seconds must be a finite nonnegative number")
    if not math.isfinite(config.summary_timeout) or config.summary_timeout <= 0:
        raise ValueError("--summary-timeout must be a finite positive number")
    if config.summary_max_chars < 4000:
        raise ValueError("--summary-max-chars must be at least 4000")
    if not config.summary_model.strip():
        if config.summary_api_key:
            raise ValueError("Set --summary-model / SUMMARY_MODEL for the summary endpoint")
        return
    if not config.graph_memory:
        raise ValueError("--summary-model requires graph memory; remove --no-graph-memory")
    if model_identity(config.summary_model) == model_identity(config.model):
        raise ValueError("Experience summary model must differ from execution --model")


class ExperienceSummaryClient:
    """Independent model/session on the execution API service authorized by the user."""

    def __init__(self, config: ExplorerConfig):
        validate_summary_config(config)
        if not config.summary_model.strip():
            raise ValueError("No experience summary model configured")
        key = config.summary_api_key or config.vlm_api_key
        if not key:
            raise ValueError("Missing experience summary API key")
        from openai import OpenAI
        self.model = config.summary_model.strip()
        self.profile = config.model_api_profile
        self.request_options = chat_request_options(
            self.model, profile=config.model_api_profile,
            reasoning_effort=config.model_reasoning_effort,
        )
        self.client = OpenAI(base_url=config.vlm_base_url, api_key=key,
                             timeout=config.summary_timeout, max_retries=0)

    def summarize(self, system_prompt: str, evidence: dict) -> dict:
        is_catalog = evidence.get("task") == "app_capability_catalog"
        options = {"timeout": self.request_timeout} if hasattr(self, "request_timeout") else {}
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}],
            max_tokens=completion_token_budget(self.model, 4096 if is_catalog else 1800, profile=self.profile),
            **self.request_options,
            **options,
        )
        choice = response.choices[0]
        self.last_finish_reason = getattr(choice, "finish_reason", None)
        raw = (choice.message.content or "").strip()
        if raw.startswith("```json\n") and raw.endswith("```"):
            raw = raw[8:-3].strip()
        elif raw.startswith("```\n") and raw.endswith("```"):
            raw = raw[4:-3].strip()
        return json.loads(raw)


def validate_node_summary(response: object, payload: dict) -> dict:
    if not isinstance(response, dict) or set(response) != {"node_id", "page_role", "functions"}:
        raise ValueError("Invalid node summary schema")
    if response["node_id"] != payload["node_id"]:
        raise ValueError("Wrong node summary identity")

    def prose(value, limit):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError("Invalid node summary text")
        return redact_evidence_text(value, limit)

    role = prose(response["page_role"], 200)
    values = response["functions"]
    expected = {unit["unit_id"]: unit for unit in payload["units"]}
    if not isinstance(values, list) or len(values) != len(expected):
        raise ValueError("Each supplied UI case needs a summary")
    results = []
    seen = set()
    for item in values:
        if not isinstance(item, dict) or set(item) != {
            "unit_id", "function", "observed_effect", "applicability", "limitations", "evidence_ids",
        }:
            raise ValueError("Unexpected node function fields")
        uid = item["unit_id"]
        if not isinstance(uid, str) or uid not in expected or uid in seen:
            raise ValueError("Unknown or duplicate UI case")
        seen.add(uid)
        unit = expected[uid]
        ids = item["evidence_ids"]
        if (not isinstance(ids, list) or len(ids) > 16
                or any(not isinstance(oid, str) or oid not in unit["evidence_ids"] for oid in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("Citation belongs to another UI/state or is unknown")
        # Every supplied effect/error/recovery variant must remain represented.
        if any(not set(ids).intersection(outcome["evidence_ids"]) for outcome in unit["outcomes"]):
            raise ValueError("Node summary omitted an observed variant")
        results.append({"unit_id": uid, "function": prose(item["function"], 160),
                        "observed_effect": prose(item["observed_effect"], 400),
                        "applicability": prose(item["applicability"], 240),
                        "limitations": prose(item["limitations"], 240), "evidence_ids": ids})
    return {"node_id": payload["node_id"], "page_role": role, "functions": results}


class NodeFunctionMemory:
    """One versioned memory per node. Saving and lookup never call a model."""

    def __init__(self, config, state, client=None):
        self.config, self.state, self.client = config, state, client
        if client is not None and (not config.summary_model.strip()
                                   or getattr(client, "model", None) != config.summary_model.strip()):
            raise ValueError("Node memory must use the configured summary model")
        self.prompt = NODE_PROMPT_PATH.read_text(encoding="utf-8")
        self.cache = {}
        self.log_path = state.run_dir / "node_function_summaries.jsonl"

    def _revision(self, evidence):
        return digest([evidence, self.config.summary_model, self.prompt])

    def _packet(self, evidence, units):
        controls = {item["function_id"]: item for item in evidence["functions"]}
        selected = []
        for unit in units:
            item = deepcopy(unit)
            item["control"] = controls[item["function_id"]]["control"]
            # Keep at least one reference for EVERY distinct effect and failure.
            # If that set does not fit, fail this case rather than hide a branch.
            for outcome in item["outcomes"]:
                outcome["evidence_ids"] = outcome["evidence_ids"][-1:]
                outcome["target_state_ids"] = outcome["target_state_ids"][-2:]
            item["evidence_ids"] = [oid for o in item["outcomes"] for oid in o["evidence_ids"]]
            item["source_state_ids"] = item["source_state_ids"][-2:]
            selected.append(item)
        return {key: evidence[key] for key in ("task", "node_id", "page_id", "page_description")} | {
            "app_package": redact_evidence_text(evidence["app_package"]), "memory_scope": redact_evidence_text(evidence["memory_scope"]),
            "units": selected, "partial_node": len(units) < len(evidence["units"]),
            "descriptions_from_execution_model": True, "input_values_omitted": True,
            "business_success": "unknown",
        }

    def summarize(self, evidence: dict) -> dict:
        revision = self._revision(evidence)
        previous = self.cache.get(evidence["node_id"])
        if self.client is None or (previous and previous["revision"] == revision):
            return self.snapshot(evidence)
        cached = {"revision": revision, "summaries": {}, "roles": [], "failed_units": []}
        # Publish the empty revision before any call so failure cannot leave old prose.
        self.cache[evidence["node_id"]] = cached
        remaining = list(evidence["units"])
        while remaining:
            batch = [remaining.pop(0)]
            while remaining and len(batch) < 3:
                candidate = self._packet(evidence, batch + [remaining[0]])
                if len(json.dumps(candidate, ensure_ascii=False)) > self.config.summary_max_chars:
                    break
                batch.append(remaining.pop(0))
            event = {"node_id": evidence["node_id"], "evidence_revision": revision,
                     "model": self.config.summary_model, "unit_ids": [u["unit_id"] for u in batch],
                     "status": "failed"}
            try:
                payload = self._packet(evidence, batch)
                if (len(json.dumps(payload, ensure_ascii=False)) > self.config.summary_max_chars
                        or any(len(u["evidence_ids"]) > 16 for u in payload["units"])):
                    raise ValueError("Node evidence exceeds input/citation budget")
                response = self.client.summarize(self.prompt, payload)
                result = validate_node_summary(response, payload)
                cached["summaries"].update({item["unit_id"]: item for item in result["functions"]})
                cached["roles"].append({"page_role": result["page_role"], "unit_ids": event["unit_ids"]})
                event["status"] = "accepted"
            except Exception as exc:
                cached["failed_units"].extend(event["unit_ids"])
                event["error_type"] = type(exc).__name__
                print(f"[Node memory] 节点功能总结未采用（{type(exc).__name__}），保留 UI 操作证据")
            # No raw model response, input values or exception messages in this log.
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        return self.snapshot(evidence)

    def summarize_page(self, page_id):
        page_id = self.state.resolve_page_id(page_id)
        page = self.state.graph.pages.get(page_id)
        if page is not None:
            return self.summarize(live_node_evidence(self.state.graph, page))

    def snapshot(self, evidence):
        revision = self._revision(evidence)
        cached = self.cache.get(evidence["node_id"])
        cached = cached if cached and cached["revision"] == revision else None
        units = {unit["unit_id"]: unit for unit in evidence["units"]}
        result = {key: evidence[key] for key in ("schema_version", "node_id", "page_id", "coverage_complete", "business_success")}
        result.update({"evidence_revision": revision, "model": self.config.summary_model.strip() or None,
                       "advisory_only": True, "page_roles": deepcopy(cached["roles"]) if cached else [],
                       "functions": [], "summary_status": "disabled" if self.client is None else
                       "empty" if not units else "pending" if cached is None else
                       "partial" if cached["failed_units"] and cached["summaries"] else
                       "unavailable" if cached["failed_units"] else "accepted"})
        for function in evidence["functions"]:
            item = deepcopy(function)
            item["cases"] = [deepcopy(units[uid]) for uid in item.pop("case_ids")]
            for case in item["cases"]:
                summary = cached["summaries"].get(case["unit_id"]) if cached else None
                if summary is not None:
                    case["model_summary"] = deepcopy(summary)
            result["functions"].append(item)
        return result

    def snapshot_page(self, page):
        return self.snapshot(live_node_evidence(self.state.graph, page))

    def context(self, evidence, *, historical=False, max_chars=2200):
        memory = self.summarize(evidence) if historical else self.snapshot(evidence)
        result = {key: memory[key] for key in ("node_id", "page_id", "summary_status", "advisory_only")}
        result.update({"historical": historical, "functions": [], "truncated": False})
        for function in memory["functions"]:
            for case in function["cases"]:
                item = {"function_id": function["function_id"], "element_id": function["element_id"],
                        "control": function["control"], "unit_id": case["unit_id"],
                        "source_state": case["source_state"], "action": case["action"],
                        "conditions": case["conditions"],
                        "status": case["status"], "attempt_count": case["attempt_count"],
                        "outcome_counts": case["outcome_counts"]}
                if "model_summary" in case:
                    item["model_summary"] = case["model_summary"]
                result["functions"].append(item)
                if len(json.dumps(result, ensure_ascii=False)) > max_chars:
                    result["functions"].pop()
                    result["truncated"] = True
        return result
