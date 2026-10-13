"""Canonical recovery conditions, independent of page retrieval and coordinates.

Only categorical UI labels are case-insensitive. Editable text and entity
identities remain exact, and an unreadable value is never treated as empty.
"""

from copy import deepcopy
import re


def enum_key(key):
    return key in {"mode", "overlay", "tab", "filter", "sort_order"} or key.endswith(("_tab", "_mode", "_overlay")) or bool(
        re.fullmatch(r"(?:active|selected)_(?:tab|step|toggle|view|filter|category)", key))


def normalized_label(value):
    return "_".join(value.strip().casefold().split()) if isinstance(value, str) else value


def unknown_value(value):
    return value is None or (isinstance(value, str) and normalized_label(value) in {
        "unknown", "unobserved", "uncertain"})


def categorical_values(values):
    return {key: normalized_label(value) if enum_key(key) else value
            for key, value in values.items()}


def canonical_guard(guard):
    result = deepcopy(guard)
    for group in ("state", "values"):
        if isinstance(result.get(group), dict):
            result[group] = categorical_values(result[group])
    return result


def required_values(guard):
    """New profiles distinguish incidental observations from recovery conditions.

    Older guards have no contract, so all their recorded values remain required.
    """
    values = guard.get("values", {})
    keys = guard.get("recovery_value_keys")
    if not isinstance(keys, list):
        return values
    return {key: values.get(key, None) for key in keys}


def guard_matches(expected, actual):
    if not expected or not actual or actual.get("conflicts") or expected.get("conflicts"):
        return False
    expected, actual = canonical_guard(expected), canonical_guard(actual)
    for key in ("page_id", "activity", "module", "state", "context"):
        if expected.get(key) != actual.get(key):
            return False
    values = required_values(expected)
    if any(key not in actual.get("values", {}) or actual["values"][key] != value
           for key, value in values.items()):
        return False
    # An extra editable/identity value cannot be dismissed as an incidental
    # observation merely because the earlier analysis failed to describe it.
    if any(re.search(r"(^|_)(query|draft|item|entity|account|task_id|list_id)(_|$)", key)
           for key in actual.get("values", {}) if key not in expected.get("values", {})):
        return False
    # A legacy guard also protects against additional, previously absent values.
    if "recovery_value_keys" not in expected and values != actual.get("values", {}):
        return False
    maps = [expected.get("state", {}), expected.get("context", {}), values]
    if not any(maps) or any(unknown_value(value) for group in maps for value in group.values()):
        return (expected.get("surface") == actual.get("surface")
                and expected.get("state_image_id", expected.get("layout_id"))
                == actual.get("state_image_id", actual.get("layout_id")))
    return True


def needs_reobservation(expected, actual):
    """Retry only absent/unreadable required conditions, never a known mismatch."""
    if not actual:
        return False
    expected, actual = canonical_guard(expected), canonical_guard(actual)
    pairs = [(expected.get(group, {}), actual.get(group, {})) for group in ("state", "context")]
    pairs.append((required_values(expected), actual.get("values", {})))
    if any(key in observed and not unknown_value(value) and not unknown_value(observed[key])
           and observed[key] != value for required, observed in pairs for key, value in required.items()):
        return False
    return any(key not in observed or unknown_value(observed[key])
               for required, observed in pairs for key in required)


def guard_differences(expected, actual):
    """Explain the predicate's actual requirements, including unknown image checks."""
    expected, actual = canonical_guard(expected), canonical_guard(actual)
    differences = {key: {"expected": expected.get(key), "actual": actual.get(key)}
                   for key in ("page_id", "activity", "module", "state", "context", "conflicts")
                   if expected.get(key) != actual.get(key)}
    required = required_values(expected)
    observed = ({key: actual.get("values", {}).get(key) for key in required}
                if "recovery_value_keys" in expected else actual.get("values", {}))
    if required != observed:
        differences["values"] = {"expected": required, "actual": observed}
    missing = sorted(key for key in required if key not in actual.get("values", {}))
    if missing:
        differences["missing_required_value_keys"] = missing
    for key in ("surface", "state_image_id"):
        if expected.get(key) != actual.get(key):
            differences[key] = {"expected": expected.get(key), "actual": actual.get(key)}
    return differences
