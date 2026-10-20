"""Program-owned evidence boundaries, separate from optional model interpretation."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import json
import re

from .knowledge import action_descriptor, digest


def evidence_quality(obs):
    review = obs.get("evidence_review") or {}
    grounding = obs.get("grounding") or {}
    mismatch = review.get("status") in {"target_mismatch", "recognition_conflict"}
    verified = bool(obs.get("destination_verified") and obs.get("observation_valid")
                    and obs.get("execution_success") and obs.get("operation_verified") is not False)
    located = bool(grounding.get("source_layout_id") and grounding.get("matches_current_layout") is True)
    return {
        "target_alignment": "conflict" if mismatch else "located_in_current_layout" if located else "unverified",
        "effect_level": "observed_ui" if verified else "unresolved",
        # A grounded tap and a recognized destination never prove persistence.
        "business_success": "unknown",
        "reusable": verified and located and not mismatch,
        "review_required": mismatch or not located,
        "review_reason": str(review.get("reason", ""))[:240],
    }


FEATURES = (
    "browse_catalog", "search", "filter", "sort", "view_details", "favorites",
    "cart", "checkout", "order_history", "reviews", "media_playback", "playlist",
    "messaging", "contacts", "social_feed", "compose", "calendar", "maps",
    "booking", "notifications", "account", "settings", "authentication", "payments",
    "files", "notes", "health_tracking", "learning", "weather",
)


ROLES = ("home", "list", "detail", "form", "search_results", "empty", "error",
         "loading", "dialog", "menu", "settings", "profile", "chat", "player", "map", "calendar")


KINDS = (
    "product", "book", "article", "video", "song", "album", "playlist", "podcast",
    "course", "lesson", "place", "restaurant", "event", "booking", "order",
    "review", "post", "comment", "contact", "conversation", "message", "notification",
    "account", "transaction", "file", "note", "health_record", "weather_report", "generic_item",
)


FIELDS = (
    "id", "title", "subtitle", "description", "category", "price", "rating", "count",
    "author", "artist", "duration", "cover", "name", "phone", "email", "address",
    "account_id", "order_id", "amount", "message", "timestamp", "status", "location",
    "latitude", "longitude", "health_note", "password", "verification_code", "balance",
    "quantity", "date", "size", "file_type", "is_favorite", "is_read",
)


VISIBILITIES = ("public", "personal", "unknown")


def _tokens(value: object, allowed: tuple | frozenset, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({item for item in value if isinstance(item, str) and item in allowed})[:limit]


def sanitize_inventory(raw: object) -> dict:
    """Drop all unknown text and values, including keys resembling allowed labels."""
    if not isinstance(raw, dict):
        return {"features": [], "screen_roles": [], "entities": []}
    entities = {}
    for item in raw.get("entities", [])[:8] if isinstance(raw.get("entities"), list) else []:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            continue
        visibility = item.get("visibility") if item.get("visibility") in VISIBILITIES else "unknown"
        fields = _tokens(item.get("fields"), FIELDS, len(FIELDS))
        entity = {"kind": item["kind"], "fields": fields, "visibility": visibility}
        entities[digest(entity)] = entity
    return {
        "features": _tokens(raw.get("features"), FEATURES, len(FEATURES)),
        "screen_roles": _tokens(raw.get("screen_roles"), ROLES, len(ROLES)),
        "entities": sorted(entities.values(), key=lambda item: (item["kind"], item["visibility"], item["fields"])),
    }


def observation_instructions() -> str:
    vocabulary = {"features": FEATURES, "screen_roles": ROLES, "entity_kinds": KINDS,
                  "field_names": FIELDS, "visibility": VISIBILITIES}
    return (
        "\nAlso return content_inventory with features, screen_roles and entities. "
        "Each entity has exactly kind, fields (field NAMES only) and visibility. "
        "Use only exact tokens from the vocabulary below. Describe STRUCTURE actually visible "
        "on this screen, including display-only content; this does not make it an action target. "
        "Never put field values, real names, titles, messages, identifiers, phone numbers, addresses, "
        "URLs, credentials, medical or financial details into content_inventory. No free-form text. "
        "For private screens retain only generic entity types and field names, visibility=personal. "
        "When uncertain use visibility=unknown; do not infer hidden features. "
        "Return empty arrays when no supported structure is visible. Example: "
        '{"features":["browse_catalog"],"screen_roles":["list"],"entities":'
        '[{"kind":"book","fields":["title","author"],"visibility":"public"}]}. '
        "Vocabulary: " + json.dumps(vocabulary, ensure_ascii=False)
    )


def redact_evidence_text(value: object, limit: int = 240) -> str:
    # Defense in depth for common identifiers. Free-form UI prose is NOT a
    # guaranteed anonymous export; the content-profile vocabulary serves that use.
    value = value if isinstance(value, str) else ""
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", value)
    value = re.sub(r"https?://\S+", "[url]", value)
    value = re.sub(r"(?<!\w)\+?\d[\d ()-]{6,}\d(?!\w)", "[identifier]", value)
    return value[:limit]


def _state(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {str(key)[:60]: ("[private]" if re.search(
                r"password|token|secret|phone|email|address|account|message|name|query|input|text", str(key), re.I
            ) else redact_evidence_text(str(value), 100))
            for key, value in list(raw.items())[:12]}


def _canonical(page_id: str, aliases: dict) -> str:
    seen = set()
    while page_id in aliases and page_id not in seen:
        seen.add(page_id)
        page_id = aliases[page_id]
    return page_id


def build_node_evidence(*, page_id: str, page: dict, observations: list[dict],
                        aliases: dict, run_id: str, app_package: str, memory_scope: str) -> dict:
    """Use recorded UI identity, source conditions and outcomes, never old prose.

    Each function owns cases separated by action, exact input and source state.
    Input values are represented by local variant labels in the model packet.
    """
    functions = {}
    units = {}
    control_aliases = page.get("control_aliases", {})
    control_aliases = control_aliases if isinstance(control_aliases, dict) else {}
    aliases = aliases if isinstance(aliases, dict) else {}
    input_variants = {}
    condition_variants = {}

    def function_for(element_id, control):
        fid = "ui_" + digest([page_id, element_id or control])
        if fid not in functions:
            functions[fid] = {"function_id": fid, "element_id": element_id,
                              "control": {key: redact_evidence_text(control.get(key)) for key in ("type", "text", "description")},
                              "case_ids": []}
        return fid

    raw_elements = page.get("elements")
    elements = {e["id"]: e for e in (raw_elements if isinstance(raw_elements, list) else [])
                if isinstance(e, dict) and isinstance(e.get("id"), str)}
    for eid, element in elements.items():
        function_for(eid, element)

    def identify(action, origin_page, element_id):
        element_id = element_id if isinstance(element_id, str) else None
        mapped = control_aliases.get(f"{origin_page}:{element_id}", element_id)
        if isinstance(mapped, str) and mapped in elements and (
                origin_page == page_id or f"{origin_page}:{element_id}" in control_aliases):
            return function_for(mapped, elements[mapped])
        # Legacy observations do not contain element_id. Only an unambiguous
        # semantic match may attach them to a visible control.
        matches = [e for e in elements.values() if e.get("type", "unknown") == action.get("element_type")
                   and e.get("text", "") == action.get("element_text", "")
                   and e.get("description", "") == action.get("target", "")]
        if len(matches) == 1:
            return function_for(matches[0]["id"], matches[0])
        return function_for(None, {"type": action.get("element_type", "unknown"),
                                  "text": action.get("element_text", ""), "description": action.get("target", "")})

    def get_case(fid, action, source_state, conditions):
        uid = "case_" + digest([fid, action, source_state, conditions])
        if uid not in units:
            raw_input = action.get("input_text", "")
            if raw_input:
                input_variants.setdefault(raw_input, f"input_{len(input_variants) + 1}")
            safe_action = {key: redact_evidence_text(value) for key, value in action.items() if key != "input_text"}
            safe_action["input_variant"] = input_variants.get(raw_input, "none")
            condition_variants.setdefault(digest(conditions), f"context_{len(condition_variants) + 1}")
            units[uid] = {"unit_id": uid, "function_id": fid, "action": safe_action,
                          "source_state": _state(source_state), "source_state_ids": [],
                          "conditions": {"variant": condition_variants[digest(conditions)],
                                         "arrival_input_count": len(conditions.get("arrival_inputs") or [])},
                          "status": "untried", "evidence_ids": [], "outcomes": [], "_observations": []}
            functions[fid]["case_ids"].append(uid)
        return units[uid]

    seen = set()
    recorded_actions = set()
    for obs in observations:
        if (obs.get("kind") != "action" or obs.get("run_id") != run_id
                or obs.get("app_package") != app_package or obs.get("memory_scope", "") != memory_scope
                or _canonical(obs.get("from_page", ""), aliases) != page_id):
            continue
        oid = obs.get("observation_id")
        if not isinstance(oid, str) or oid in seen:
            continue
        seen.add(oid)
        action = obs["action"]
        fid = identify(action, obs["from_page"], obs.get("element_id"))
        recorded_actions.add((fid, digest(action)))
        source_state = obs.get("source", {}).get("state", {})
        case = get_case(fid, action, source_state, obs.get("conditions", {}))
        case["evidence_ids"].append(oid)
        sid = obs.get("from_state_id", "")
        if sid and sid not in case["source_state_ids"]:
            case["source_state_ids"].append(sid)
        case["_observations"].append(obs)

    raw_actions = page.get("planned_actions")
    for action in raw_actions if isinstance(raw_actions, list) else []:
        if not isinstance(action, dict):
            continue
        descriptor = action_descriptor(action)
        fid = identify(descriptor, page_id, action.get("element_id"))
        if (fid, digest(descriptor)) not in recorded_actions:
            get_case(fid, descriptor, {}, {})
    for fid, function in functions.items():
        if not function["case_ids"]:
            get_case(fid, {}, {}, {})

    for case in units.values():
        attempts = case.pop("_observations")
        variants = {}
        for obs in attempts:
            target = obs.get("target") or {}
            valid = obs.get("destination_verified") is True
            # State/effect differences remain separate even if the target node is
            # the same. Recovery is recorded separately from forward arrival.
            key = digest([obs["outcome"], target if valid else None,
                          _canonical(obs.get("to_page", ""), aliases) if valid else "",
                          obs.get("recovery"), obs.get("error_stage")])
            outcome = variants.setdefault(key, {
                "outcome": obs["outcome"], "destination_verified": valid,
                "to_page": _canonical(obs.get("to_page", ""), aliases) if valid else "",
                "target_description": redact_evidence_text(target.get("description")) if valid else "",
                "target_state": _state(target.get("state")) if valid else {},
                "target_state_ids": [], "recovery": obs.get("recovery", ""),
                "error_stage": obs.get("error_stage", ""), "count": 0, "evidence_ids": [],
            })
            outcome["count"] += 1
            outcome["evidence_ids"].append(obs["observation_id"])
            if obs.get("to_state_id") and obs["to_state_id"] not in outcome["target_state_ids"]:
                outcome["target_state_ids"].append(obs["to_state_id"])
        effects = {digest([o["outcome"], o.get("target"), _canonical(o.get("to_page", ""), aliases)])
                   for o in attempts if o.get("destination_verified")}
        case["status"] = ("contested" if len(effects) > 1 else "observed" if effects
                          else "unresolved" if attempts else "untried")
        case["attempt_count"] = len(attempts)
        case["outcome_counts"] = dict(Counter(o["outcome"] for o in attempts))
        case["outcomes"] = list(variants.values())
    return {"schema_version": 1, "task": "node_function_memory", "node_id": f"{run_id}/{page_id}",
            "page_id": page_id, "app_package": app_package, "memory_scope": memory_scope,
            "page_description": redact_evidence_text(page.get("page_description") or page.get("page_type")),
            "functions": list(functions.values()), "units": list(units.values()),
            "coverage_complete": False, "business_success": "unknown"}


def live_node_evidence(graph, page) -> dict:
    return build_node_evidence(page_id=page.page_id, page=asdict(page), observations=graph.observations,
                               aliases=graph.node_aliases, run_id=graph.run_id,
                               app_package=graph.app_package, memory_scope=graph.memory_scope)
