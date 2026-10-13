"""One incremental, evidence-linked App capability catalog, using the summary model.

The catalog is advisory. It never changes work queues, action coverage or graph
edges. Source observations remain immutable; saved model prose is not evidence.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import re
import time

from .evidence import evidence_quality
from .knowledge import digest
from .summary import ExperienceSummaryClient
from .summary_format import SummaryBudgetExceeded, summarize_validated, validate_text


PROMPT_PATH = Path(__file__).parent / "prompts" / "app_capabilities.md"


def _text(value, limit=160):
    return str(value or "")[:limit]


def _state(raw):
    """Retain functional categories; concrete input values stay in the raw trace."""
    result = {}
    if not isinstance(raw, dict):
        return result
    categories = {"unknown", "empty", "filled", "valid", "invalid", "set", "unset", "present", "missing"}
    for key, value in list(raw.items())[:18]:
        if not isinstance(key, str) or isinstance(value, (dict, list)):
            continue
        if re.search(r"password|token|secret|email|phone|address|title|name|query|description|input|text", key, re.I):
            if isinstance(value, bool):
                pass  # title_valid=False is a functional condition, not a filled title.
            elif isinstance(value, str) and value.casefold() in categories:
                value = value.casefold()
            else:
                value = "unknown" if value is None else "filled" if str(value) else "empty"
        result[key[:60]] = value if isinstance(value, (bool, int, float)) else _text(value or "unknown", 80)
    return result


def _key(value):
    return re.sub(r"[\s_-]+", ".", value.strip().casefold())


def _tokens(value):
    return set(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", str(value).casefold()))


class AppCapabilityCatalog:
    def __init__(self, config, state, client=None):
        self.config, self.state = config, state
        self.model = config.summary_model.strip()
        self.client = client if client is not None else ExperienceSummaryClient(config) if self.model else None
        if self.client is not None and getattr(self.client, "model", None) != self.model:
            raise ValueError("App capability catalog must use the configured summary model")
        self.prompt = PROMPT_PATH.read_text(encoding="utf-8")
        self.log_path = state.run_dir / "app_capability_events.jsonl"
        self.evidence, self.capabilities = {}, {}
        self.seen, self.processed, self.failed = set(), set(), set()
        self.fingerprints = {}
        self.pending = []
        self.revision = 0
        self.summary_seconds = 0.0
        self.budget_exhausted = False
        self.endpoint_unavailable = False
        self.metrics = {"model_batches": 0, "accepted_batches": 0, "failed_batches": 0,
                        "duplicate_observations": 0, "context_deliveries": 0}

    def _add(self, evidence_id, data, refs):
        if evidence_id in self.seen:
            return
        self.seen.add(evidence_id)
        signature = digest(data)
        existing = self.fingerprints.get(signature)
        if existing:
            self.evidence[existing]["source_refs"].append(refs)
            self.metrics["duplicate_observations"] += 1
            return
        self.fingerprints[signature] = evidence_id
        self.evidence[evidence_id] = {"evidence_id": evidence_id, **data, "source_refs": [refs]}
        self.pending.append(evidence_id)

    def ingest_graph(self, graph):
        """Accept a saved graph or a live projection. Ignore all previously generated prose."""
        if graph.get("app_package") != self.config.app_package or graph.get("memory_scope", "") != self.config.memory_scope:
            return
        run = graph.get("run_id", "")
        works = graph.get("work_items", {})
        pages = graph.get("pages", {})
        covered_pages = set()
        for wid, work in works.items():
            if work.get("status") == "merged":
                continue
            pid = work.get("page_id", "")
            page = pages.get(pid, {})
            covered_pages.add(pid)
            if self._belongs_to_app(page):
                self._surface(run, pid, page, work, wid)
        for pid, page in pages.items():
            if pid not in covered_pages and self._belongs_to_app(page):
                self._surface(run, pid, page, {}, "")
        reviews = graph.get("evidence_reviews", {})
        for obs in graph.get("inherited_observations", []) + graph.get("observations", []):
            if obs.get("kind") != "action" or obs.get("app_package") != self.config.app_package:
                continue
            if obs.get("memory_scope", "") != self.config.memory_scope:
                continue
            oid = obs.get("observation_id")
            if not oid or oid in self.seen:
                continue
            projected = {**obs, "evidence_review": reviews.get(oid, obs.get("evidence_review", {}))}
            quality = evidence_quality(projected)
            action = obs.get("action", {})
            scope = obs.get("functional_scope") or {}
            source, target = obs.get("source", {}), obs.get("target") or {}
            before, after = _state(source.get("state")), _state(target.get("state"))
            # Only executed, identified changes count as observed effects. Persistence is never inferred.
            effect = (quality["effect_level"] == "observed_ui"
                      and obs.get("outcome") in {"same_page_changed", "destination_observed"}
                      and quality["target_alignment"] != "conflict")
            data = {"kind": "action", "module": _text(scope.get("module") or source.get("description")),
                    "action": {"type": action.get("action_type"), "target": _text(action.get("target")),
                               "label": _text(action.get("element_text"), 80),
                               "family": _text(obs.get("action_semantics", {}).get("family")),
                               "input_supplied": bool(action.get("input_text"))},
                    "before": {"description": _text(source.get("description")), "state": before},
                    "after": {"description": _text(target.get("description")), "state": after},
                    "outcome": obs.get("outcome"), "purpose": obs.get("purpose", "exploration"),
                    "effect_observed": effect, "evidence_quality": quality,
                    "changed_fields": sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key)),
                    "workflow_id": obs.get("work_context", {}).get("workflow_id", ""),
                    "context_key": obs.get("work_context", {}).get("context_key", ""),
                    "trace": [{"action": step.get("action_type"), "target": _text(step.get("target_description"), 100),
                               "input_supplied": bool(step.get("input_text"))} for step in obs.get("execution_path", [])[-6:]],
                    "trace_truncated": len(obs.get("execution_path", [])) > 6,
                    "business_success": "unknown"}
            refs = {"run_id": obs.get("run_id", run), "observation_id": oid,
                    "from_page": obs.get("from_page"), "to_page": obs.get("to_page"),
                    "from_work_id": obs.get("from_work_id", ""), "to_work_id": obs.get("to_work_id", ""),
                    "from_state_id": obs.get("from_state_id", ""), "to_state_id": obs.get("to_state_id", ""),
                    "before_screenshot": obs.get("before_screenshot", ""), "after_screenshot": obs.get("after_screenshot", "")}
            self._add(oid, data, refs)
        # Untried surfaces from earlier runs are absent from inherited action logs.
        # Carry only their structured execution evidence, never saved capability prose.
        prior_catalog = graph.get("app_capabilities") or {}
        for record in prior_catalog.get("evidence", []) if isinstance(prior_catalog, dict) else []:
            if not isinstance(record, dict) or record.get("kind") != "surface" or not isinstance(record.get("evidence_id"), str):
                continue
            refs = record.get("source_refs")
            controls = record.get("controls")
            if not isinstance(refs, list) or not refs or not all(isinstance(ref, dict) for ref in refs) or not isinstance(controls, list):
                continue
            if not all(isinstance(control, dict) for control in controls):
                continue
            data = {"kind": "surface", "module": _text(record.get("module")), "description": _text(record.get("description")),
                    "state": _state(record.get("state")), "controls": [{"type": _text(e.get("type"), 40),
                    "label": _text(e.get("label"), 70), "description": _text(e.get("description"), 100)} for e in controls[:24]],
                    "controls_truncated": bool(record.get("controls_truncated")) or len(controls) > 24,
                    "action_families": [_text(x) for x in (record.get("action_families") or [])[:16] if isinstance(x, str)]}
            if record["evidence_id"] not in self.seen:
                self._add(record["evidence_id"], data, deepcopy(refs[0]))

    def _belongs_to_app(self, page):
        activity = str(page.get("activity") or "")
        package = page.get("app_package") or (activity.split("/", 1)[0] if "/" in activity else self.config.app_package)
        return package == self.config.app_package

    def _surface(self, run, pid, page, work, wid):
        elements = work.get("elements", page.get("elements", []))
        controls = [{"type": e.get("type", "unknown"), "label": _text(e.get("text"), 70),
                     "description": _text(e.get("description"), 100)} for e in elements[:24]]
        raw_state = work.get("normalized_state", page.get("semantic_state", {}))
        data = {"kind": "surface", "module": _text(work.get("module") or page.get("page_description")),
                "description": _text(page.get("page_description") or page.get("page_type")),
                "state": _state(raw_state), "controls": controls, "controls_truncated": len(elements) > 24,
                "action_families": sorted({a.get("action_family") for a in work.get("planned_actions", []) if a.get("action_family")})[:16]}
        # The evidence key includes the structural contents, so late controls are not lost.
        eid = "surface_" + digest([run, pid, wid, data])
        state_ids = work.get("observed_state_ids", list(page.get("states", {})))
        observed = page.get("states", {}).get(state_ids[-1], {}) if state_ids else {}
        self._add(eid, data, {"run_id": run, "page_id": pid, "work_id": wid,
                            "state_ids": state_ids,
                            "screenshot": observed.get("screenshot_path") or page.get("screenshot_path", "")})

    def sync(self):
        graph = self.state.graph
        self.ingest_graph({"app_package": graph.app_package, "memory_scope": graph.memory_scope, "run_id": graph.run_id,
                           "pages": {key: {"page_description": page.page_description, "page_type": page.page_type,
                                            "activity": page.activity,
                                            "semantic_state": page.semantic_state, "screenshot_path": page.screenshot_path,
                                            "states": {sid: {"screenshot_path": visual.screenshot_path} for sid, visual in page.states.items()},
                                            "elements": [asdict(element) for element in page.elements]} for key, page in graph.pages.items()},
                           "work_items": {key: asdict(work) for key, work in graph.work_items.items()},
                           "observations": graph.observations})

    def _packet(self, ids):
        focus = _tokens(" ".join(self.evidence[eid].get("module", "") for eid in ids))
        ordered = sorted(self.capabilities.values(), key=lambda cap: len(focus & _tokens(cap["name"] + cap["description"])), reverse=True)
        payload = {"task": "app_capability_catalog", "app_package": self.config.app_package,
                   "memory_scope": self.config.memory_scope, "catalog_revision": self.revision,
                   "existing_capabilities": [], "evidence": [], "partial_catalog": False,
                   "source": "execution_model_observations_not_independent_screenshot_review"}
        # Spend at most a quarter of the input on the identity index, retaining room for new evidence.
        for cap in ordered:
            item = {key: cap[key] for key in ("capability_id", "key", "name")}
            if len(payload["existing_capabilities"]) < 2:
                item.update(description=cap["description"][:160], preconditions=cap["preconditions"][:6], inputs=cap["inputs"][:6],
                            variants=[{key: v[key] for key in ("key", "name", "evidence_status")} for v in cap["variants"][:6]],
                            next_checks=cap["next_checks"][:2])
            payload["existing_capabilities"].append(item)
            if len(json.dumps(payload, ensure_ascii=False)) > self.config.summary_max_chars // 4:
                payload["existing_capabilities"][-1] = {key: cap[key] for key in ("capability_id", "key", "name")}
            if len(json.dumps(payload, ensure_ascii=False)) > self.config.summary_max_chars // 4:
                payload["existing_capabilities"].pop()
                payload["partial_catalog"] = True
                break
        for eid in ids:
            raw = self.evidence[eid]
            item = {key: value for key, value in raw.items() if key != "source_refs"}
            item["source_refs"] = [{key: value for key, value in raw["source_refs"][0].items() if "screenshot" not in key}]
            # Compact an unusually large surface while keeping labels and explicit truncation.
            if len(json.dumps(item, ensure_ascii=False)) > self.config.summary_max_chars // 2:
                item = deepcopy(item)
                if item["kind"] == "surface":
                    item["controls"] = [{"type": e["type"], "label": e["label"], "description": e["description"][:40]} for e in item["controls"]]
                else:
                    item["trace"] = item["trace"][-2:]
                    item["trace_truncated"] = True
                item["text_truncated"] = True
            payload["evidence"].append(item)
            if len(json.dumps(payload, ensure_ascii=False)) > self.config.summary_max_chars:
                payload["evidence"].pop()
                break
        return payload

    def _validate(self, response, payload):
        if not isinstance(response, dict) or set(response) != {"capabilities"}:
            raise ValueError("Expected only capabilities")
        items = response["capabilities"]
        if not isinstance(items, list) or len(items) > 6:
            raise ValueError("At most six capability updates per batch")
        evidence_ids = {item["evidence_id"] for item in payload["evidence"]}
        existing = {item["capability_id"]: item for item in payload["existing_capabilities"]}
        seen = set()

        def refs(value):
            if (not isinstance(value, list) or not 1 <= len(value) <= 12
                    or any(not isinstance(oid, str) or oid not in evidence_ids for oid in value)):
                raise ValueError("Citations must reference evidence supplied in this batch")

        for item in items:
            if not isinstance(item, dict) or set(item) != {"capability_id", "key", "name", "description", "preconditions", "inputs", "variants", "next_checks", "evidence_ids"}:
                raise ValueError("Unexpected capability fields")
            cid = item["capability_id"]
            if not isinstance(cid, str) or cid and cid not in existing:
                raise ValueError("Unknown capability identity")
            for key, limit in (("key", 80), ("name", 80), ("description", 400)):
                validate_text(item[key], limit, key)
            if not re.fullmatch(r"[a-z][a-z0-9_.-]*", item["key"]):
                raise ValueError("key must be a stable ASCII semantic identifier")
            if cid and _key(item["key"]) != existing[cid]["key"]:
                raise ValueError("Existing capability keys are immutable")
            identity = _key(item["key"])
            if identity in seen:
                raise ValueError("Duplicate capability update")
            seen.add(identity)
            for field, limit in (("preconditions", 6), ("inputs", 12), ("next_checks", 5)):
                if not isinstance(item[field], list) or len(item[field]) > limit:
                    raise ValueError(f"Invalid {field}")
                for value in item[field]:
                    validate_text(value, 160, field)
            refs(item["evidence_ids"])
            if not isinstance(item["variants"], list) or len(item["variants"]) > 8:
                raise ValueError("Invalid variants")
            variant_keys = set()
            for variant in item["variants"]:
                if not isinstance(variant, dict) or set(variant) != {"key", "name", "description", "evidence_ids"}:
                    raise ValueError("Invalid variant schema")
                for field, limit in (("key", 80), ("name", 80), ("description", 300)):
                    validate_text(variant[field], limit, f"variant.{field}")
                if _key(variant["key"]) in variant_keys:
                    raise ValueError("Duplicate variant")
                variant_keys.add(_key(variant["key"]))
                refs(variant["evidence_ids"])
                if not set(variant["evidence_ids"]).issubset(item["evidence_ids"]):
                    raise ValueError("Variant citations must belong to its capability")
        return response

    def _level(self, ids):
        evidence = [self.evidence[eid] for eid in ids]
        if any(e.get("evidence_quality", {}).get("target_alignment") == "conflict" for e in evidence):
            return "needs_review"
        return "effect_observed" if any(e.get("effect_observed") for e in evidence) else "entry_observed"

    def _merge(self, response):
        # Validation is complete before this method: a bad batch never partially changes the catalog.
        for update in response["capabilities"]:
            key = _key(update["key"])
            prior = next((cap for cap in self.capabilities.values() if cap["key"] == key or
                          cap["name"].strip().casefold() == update["name"].strip().casefold()), None)
            cid = update["capability_id"] or (prior["capability_id"] if prior else "cap_" + digest([self.config.app_package, key]))
            old = self.capabilities.get(cid, {})
            value = deepcopy(update)
            value.update(capability_id=cid, key=old.get("key", key), advisory_only=True,
                         interpretation_verified=False, business_success="unknown")
            value["evidence_ids"] = list(dict.fromkeys(old.get("evidence_ids", []) + update["evidence_ids"]))
            value["inputs"] = list(dict.fromkeys(old.get("inputs", []) + update["inputs"]))
            variants = {variant["key"]: deepcopy(variant) for variant in old.get("variants", [])}
            for variant in update["variants"]:
                vkey = _key(variant["key"])
                item = {**variant, "key": vkey, "evidence_ids": list(dict.fromkeys(
                    variants.get(vkey, {}).get("evidence_ids", []) + variant["evidence_ids"]))}
                item["evidence_status"] = self._level(item["evidence_ids"])
                variants[vkey] = item
            value["variants"] = list(variants.values())
            value["evidence_status"] = self._level(value["evidence_ids"])
            self.capabilities[cid] = value
        self.revision += 1

    def update(self, *, reason="round", final=False):
        self.sync()
        if self.client is None:
            return
        self.pending.sort(key=lambda eid: (
            not any(ref.get("run_id") == self.state.graph.run_id for ref in self.evidence[eid]["source_refs"]),
            self.evidence[eid]["kind"] != "action"))
        # Finalization shares the same cumulative request budget as exploration rounds.
        while self.pending:
            remaining = self.config.summary_budget_seconds - self.summary_seconds
            if remaining <= 0 or self.endpoint_unavailable:
                self.budget_exhausted = remaining <= 0
                self.save()
                break
            payload = self._packet(self.pending)
            ids = [item["evidence_id"] for item in payload["evidence"]]
            if not ids:
                ids = self.pending[:1]
            event = {"reason": reason, "model": self.model, "revision": self.revision,
                     "evidence_ids": ids, "status": "failed", "started_at": time.time()}
            started = time.monotonic()
            deferred = False
            try:
                if not payload["evidence"]:
                    raise ValueError("Single evidence exceeds input budget")
                self.metrics["model_batches"] += 1
                result = summarize_validated(self.client, self.prompt, payload,
                                             lambda response: self._validate(response, payload), event,
                                             deadline=started + min(remaining, self.config.summary_timeout))
                self._merge(result)
                self.processed.update(ids)
                self.metrics["accepted_batches"] += 1
                event.update(status="accepted", capability_ids=list(self.capabilities), revision=self.revision)
            except Exception as exc:
                deferred = isinstance(exc, (SummaryBudgetExceeded, TimeoutError)) or type(exc).__name__ in {
                    "APITimeoutError", "APIConnectionError", "AuthenticationError", "RateLimitError"}
                if deferred:
                    self.endpoint_unavailable = True
                    event["status"] = "deferred"
                else:
                    self.failed.update(ids)
                    self.metrics["failed_batches"] += 1
                event["error_type"] = type(exc).__name__
                print(f"[Capabilities] 功能总结未采用（{type(exc).__name__}），保留证据和已有目录")
            duration = time.monotonic() - started
            self.summary_seconds += duration
            self.state.graph.timings["summary_requests_seconds"] = self.summary_seconds
            self.budget_exhausted = self.summary_seconds >= self.config.summary_budget_seconds
            event["duration_seconds"] = duration
            if not deferred:
                self.pending = [eid for eid in self.pending if eid not in ids]
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.save()
            # An unavailable endpoint should not turn finalization into one timeout per queued batch.
            if not final or deferred:
                break

    def snapshot(self):
        current_ids = [eid for eid, item in self.evidence.items()
                       if any(ref.get("run_id") == self.state.graph.run_id for ref in item["source_refs"])]
        return {"schema_version": 1, "app_package": self.config.app_package, "memory_scope": self.config.memory_scope,
                "run_id": self.state.graph.run_id, "summary_model": self.model or None, "revision": self.revision,
                "summary_status": "disabled" if self.client is None else "partial" if (self.failed or self.endpoint_unavailable) and self.capabilities else
                                  "unavailable" if self.failed or self.endpoint_unavailable else "pending" if self.pending else "accepted" if self.capabilities else "empty",
                "coverage_complete": False, "business_success": "unknown", "advisory_only": True,
                "summary_budget_seconds": self.config.summary_budget_seconds,
                "summary_seconds": self.summary_seconds, "budget_exhausted": self.budget_exhausted,
                "endpoint_unavailable": self.endpoint_unavailable,
                "current_run_evidence_ids": current_ids,
                "historical_only_evidence_ids": [eid for eid in self.evidence if eid not in current_ids],
                "capabilities": deepcopy(list(self.capabilities.values())), "evidence": deepcopy(list(self.evidence.values())),
                "pending_evidence_ids": list(self.pending), "failed_evidence_ids": sorted(self.failed),
                "metrics": dict(self.metrics), "event_log": str(self.log_path)}

    def context(self, *, query="", page_id="", work_id="", max_chars=2200):
        tokens = _tokens(query)
        def score(cap):
            refs = [ref for eid in cap["evidence_ids"] for ref in self.evidence[eid]["source_refs"]]
            local = sum(ref.get("run_id") == self.state.graph.run_id and (
                (work_id and work_id in (ref.get("work_id"), ref.get("from_work_id"), ref.get("to_work_id"))) or
                (page_id and page_id in (ref.get("page_id"), ref.get("from_page"), ref.get("to_page")))) for ref in refs)
            return 10 * local + len(tokens & _tokens(cap["name"] + cap["description"] + " ".join(cap["inputs"])))
        result = {"revision": self.revision, "advisory_only": True, "capabilities": [], "truncated": False}
        for cap in sorted(self.capabilities.values(), key=score, reverse=True):
            if len(result["capabilities"]) >= 4:
                result["truncated"] = True
                break
            item = {key: cap[key] for key in ("capability_id", "name", "evidence_status")}
            item.update(description=cap["description"][:240], preconditions=cap["preconditions"][:3],
                        next_checks=cap["next_checks"][:2], evidence_ids=cap["evidence_ids"][-3:], business_success="unknown")
            result["capabilities"].append(item)
            if len(json.dumps(result, ensure_ascii=False)) > max_chars:
                result["capabilities"].pop()
                result["truncated"] = True
            else:
                self.metrics["context_deliveries"] += 1
        return result

    def save(self):
        data = self.snapshot()
        encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        levels = {"entry_observed": "已发现入口", "effect_observed": "已观察到界面效果", "needs_review": "证据存在冲突，待复核"}
        lines = [f"# App 核心功能目录：{self.config.app_package}", "",
                 f"总结模型：{self.model or '未配置'}；修订：{self.revision}；状态：{data['summary_status']}", "",
                 f"本次运行证据：{len(data['current_run_evidence_ids'])}；仅历史证据：{len(data['historical_only_evidence_ids'])}。目录包含历史经验，不能视为本轮覆盖率。", "",
                 "功能描述由 summary 模型根据探索证据归纳；界面效果不等于业务完成或数据已持久化。探索覆盖不完整。", ""]
        for cap in self.capabilities.values():
            lines.extend([f"## {cap['name']}", "", cap["description"], "",
                          f"证据状态：{levels[cap['evidence_status']]}", "",
                          "前置条件：" + "；".join(cap["preconditions"]), "",
                          "输入或可配置项：" + "、".join(cap["inputs"]), ""])
            for variant in cap["variants"]:
                lines.append(f"- {variant['name']}：{variant['description']}（{levels[variant['evidence_status']]}）")
            lines.extend(["", "待验证：" + "；".join(cap["next_checks"]), "",
                          "证据：" + ", ".join(cap["evidence_ids"]), ""])
        if not self.capabilities:
            lines.append("尚无被采用的模型功能总结；已采集的原始证据保留在 JSON 中。")
        lines.extend(["", f"待处理证据：{len(self.pending)}；失败证据：{len(self.failed)}。", ""])
        for root in {Path(self.config.output_dir), self.state.run_dir}:
            (root / "app_capabilities.json").write_text(encoded, encoding="utf-8")
            (root / "app_capabilities.md").write_text("\n".join(lines), encoding="utf-8")
