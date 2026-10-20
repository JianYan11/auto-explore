"""Behavioral coverage for evidence and summary features retained during consolidation."""

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from harness.capabilities import AppCapabilityCatalog
from harness.config import ExplorerConfig
from harness.contracts import AppGraph, PageState, UIElement
from harness.evidence import build_node_evidence, sanitize_inventory
from harness.memory import GraphMemory
from harness.replica import AppContentProfile
from harness.summary import NodeFunctionMemory, validate_node_summary


class SummaryPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = ExplorerConfig(app_package="test.app", model="execution-model",
                                     summary_model="summary-model", output_dir=str(self.root),
                                     graph_memory=False)
        self.page = PageState("page", "", "hash", "test.app/Main", "list",
                              [UIElement("add", "button", "Add", "Create task", {}, True, "")],
                              [], [], [], 0, app_package="test.app", page_description="Task list")
        self.page.content_inventory = {"features": ["notes"], "screen_roles": ["list"],
                                       "entities": [{"kind": "note", "fields": ["title"],
                                                     "visibility": "personal"}]}
        self.graph = AppGraph("test.app", "Test", {"page": self.page}, [], 0,
                              root_page_id="page", run_id="run")
        self.state = SimpleNamespace(graph=self.graph, run_dir=self.root)

    def observation(self, oid, destination="detail", *, title_valid=True, input_text="secret title"):
        return {"schema_version": 1, "kind": "action", "observation_id": oid, "run_id": "run",
                "action_key": "add", "timestamp": 0,
                "app_package": "test.app", "memory_scope": "", "from_page": "page",
                "element_id": "add", "action": {"action_type": "tap", "target": "Create task",
                "element_type": "button", "element_text": "Add", "input_text": input_text},
                "source": {"description": "Task list", "state": {"title_valid": title_valid}},
                "conditions": {}, "outcome": "destination_observed", "to_page": destination,
                "destination_verified": True, "observation_valid": True, "execution_success": True,
                "operation_verified": True, "target": {"description": destination, "state": {}},
                "grounding": {"source_layout_id": "layout", "matches_current_layout": True}}

    def evidence(self, observations):
        return build_node_evidence(page_id="page", page=asdict(self.page), observations=observations,
                                   aliases={}, run_id="run", app_package="test.app", memory_scope="")

    def test_inventory_is_enum_only_and_deduplicated(self):
        raw = deepcopy(self.page.content_inventory)
        raw["features"] += ["notes", "private value"]
        raw["entities"][0].update(title="private title", fields=["title", "password", "private value"])
        clean = sanitize_inventory(raw)
        self.assertEqual(clean["features"], ["notes"])
        self.assertEqual(clean["entities"][0]["fields"], ["password", "title"])
        self.assertNotIn("private title", json.dumps(clean))
        self.assertEqual(sanitize_inventory(None), {"features": [], "screen_roles": [], "entities": []})

    def test_node_cases_keep_conditions_inputs_and_counterexamples_separate(self):
        observations = [self.observation("one"), self.observation("two", "unexpected"),
                        self.observation("three", title_valid=False),
                        self.observation("four", input_text="different private input")]
        before = deepcopy(observations)
        evidence = self.evidence(observations)
        self.assertEqual(len(evidence["units"]), 3)
        self.assertEqual(evidence["units"][0]["status"], "contested")
        self.assertEqual(len(evidence["units"][0]["outcomes"]), 2)
        self.assertNotIn("secret title", json.dumps(evidence))
        self.assertEqual(observations, before)
        self.assertFalse(evidence["coverage_complete"])

    def test_optional_node_model_summary_retains_each_observed_variant(self):
        evidence = self.evidence([self.observation("one"), self.observation("two", "unexpected")])
        client = Mock(model="summary-model", last_finish_reason="stop")
        def summarize(prompt, packet):
            return {"node_id": packet["node_id"], "page_role": "任务列表", "functions": [
                {"unit_id": unit["unit_id"], "function": "新增任务", "observed_effect": "观察到不同页面",
                 "applicability": "匹配来源状态", "limitations": "未验证持久化",
                 "evidence_ids": unit["evidence_ids"]} for unit in packet["units"]]}
        client.summarize.side_effect = summarize
        service = NodeFunctionMemory(self.config, self.state, client)
        result = service.summarize(evidence)
        self.assertEqual(result["summary_status"], "accepted")
        self.assertIn("model_summary", result["functions"][0]["cases"][0])
        response = summarize("", service._packet(evidence, evidence["units"]))
        response["functions"][0]["evidence_ids"] = ["one"]
        with self.assertRaisesRegex(ValueError, "omitted an observed variant"):
            validate_node_summary(response, service._packet(evidence, evidence["units"]))

    def catalog_client(self):
        client = Mock(model="summary-model", last_finish_reason="stop")
        def summarize(prompt, packet):
            ids = [item["evidence_id"] for item in packet["evidence"]]
            return {"capabilities": [{"capability_id": "", "key": "task.create", "name": "新增任务",
                    "description": "观察到任务入口或界面变化，尚未验证持久化", "preconditions": [],
                    "inputs": ["标题"], "variants": [], "next_checks": ["验证保存结果"], "evidence_ids": ids}]}
        client.summarize.side_effect = summarize
        return client

    def test_app_catalog_still_calls_model_publishes_and_does_not_mark_coverage(self):
        client = self.catalog_client()
        self.graph.observations = [self.observation("one")]
        service = AppCapabilityCatalog(self.config, self.state, client)
        service.update(final=True)
        result = service.snapshot()
        self.assertEqual(result["summary_status"], "accepted")
        self.assertTrue(client.summarize.called)
        self.assertEqual(result["capabilities"][0]["evidence_status"], "effect_observed")
        self.assertEqual(result["capabilities"][0]["business_success"], "unknown")
        self.assertEqual(self.graph.functional_coverage, {})
        self.assertTrue((self.root / "app_capabilities.md").is_file())
        surface = next(item for item in result["evidence"] if item["kind"] == "surface")
        self.assertEqual(surface["content_inventory"], self.page.content_inventory)

    def test_conflicting_target_is_retained_as_needs_review(self):
        self.graph.observations = [{**self.observation("one"),
                                    "evidence_review": {"status": "target_mismatch", "reason": "wrong target"}}]
        service = AppCapabilityCatalog(self.config, self.state, self.catalog_client())
        service.update(final=True)
        cap = service.snapshot()["capabilities"][0]
        self.assertEqual(cap["evidence_status"], "needs_review")

    def test_disabled_summary_keeps_evidence_without_creating_model_client(self):
        self.config.summary_model = ""
        with patch("openai.OpenAI", side_effect=AssertionError("Unexpected model client")):
            service = AppCapabilityCatalog(self.config, self.state)
            service.update(final=True)
        self.assertEqual(service.snapshot()["summary_status"], "disabled")
        self.assertTrue(service.snapshot()["evidence"])

    def test_summary_budget_keeps_pending_evidence_without_extra_calls(self):
        self.config.summary_budget_seconds = 0
        client = self.catalog_client()
        service = AppCapabilityCatalog(self.config, self.state, client)
        service.update(final=True)
        client.summarize.assert_not_called()
        self.assertTrue(service.snapshot()["pending_evidence_ids"])
        self.assertTrue(service.snapshot()["budget_exhausted"])

    def test_replica_exports_remain_synthetic_and_omit_credentials(self):
        profile = AppContentProfile(self.config, self.state)
        inventory = deepcopy(self.page.content_inventory)
        inventory["entities"][0].update(fields=["title", "password"], title="private original")
        profile.observe(inventory)
        profile.save()
        data = json.loads((self.root / "app_content_profile.json").read_text())
        entity = data["content_types"][0]
        self.assertEqual(entity["omitted_fields"], ["password"])
        self.assertNotIn("password", entity["synthetic_examples"][0]["values"])
        self.assertNotIn("private original", json.dumps(data))

    def test_optional_replica_model_advice_still_validates_enum_citations(self):
        client = Mock(model="summary-model")
        def summarize(prompt, packet):
            return {"app_categories": ["productivity"], "feature_priorities": [{"feature": "notes",
                    "priority": "core", "evidence_ids": [packet["evidence"][0]["evidence_id"]]}],
                    "replica_scenarios": [], "gaps": ["return_navigation"]}
        client.summarize.side_effect = summarize
        profile = AppContentProfile(self.config, self.state, client)
        profile.observe(self.page.content_inventory)
        self.assertEqual(profile.snapshot()["advice_status"], "accepted")
        bad = summarize("", {"evidence": list(profile.evidence.values())})
        bad["feature_priorities"][0]["evidence_ids"] = ["invented"]
        with self.assertRaisesRegex(ValueError, "Unrecognized content evidence"):
            profile._validate_advice(bad, list(profile.evidence.values()))

    def test_memory_delivers_bounded_node_facts_without_model_calls(self):
        # Large evidence must not evict the current page or exceed the prompt budget.
        self.graph.observations = [self.observation(str(i), input_text=f"private value {i}") for i in range(40)]
        self.config.memory_max_chars = 2500
        with patch("openai.OpenAI", side_effect=AssertionError("Unexpected model client")):
            memory = GraphMemory(self.config)
            context = memory.context(self.graph, current_page_id="page", purpose="action")
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), self.config.memory_max_chars)
        self.assertTrue(context["pages"])
        self.graph.observations = [self.observation("one")]
        self.config.memory_max_chars = 12000
        context = memory.context(self.graph, current_page_id="page", purpose="action")
        self.assertEqual(context["pages"][0]["node_evidence"]["units"][0]["evidence_ids"], ["one"])


if __name__ == "__main__":
    unittest.main()
