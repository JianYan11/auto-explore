"""Optional structural profile and synthetic replica fixtures.

This separate product is not invoked by cloud exploration. Captured field values
never enter its enum-only exports. Core observation sanitization lives in evidence.
"""
from __future__ import annotations

import json
from pathlib import Path

from .evidence import sanitize_inventory
from .knowledge import digest


PERSONAL_FIELDS = frozenset({
    "name", "phone", "email", "address", "account_id", "order_id", "amount", "message",
    "timestamp", "location", "latitude", "longitude", "health_note", "balance",
})


OMITTED_FIELDS = frozenset({"password", "verification_code"})


CATEGORIES = ("shopping", "reading", "news", "music", "video", "social", "messaging",
              "travel", "food", "productivity", "finance", "health", "education", "weather", "other")


SCENARIOS = {
    "browse_to_detail": {"browse_catalog", "view_details"},
    "search_and_filter": {"search", "filter"},
    "save_favorite": {"favorites"},
    "cart_and_order": {"cart", "order_history"},
    "play_media": {"media_playback"},
    "read_conversation": {"messaging"},
    "view_booking": {"booking"},
    "edit_settings": {"settings"},
    "read_content": {"view_details"},
    "empty_and_error_states": set(),
}


GAPS = ("detail_fields", "pagination", "empty_state", "error_state", "loading_state",
        "search_results", "filter_options", "relationships", "authentication_states", "return_navigation")


PROMPT_PATH = Path(__file__).parent / "prompts" / "app_content_profile.md"


def synthetic_value(kind: str, field: str, index: int):
    """Fixtures depend only on schema tokens and row index, never captured values."""
    values = {
        "id": f"demo_{kind}_{index:02d}", "title": f"Demo {kind.replace('_', ' ').title()} {index:02d}",
        "subtitle": "A fictional example", "description": "Synthetic content for a replica environment.",
        "category": "Demo collection", "price": 19.5 + index, "rating": 4.2,
        "count": 10 + index, "author": f"Fictional Author {index:02d}",
        "artist": f"Fictional Artist {index:02d}", "duration": 180 + index * 10,
        "cover": f"assets/demo/{kind}-{index:02d}.png", "name": f"Demo Person {index:02d}",
        "phone": "00000000000", "email": f"demo{index:02d}@example.invalid",
        "address": f"Fictional District, Demo Street, Unit {index:02d}",
        "account_id": f"demo_account_{index:02d}", "order_id": f"DEMO-ORDER-{index:02d}",
        "amount": 10.0 + index, "message": "This is a fictional demonstration message.",
        "timestamp": 0, "status": "demo_pending" if index == 1 else "demo_complete",
        "location": "Fictional Demo Park", "latitude": 0.0, "longitude": 0.0,
        "health_note": "Synthetic record; no real person's health information.", "balance": 100.0,
        "quantity": index, "date": "2000-01-01", "size": 1024 * index,
        "file_type": "text/plain", "is_favorite": index == 2, "is_read": index == 2,
    }
    return values[field]


class AppContentProfile:
    def __init__(self, config, state, summary_client=None, inherited: list[dict] | None = None):
        self.config, self.state, self.client = config, state, summary_client
        self.evidence: dict[str, dict] = {}
        self.model_advice = None
        self.revision = ""
        self.attempted_revision = ""
        self.prompt = PROMPT_PATH.read_text(encoding="utf-8")
        self.log_path = state.run_dir / "content_profile_events.jsonl"
        for inventory in inherited or []:
            self._add(inventory, historical=True)

    def _add(self, raw: object, *, historical: bool = False) -> bool:
        inventory = sanitize_inventory(raw)
        if not any(inventory.values()):
            return False
        evidence_id = "content_" + digest(inventory)
        if evidence_id in self.evidence:
            if not historical:
                self.evidence[evidence_id]["observed_this_run"] = True
            return False
        self.evidence[evidence_id] = {"evidence_id": evidence_id, "inventory": inventory,
                                      "observed_this_run": not historical}
        self.revision = digest([self.prompt, sorted(self.evidence)])
        self.model_advice = None
        return True

    def observe(self, inventory: object) -> None:
        self._add(inventory)
        if self.evidence:
            self._summarize()

    def _validate_advice(self, response: object, submitted: list[dict]) -> dict:
        if not isinstance(response, dict) or set(response) != {"app_categories", "feature_priorities", "replica_scenarios", "gaps"}:
            raise ValueError("Invalid profile schema")
        categories = response["app_categories"]
        gaps = response["gaps"]
        if (not isinstance(categories, list) or not 1 <= len(categories) <= 4
                or any(not isinstance(item, str) or item not in CATEGORIES for item in categories)
                or not isinstance(gaps, list) or len(gaps) > 8
                or any(not isinstance(item, str) or item not in GAPS for item in gaps)):
            raise ValueError("Invalid profile vocabulary")
        by_id = {item["evidence_id"]: item["inventory"] for item in submitted}
        for key, limit in (("feature_priorities", 8), ("replica_scenarios", 4)):
            if not isinstance(response[key], list) or len(response[key]) > limit:
                raise ValueError("Too many profile recommendations")
            for item in response[key]:
                expected = ({"feature", "priority", "evidence_ids"} if key == "feature_priorities"
                            else {"template", "entity_kind", "evidence_ids"})
                if not isinstance(item, dict) or set(item) != expected:
                    raise ValueError("Unexpected profile fields")
                ids = item["evidence_ids"]
                if (not isinstance(ids, list) or not 1 <= len(ids) <= 4
                        or any(not isinstance(oid, str) or oid not in by_id for oid in ids)):
                    raise ValueError("Unrecognized content evidence")
                features = {feature for oid in ids for feature in by_id[oid]["features"]}
                kinds = {entity["kind"] for oid in ids for entity in by_id[oid]["entities"]}
                if key == "feature_priorities":
                    if (not isinstance(item["feature"], str) or item["feature"] not in features
                            or item["priority"] not in ("core", "supporting")):
                        raise ValueError("Unsupported feature")
                elif (not isinstance(item["template"], str) or item["template"] not in SCENARIOS
                      or not isinstance(item["entity_kind"], str) or item["entity_kind"] not in kinds
                      or not SCENARIOS[item["template"]].issubset(features)):
                    raise ValueError("Unsupported scenario")
        # The entire accepted response consists of allowlisted tokens and generated IDs.
        return json.loads(json.dumps(response))

    def _summarize(self) -> None:
        if self.client is None or self.revision == self.attempted_revision:
            return
        self.attempted_revision = self.revision
        # Send only sanitized structural tokens. Never forward source screenshots/values.
        submitted = []
        payload = {"evidence": submitted, "partial_evidence": False,
                   "allowed_categories": CATEGORIES, "allowed_scenarios": SCENARIOS,
                   "allowed_gaps": GAPS}
        # Sets are used internally for prerequisite checks; send lists to the API.
        payload["allowed_scenarios"] = {key: sorted(value) for key, value in SCENARIOS.items()}
        event = {"revision": self.revision, "status": "failed"}
        try:
            # Cover diverse structures before repeated schemas. Keep older main
            # features eligible even after many new observations; stay bounded.
            candidates = list(reversed(self.evidence.values()))
            covered = set()
            def coverage(item):
                inventory = item["inventory"]
                return ({"feature:" + feature for feature in inventory["features"]}
                        | {"role:" + role for role in inventory["screen_roles"]}
                        | {"kind:" + entity["kind"] for entity in inventory["entities"]}
                        | {entity["kind"] + ":" + field for entity in inventory["entities"]
                           for field in entity["fields"]})
            while candidates and len(submitted) < 20:
                item = max(candidates, key=lambda item: len(coverage(item) - covered))
                candidates.remove(item)
                submitted.append(item)
                if len(json.dumps(payload, ensure_ascii=False)) > self.config.summary_max_chars:
                    submitted.pop()
                else:
                    covered.update(coverage(item))
            if not submitted:
                raise ValueError("Profile evidence exceeds input budget")
            payload["partial_evidence"] = len(submitted) < len(self.evidence)
            response = self.client.summarize(self.prompt, payload)
            self.model_advice = self._validate_advice(response, submitted)
            event["status"] = "accepted"
        except Exception as exc:
            # Errors/model responses may contain personal text: log only the exception class.
            event["error_type"] = type(exc).__name__
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

    def snapshot(self) -> dict:
        features, roles, types = set(), set(), {}
        for evidence in self.evidence.values():
            inventory = evidence["inventory"]
            features.update(inventory["features"])
            roles.update(inventory["screen_roles"])
            for item in inventory["entities"]:
                entity = types.setdefault(item["kind"], {"fields": set(), "visibility": set(), "evidence_ids": []})
                entity["fields"].update(item["fields"])
                entity["visibility"].add(item["visibility"])
                entity["evidence_ids"].append(evidence["evidence_id"])
        entities = []
        for kind, entity in sorted(types.items()):
            fields = sorted(entity["fields"])
            entities.append({
                "kind": kind, "fields": fields, "visibility": sorted(entity["visibility"]),
                "evidence_ids": sorted(set(entity["evidence_ids"])),
                "sensitive_fields": [field for field in fields if field in PERSONAL_FIELDS | OMITTED_FIELDS],
                "omitted_fields": [field for field in fields if field in OMITTED_FIELDS],
                "synthetic_examples": [
                    {"synthetic": True, "values": {field: synthetic_value(kind, field, index)
                                                    for field in fields if field not in OMITTED_FIELDS}}
                    for index in (1, 2)
                ],
            })
        return {
            "schema_version": 1, "features": sorted(features), "screen_roles": sorted(roles),
            "content_types": entities, "evidence": list(self.evidence.values()),
            "model_advice": self.model_advice, "revision": self.revision,
            "advice_status": ("accepted" if self.model_advice else "disabled" if self.client is None
                              else "unavailable" if self.attempted_revision else "pending"),
            "evidence_origin": "execution_model_screen_analysis_not_independently_verified",
            "coverage_complete": False,
            "privacy": {"captured_values_exported": False, "examples_are_synthetic": True,
                        "identity_mapping_retained": False, "raw_screenshots_included": False},
        }

    def save(self) -> None:
        data = self.snapshot()
        output = Path(self.config.output_dir)
        (output / "app_content_profile.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["# App content profile", "", "Incremental observed structure and fictional replica fixtures.", "",
                 "All example values are generated from schema tokens, not copied or mapped from real records.", "",
                 "Structure is extracted by the execution model, not independently verified. Coverage is incomplete.", "",
                 "## Observed features", "", ", ".join(data["features"]) or "No supported observations yet.", "",
                 "## Observed screen roles", "", ", ".join(data["screen_roles"]) or "Unknown.", ""]
        for entity in data["content_types"]:
            lines.extend([f"## {entity['kind']}", "", "Fields: " + ", ".join(entity["fields"]), "",
                          "Visibility: " + ", ".join(entity["visibility"]), "",
                          "Evidence: " + ", ".join(entity["evidence_ids"]), "",
                          "Synthetic examples (not observed App content):", "", "```json",
                          json.dumps(entity["synthetic_examples"], ensure_ascii=False, indent=2), "```", ""])
        lines.extend(["## Suggested replica scenarios", "", "These are suggestions, not verified navigation paths.", "",
                      "Independent model advice: " + data["advice_status"], "",
                      "```json", json.dumps(data["model_advice"], ensure_ascii=False, indent=2), "```", "",
                      "Demo contact details are non-operational; asset paths are placeholders. Adapt fixture statuses to the replica schema.",
                      "The original exploration screenshots and graph are separate debugging artifacts and may contain personal information."])
        (output / "app_content_profile.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
