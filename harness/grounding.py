"""Bind coordinates to an exact observation, independently of page identity."""

from __future__ import annotations

from dataclasses import asdict, replace
from hashlib import sha256
from math import isfinite
from typing import Any

from .actions import ActionCodec
from .contracts import ExplorationAction, ScreenCapture


def layout_id(capture: ScreenCapture) -> str:
    # Deliberately conservative: perceptual/page hashes may hide reordered rows.
    # Even a clock/animation change invalidates this coordinate cache.
    payload = f"{capture.screen_size[0]}x{capture.screen_size[1]}:{capture.image_b64}"
    return "sha256:" + sha256(payload.encode("utf-8")).hexdigest()


def state_image_id(capture, top_ratio=0.07, bottom_ratio=0.05):
    """Exact app-content evidence for unknown state, excluding system chrome."""
    import base64
    from io import BytesIO
    from PIL import Image
    try:
        with Image.open(BytesIO(base64.b64decode(capture.image_b64))) as image:
            width, height = image.size
            pixels = image.convert("RGB").crop((0, int(height * top_ratio), width, int(height * (1 - bottom_ratio))))
            return sha256(str(pixels.size).encode() + pixels.tobytes()).hexdigest()
    except Exception:
        return layout_id(capture)


def with_source(action: ExplorationAction, capture: ScreenCapture, method: str) -> ExplorationAction:
    return replace(action, source_layout_id=layout_id(capture),
                   source_screen_size=capture.screen_size, grounding_method=method)


def matches_source(action: ExplorationAction, capture: ScreenCapture) -> bool:
    return (action.source_layout_id == layout_id(capture)
            and action.source_screen_size is not None
            and tuple(action.source_screen_size) == tuple(capture.screen_size))


def bind_location(action: ExplorationAction, raw: Any, capture: ScreenCapture,
                  method: str) -> ExplorationAction | None:
    """Use only fresh geometry; never let relocation rewrite the operation/target."""
    if isinstance(raw, ExplorationAction):
        raw = asdict(raw)
    if not isinstance(raw, dict):
        return None

    def valid_numbers(value: Any, length: int, keys: tuple[str, ...]) -> bool:
        if isinstance(value, dict):
            values = [value.get(key) for key in keys]
        elif isinstance(value, (list, tuple)) and len(value) == length:
            values = value
        else:
            return False
        return all(type(number) in (int, float) and isfinite(number) for number in values)

    if action.action_type == "swipe":
        start = raw.get("swipe_start") or raw.get("start")
        end = raw.get("swipe_end") or raw.get("end")
        if not all(valid_numbers(point, 2, ("x", "y")) for point in (start, end)):
            return None
    else:
        bounds = next((raw[key] for key in (
            "bbox_2d", "bounds_1000", "bounds_999", "bbox_999", "bounds", "box", "bbox",
        ) if raw.get(key)), None)
        keys = ("x", "y", "width", "height") if isinstance(bounds, dict) and "x" in bounds else ("x1", "y1", "x2", "y2")
        if not valid_numbers(bounds, 4, keys):
            return None
    try:
        located = ActionCodec.action_from_model(raw, step=ActionCodec.to_sequence_step(action))
        width, height = capture.screen_size
        if action.action_type == "swipe":
            points = (located.swipe_start, located.swipe_end)
            if not all(point and 0 <= point["x"] < width and 0 <= point["y"] < height for point in points):
                return None
            if located.swipe_start == located.swipe_end:
                return None
        else:
            box = located.bounds
            if not (0 <= box["x"] < width and 0 <= box["y"] < height
                    and box["width"] > 0 and box["height"] > 0
                    and box["x"] + box["width"] <= width
                    and box["y"] + box["height"] <= height):
                return None
    except (TypeError, ValueError, OverflowError, KeyError):
        return None
    return with_source(replace(action, bounds=located.bounds,
                               swipe_start=located.swipe_start, swipe_end=located.swipe_end),
                       capture, method)
