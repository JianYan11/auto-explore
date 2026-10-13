"""Control identity, observed input values, and navigation depth."""

import re


def name(value):
    return "_".join(str(value or "").strip().casefold().split())


def control_identity(action):
    explicit = name(action.control_key)
    if explicit and explicit not in {"click", "input", "button", "tap", "field", "unknown"}:
        return explicit
    description = action.target_description.casefold()
    if action.action_type == "input" or action.element_type in {"input", "text_field", "edittext"}:
        if action.field_key:
            return "field:" + name(action.field_key)
        # Legacy observations often embed the current text and location in their description.
        description = re.split(r"\b(?:currently containing|containing|with placeholder|located)\b", description)[0]
        description = re.sub(r"['\"].*?['\"]", "", description).strip(" ,")
        if "search" in description:
            return "field:search_query"
        return "field:" + name(description or "unidentified_input")
    # A generic family such as 'click' must never merge distinct icon buttons.
    label = name(action.element_text)
    if label:
        return "label:" + label
    description = re.split(r",?\s+located\b", description)[0]
    description = re.sub(r"\b(?:at|in) the (?:top|bottom|left|right)[\w -]*.*$", "", description)
    return "control:" + name(description.strip(" ,") or action.element_id or "unidentified")


def overlay_id(analysis):
    state = (analysis.get("exploration") or {}).get("state") or {}
    # An inline card/banner does not capture interaction with the rest of the page.
    if state.get("overlay_blocking") is False:
        return ""
    value = state.get("overlay")
    if value and name(value) not in {"none", "closed", "false", "unknown"}:
        return str(value)
    for key, value in state.items():
        if (key.endswith("dialog_open") or key.endswith("menu_open")) and value not in (False, None, "", "closed", "none"):
            return key
    return "unidentified_overlay" if state.get("overlay_blocking") is True else ""




def input_value(action, profile, actions=()):
    identity = control_identity(action)
    matches = [a for a in actions if a.action_type == "input" and control_identity(a) == identity]
    if len(matches) == 1 and matches[0].field_value is not None:
        return matches[0].field_value
    values = profile.get("values", {})
    if action.field_key and action.field_key in values:
        return values[action.field_key] if isinstance(values[action.field_key], str) else None
    # Compatibility with existing draft analyses. Never substitute the last submitted query.
    if "search" in identity and "draft_query" in values:
        return values["draft_query"] if isinstance(values["draft_query"], str) else None
    label = name(action.element_text or action.target_description)
    if label in values:
        return values[label] if isinstance(values[label], str) else None
    return None


def route_depth(path):
    """Count loop-erased page transitions; typing and returning do not add nesting."""
    pages = []
    for step in path:
        source, target = step.source_guard.get("page_id"), step.target_guard.get("page_id")
        if not pages:
            pages.append(source or None)
        if source and source in pages:
            pages = pages[:pages.index(source) + 1]
        elif source and pages[-1] is None:
            pages[-1] = source
        if target and target in pages:
            pages = pages[:pages.index(target) + 1]
        elif not target or target != source:
            # An unrecognized intermediate Back remains conservative until an
            # observed ancestor proves the return and removes that unknown segment.
            pages.append(target or None)
    return max(0, len(pages) - 1)
