"""Template comparison contract and observed behavioral changes.

Missing metadata is absence of evidence, never evidence of an operation's effect.
Legacy same_page results remain usable without claiming UI-state equivalence.
"""

from .recovery import canonical_guard, unknown_value


def comparison_result(raw):
    def boolean(value, name, *, nullable=False):
        if isinstance(value, str):
            value = {"true": True, "yes": True, "1": True,
                     "false": False, "no": False, "0": False}.get(value.strip().lower(), value)
        if isinstance(value, bool) or (nullable and value is None):
            return value
        raise ValueError(f"VLM comparison must return boolean {name} or null")

    dual = "same_template" in raw
    template = boolean(raw.get("same_template" if dual else "same_page"), "same_template", nullable=dual)
    ui_state = boolean(raw.get("same_ui_state"), "same_ui_state", nullable=True) if dual else None
    if template is False and ui_state is True:
        raise ValueError("Different templates cannot have the same UI state")
    if template is False:
        ui_state = False
    states = {}
    for name in ("existing_state", "new_state"):
        value = raw.get(name, {})
        if not isinstance(value, dict):
            raise ValueError(f"{name} must be a flat object")
        if any(not isinstance(k, str) or isinstance(v, (dict, list)) for k, v in value.items()):
            raise ValueError(f"{name} must contain scalar state values")
        states[name] = value
    changes = raw.get("state_changes", [])
    if not isinstance(changes, list) or any(not isinstance(change, dict) for change in changes):
        raise ValueError("state_changes must be a list of objects")
    return {"identity_contract": "template_state_v1" if dual else "legacy_page",
            "same_template": template, "same_ui_state": ui_state,
            # Compatibility alias for callers that consume a template-match boolean.
            "same_page": template is True, "state_changes": changes, **states}


def behavioral_changes(before, after):
    """Known overlapping conditions changed; key additions/omissions are not effects."""
    if not before or not after:
        return {}
    before, after = canonical_guard(before), canonical_guard(after)
    if (before.get("page_id") == after.get("page_id") and before.get("state_image_id")
            and before["state_image_id"] == after.get("state_image_id")):
        return {}  # Identical app pixels cannot establish a new visible effect from metadata alone.
    changes = {}
    if before.get("page_id") and after.get("page_id") and before["page_id"] != after["page_id"]:
        changes["page_id"] = {"before": before["page_id"], "after": after["page_id"]}
    for group in ("state", "context", "values"):
        first, second = before.get(group, {}), after.get(group, {})
        keys = set(first) & set(second)
        if group == "values":
            # List windows/result counts are data evidence, not functional effects.
            keys -= {"first_visible_item", "last_visible_item", "total_results", "title"} - set(
                before.get("input_fields", {})) - set(after.get("input_fields", {}))
        for key in sorted(keys):
            if not unknown_value(first[key]) and not unknown_value(second[key]) and first[key] != second[key]:
                changes[f"{group}.{key}"] = {"before": first[key], "after": second[key]}
    return changes


def compatible_behavior(expected, actual):
    """Allow advisory drift after visual verification, but preserve known behavioral roles."""
    if not expected or not actual or expected.get("conflicts") or actual.get("conflicts"):
        return False
    expected, actual = canonical_guard(expected), canonical_guard(actual)
    if any(expected.get(key) != actual.get(key) for key in ("page_id", "module")):
        return False
    for group in ("state", "context"):
        first, second = expected.get(group, {}), actual.get(group, {})
        for key in set(first) | set(second):
            if key in {"first_visible_item", "last_visible_item", "total_results"}:
                continue
            # Local section/overlay/mode and entity/caller identity cannot be merged
            # just because a template or usable-arrival verifier accepted an image.
            if group == "state" or any(part in key for part in ("item", "entity", "account", "caller", "draft", "task_id", "list_id")):
                if key not in first or key not in second or unknown_value(first[key]) or unknown_value(second[key]):
                    return False
                if first[key] != second[key]:
                    return False
    return True
