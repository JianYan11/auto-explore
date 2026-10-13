"""External tools used by the harness: VLM requests and ADB device control."""

from __future__ import annotations

import base64
import json
import math
import re
import shlex
import subprocess
import time
from io import BytesIO
from typing import Any, Dict, List, Optional, Set, Tuple

from PIL import Image

from .config import (
    QWEN_COORDINATE_SCALE,
    QWEN_MAX_PIXELS,
    QWEN_MIN_PIXELS,
    QWEN_RESIZE_FACTOR,
    ExplorerConfig,
)
from .context import HASH_PROMPT, PAGE_DESCRIPTION_PROMPT, ExplorationContextBuilder
from .contracts import ActionSequenceStep, ExplorationAction, InputFieldObservation, ScreenCapture
from .visual import perceptual_hash_png


COORDINATE_BOUNDS_KEYS = (
    "bbox_2d",
    "bounds_1000",
    "bounds_999",
    "bbox_999",
    "bounds",
    "box",
    "bbox",
    "start_box",
)
INPUT_CLEAR_DELETE_MARGIN = 8
COORDINATE_POINT_KEYS = (
    "coordinate",
    "point",
    "tap_point",
    "point_1000",
    "tap_point_1000",
    "point_999",
    "tap_point_999",
    "swipe_start",
    "swipe_end",
    "start",
    "end",
)


def _round_by_factor(number: float, factor: int) -> int:
    return round(number / factor) * factor


def _ceil_by_factor(number: float, factor: int) -> int:
    return math.ceil(number / factor) * factor


def _floor_by_factor(number: float, factor: int) -> int:
    return math.floor(number / factor) * factor


def _smart_resize(
    height: int,
    width: int,
    *,
    factor: int,
    min_pixels: int,
    max_pixels: int,
    max_image_side: int,
) -> Tuple[int, int]:
    if height < 2 or width < 2:
        raise ValueError(f"Image dimensions are too small: {width}x{height}")
    if max(height, width) / min(height, width) > 200:
        raise ValueError(f"Image aspect ratio is too large: {width}x{height}")
    if max_image_side < factor:
        raise ValueError("max_image_side must be greater than or equal to factor")

    side_scale = min(1.0, max_image_side / float(max(height, width)))
    target_height = height * side_scale
    target_width = width * side_scale
    resized_height = _round_by_factor(target_height, factor)
    resized_width = _round_by_factor(target_width, factor)
    resized_pixels = resized_height * resized_width

    if resized_pixels > max_pixels:
        beta = math.sqrt(resized_pixels / max_pixels)
        resized_height = _floor_by_factor(resized_height / beta, factor)
        resized_width = _floor_by_factor(resized_width / beta, factor)
    elif resized_pixels < min_pixels:
        beta = math.sqrt(min_pixels / max(1, resized_pixels))
        resized_height = _ceil_by_factor(resized_height * beta, factor)
        resized_width = _ceil_by_factor(resized_width * beta, factor)

    if max(resized_height, resized_width) > max_image_side:
        side_scale = max_image_side / float(max(resized_height, resized_width))
        resized_height = _floor_by_factor(resized_height * side_scale, factor)
        resized_width = _floor_by_factor(resized_width * side_scale, factor)

    resized_height = max(factor, min(max_image_side, resized_height))
    resized_width = max(factor, min(max_image_side, resized_width))
    return resized_height, resized_width


class VLMAnalyzer:
    """OpenAI-compatible visual analysis tool."""

    def __init__(
        self,
        config: ExplorerConfig,
        context_builder: Optional[ExplorationContextBuilder] = None,
    ):
        if not config.vlm_api_key:
            raise ValueError(
                "未设置模型 API Key。请设置 QWEN_API_KEY / MODELSCOPE_API_KEY，"
                "或通过 --api-key 传入。"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise SystemExit("缺少依赖 openai，请先运行：pip install openai pillow") from exc

        self.config = config
        self.client = OpenAI(
            base_url=config.vlm_base_url,
            api_key=config.vlm_api_key,
            timeout=config.vlm_timeout,
        )
        self.model = config.model
        self.max_image_side = config.vlm_max_image_side
        self.context_builder = context_builder or ExplorationContextBuilder()
        self.last_comparison: Dict[str, Any] = {}
        self.embedding_client = None

    def audit_graph_nodes(self, graph_text: str) -> Dict[str, Any]:
        return self._request_json(
            self.context_builder.graph_merge_audit_prompt(graph_text), [], max_tokens=4096,
        )

    def select_exploration_target(self, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self._request_json(
            self.context_builder.exploration_target_prompt(
                json.dumps(candidates, ensure_ascii=False)
            ), [], max_tokens=512,
        )

    def describe_screen(self, screenshot_b64: str, screen_size: Tuple[int, int]) -> Dict[str, Any]:
        image_b64, _ = self._prepare_image_for_model(screenshot_b64, screen_size)
        return self._request_json(
            PAGE_DESCRIPTION_PROMPT, [("current screen", image_b64)], max_tokens=1024,
        )

    def embed_description(self, description: str) -> List[float]:
        if self.embedding_client is None:
            from openai import OpenAI

            self.embedding_client = OpenAI(
                base_url=self.config.embedding_base_url or self.config.vlm_base_url,
                api_key=self.config.embedding_api_key or self.config.vlm_api_key,
                timeout=self.config.embedding_timeout,
                max_retries=1,
            )
        result = self.embedding_client.embeddings.create(
            model=self.config.embedding_model, input=description,
        )
        return result.data[0].embedding

    def analyze_screen_with_memory(
        self, screenshot_b64: str, screen_size: Tuple[int, int], memory: dict,
    ) -> Dict[str, Any]:
        return self.analyze_screen(screenshot_b64, screen_size, memory=memory)

    def analyze_screen_with_history(
        self, screenshot_b64: str, screen_size: Tuple[int, int], *,
        reference: ScreenCapture, history: dict, memory: Optional[dict] = None,
    ) -> Dict[str, Any]:
        return self.analyze_screen(screenshot_b64, screen_size, memory=memory,
                                   reference=reference, history=history)

    def choose_next_action(
        self, screenshot_b64: str, screen_size: Tuple[int, int],
        candidates: List[Dict[str, Any]], memory: dict,
    ) -> Dict[str, Any]:
        image_b64, sent_size = self._prepare_image_for_model(screenshot_b64, screen_size)
        result = self._request_json(
            self.context_builder.next_action_prompt(candidates, memory, *sent_size),
            [("current screen", image_b64)], max_tokens=1024,
        )
        self._map_analysis_coordinates_to_original(
            result, original_size=screen_size, sent_size=sent_size,
        )
        return result

    def analyze_screen(self, screenshot_b64: str, screen_size: Tuple[int, int],
                       *, memory: Optional[dict] = None, reference: Optional[ScreenCapture] = None,
                       history: Optional[dict] = None) -> Dict[str, Any]:
        image_b64, sent_size = self._prepare_image_for_model(screenshot_b64, screen_size)
        prompt = (self.context_builder.screen_analysis_prompt(*sent_size) if memory is None
                  else self.context_builder.screen_analysis_prompt(*sent_size, memory=memory))
        try:
            images = [("current screen", image_b64)]
            if reference is not None and history is not None:
                reference_image, _ = self._prepare_image_for_model(reference.image_b64, reference.screen_size)
                images = [("HISTORICAL REFERENCE (previous UI; not executable)", reference_image),
                          ("CURRENT SCREEN (all output coordinates refer to this image)", image_b64)]
                prompt = self.context_builder.revisit_analysis_prompt(*sent_size, history, memory)
            analysis = self._request_json(prompt, images)
            self._map_analysis_coordinates_to_original(
                analysis,
                original_size=screen_size,
                sent_size=sent_size,
            )
            return analysis
        except Exception as exc:
            print(f"VLM screen analysis failed: {exc}")
            return {
                "analysis_status": "failed", "analysis_error": f"{type(exc).__name__}: {exc}",
                "elements": [],
                "page_type": "unknown",
                "recommended_actions": [],
                "actions": [],
            }

    def compare_screens(
        self,
        candidate_b64: str,
        candidate_size: Tuple[int, int],
        existing_b64: str,
        existing_size: Tuple[int, int],
    ) -> Tuple[bool, str]:
        return self.compare_screens_with_description(
            candidate_b64,
            candidate_size,
            existing_b64,
            existing_size,
            existing_description="",
        )

    def compare_screens_with_description(
        self,
        candidate_b64: str,
        candidate_size: Tuple[int, int],
        existing_b64: str,
        existing_size: Tuple[int, int],
        *,
        existing_description: str,
    ) -> Tuple[bool, str]:
        candidate_image, _ = self._prepare_image_for_model(candidate_b64, candidate_size)
        existing_image, _ = self._prepare_image_for_model(existing_b64, existing_size)
        attempt_records: List[Dict[str, Any]] = []
        max_attempts = max(1, self.config.vlm_compare_max_attempts)
        for attempt in range(1, max_attempts + 1):
            try:
                result = self._request_json(
                    HASH_PROMPT.format(existing_description=existing_description),
                    [("existing screen", existing_image), ("new candidate screen", candidate_image)],
                    max_tokens=512,
                )
                from .identity import comparison_result
                identity = comparison_result(result)
                same_page = identity["same_template"] is True
                reason = str(result.get("reason") or "")
                refined_existing = str(result.get("existing_description") or "").strip()
                refined_new = str(result.get("new_description") or "").strip()
                attempt_records.append(
                    {
                        "attempt": attempt,
                        "status": "success",
                        "same_page": bool(same_page),
                        **identity,
                        "reason": reason,
                        "existing_description": refined_existing,
                        "new_description": refined_new,
                    }
                )
                self.last_comparison = {
                    "status": "success",
                    "same_page": bool(same_page),
                    **identity,
                    "reason": reason,
                    "existing_description": refined_existing,
                    "new_description": refined_new,
                    "attempt_count": attempt,
                    "max_attempts": max_attempts,
                    "attempts": attempt_records,
                }
                return bool(same_page), reason
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                attempt_records.append(
                    {
                        "attempt": attempt,
                        "status": "failed",
                        "error": error,
                    }
                )
                if attempt < max_attempts:
                    print(f"VLM 页面判重请求失败，准备重试（{attempt}/{max_attempts}）: {error}")
                    if self.config.vlm_compare_retry_delay > 0:
                        time.sleep(self.config.vlm_compare_retry_delay)

        reason = f"VLM 页面判重在 {max_attempts} 次请求后失败"
        self.last_comparison = {
            "status": "failed",
            "same_page": None,
            "reason": reason,
            "attempt_count": max_attempts,
            "max_attempts": max_attempts,
            "attempts": attempt_records,
            "error": attempt_records[-1].get("error", reason),
        }
        return False, reason

    def verify_replay_state(
        self, screenshot_b64: str, screen_size: Tuple[int, int], *, expected: dict,
        page_description: str = "", reference_b64: str | None = None,
        reference_size: Tuple[int, int] | None = None, reference_is_template: bool = False,
        action: dict | None = None, next_action: dict | None = None,
    ) -> Dict[str, Any]:
        current_image, _ = self._prepare_image_for_model(screenshot_b64, screen_size)
        images = []
        if reference_b64 is not None:
            reference_image, _ = self._prepare_image_for_model(reference_b64, reference_size or screen_size)
            images.append(("historical reference", reference_image))
        images.append(("CURRENT replay result", current_image))
        prompt = self.context_builder.replay_verification_prompt(
            expected, page_description, action, next_action, reference_is_template=reference_is_template)
        attempts = []
        max_attempts = max(1, self.config.vlm_compare_max_attempts)
        for attempt in range(1, max_attempts + 1):
            try:
                result = self._request_json(prompt, images, max_tokens=768)
                if not isinstance(result, dict) or type(result.get("matched")) is not bool:
                    raise ValueError("VLM replay verification must return a boolean matched")
                reason = str(result.get("reason") or "")
                attempts.append({"attempt": attempt, "status": "success",
                                 "matched": result["matched"], "reason": reason})
                return {"status": "success", "matched": result["matched"], "reason": reason,
                        "attempt_count": attempt, "attempts": attempts}
            except Exception as exc:
                attempts.append({"attempt": attempt, "status": "failed",
                                 "error": f"{type(exc).__name__}: {exc}"})
                if attempt < max_attempts and self.config.vlm_compare_retry_delay > 0:
                    time.sleep(self.config.vlm_compare_retry_delay)
        return {"status": "failed", "matched": False, "reason": attempts[-1]["error"],
                "attempt_count": max_attempts, "attempts": attempts}

    def verify_input_empty(
        self, screenshot_b64: str, screen_size: Tuple[int, int],
        bounds: Dict[str, int], target_description: str,
    ) -> Dict[str, Any]:
        """Check the already located field without asking for new coordinates."""
        image_b64, sent_size = self._prepare_image_for_model(screenshot_b64, screen_size)
        width, height = screen_size
        box = [bounds["x"] / width * 1000, bounds["y"] / height * 1000,
               (bounds["x"] + bounds["width"]) / width * 1000,
               (bounds["y"] + bounds["height"]) / height * 1000]
        prompt = self.context_builder.input_empty_prompt(target_description, box, *sent_size)
        try:
            result = self._request_json(prompt, [("current screen after clearing", image_b64)], max_tokens=256)
            if not isinstance(result, dict):
                return {"editable": False, "empty": None, "reason": "invalid response"}
            return result
        except Exception as exc:
            return {"editable": False, "empty": None, "reason": f"{type(exc).__name__}: {exc}"}

    def locate_replay_action(
        self,
        screenshot_b64: str,
        screen_size: Tuple[int, int],
        step: ActionSequenceStep,
        *, memory: Optional[dict] = None,
    ) -> Optional[Dict[str, Any]]:
        image_b64, sent_size = self._prepare_image_for_model(screenshot_b64, screen_size)
        prompt = self.context_builder.replay_prompt(step, *sent_size)
        if memory is not None:
            prompt += self.context_builder.memory_instructions(memory)
        try:
            result = self._request_json(prompt, [("current screen", image_b64)], max_tokens=1024)
            found = result.get("found", False)
            if isinstance(found, str):
                found = found.strip().lower() in {"true", "yes", "1"}
            if not found or not isinstance(result.get("action"), dict):
                return None
            self._map_analysis_coordinates_to_original(
                result,
                original_size=screen_size,
                sent_size=sent_size,
            )
            if "editable" in result:
                result["action"]["editable"] = result["editable"]
            if "value" in result:
                result["action"]["value"] = result["value"]
            return result["action"]
        except Exception as exc:
            print(f"VLM replay target location failed: {exc}")
            return None

    def locate_replay_action_with_memory(
        self, screenshot_b64: str, screen_size: Tuple[int, int],
        step: ActionSequenceStep, memory: dict,
    ) -> Optional[Dict[str, Any]]:
        return self.locate_replay_action(screenshot_b64, screen_size, step, memory=memory)

    def _request_json(
        self,
        prompt: str,
        images: List[Tuple[str, str]],
        *,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        content: List[Dict[str, Any]] = []
        for label, image_b64 in images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                }
            )
            content.append({"type": "text", "text": f"Image label: {label}"})
        content.append({"type": "text", "text": prompt})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": content}],
            max_tokens=max_tokens,
            temperature=0,
            extra_body={"enable_thinking": False},
        )
        message = response.choices[0].message
        response_content = message.content or getattr(message, "reasoning_content", None) or ""
        parsed = self._parse_json_response(response_content)
        if isinstance(parsed, list):
            parsed = {"elements": parsed}
        if not isinstance(parsed, dict):
            raise ValueError("VLM JSON must be an object or an element array")
        return parsed

    def _prepare_image_for_model(
        self,
        screenshot_b64: str,
        screen_size: Tuple[int, int],
    ) -> Tuple[str, Tuple[int, int]]:
        image_bytes = base64.b64decode(screenshot_b64)
        with Image.open(BytesIO(image_bytes)) as opened:
            image = opened.convert("RGB")

        original_width, original_height = screen_size
        if image.size != screen_size:
            original_width, original_height = image.size
        resized_height, resized_width = _smart_resize(
            original_height,
            original_width,
            factor=QWEN_RESIZE_FACTOR,
            min_pixels=QWEN_MIN_PIXELS,
            max_pixels=QWEN_MAX_PIXELS,
            max_image_side=self.max_image_side,
        )
        resized_size = (resized_width, resized_height)
        if image.size == resized_size:
            return screenshot_b64, resized_size

        resized = image.resize(resized_size, Image.Resampling.LANCZOS)
        buffer = BytesIO()
        resized.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("utf-8"), resized_size

    @staticmethod
    def _coordinate_type(analysis: Dict[str, Any]) -> str:
        declared = str(analysis.get("coordinate_space") or "").lower()
        if any(token in declared for token in ("absolute", "pixel")):
            return "absolute"
        if any(token in declared for token in ("normalized", "qwen", "1000", "999")):
            return "qwen"
        for mapping in VLMAnalyzer._coordinate_mappings(analysis):
            if any(
                key in mapping
                for key in (
                    "bbox_2d",
                    "bounds_1000",
                    "bounds_999",
                    "bbox_999",
                    "point_1000",
                    "tap_point_1000",
                    "point_999",
                    "tap_point_999",
                )
            ):
                return "qwen"
        return "qwen"

    @staticmethod
    def _coordinate_mappings(analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
        mappings: List[Dict[str, Any]] = []
        queue: List[Dict[str, Any]] = [analysis]
        seen: Set[int] = set()
        while queue:
            mapping = queue.pop(0)
            marker = id(mapping)
            if marker in seen:
                continue
            seen.add(marker)
            if mapping is not analysis:
                mappings.append(mapping)
            for key in ("elements", "actions"):
                value = mapping.get(key)
                if isinstance(value, list):
                    queue.extend(item for item in value if isinstance(item, dict))
            nested_action = mapping.get("action")
            if isinstance(nested_action, dict):
                queue.append(nested_action)
        return mappings

    @staticmethod
    def _map_analysis_coordinates_to_original(
        analysis: Dict[str, Any],
        *,
        original_size: Tuple[int, int],
        sent_size: Tuple[int, int],
    ) -> None:
        coordinate_type = VLMAnalyzer._coordinate_type(analysis)
        all_coordinate_keys = COORDINATE_BOUNDS_KEYS + COORDINATE_POINT_KEYS
        for mapping in VLMAnalyzer._coordinate_mappings(analysis):
            for key in all_coordinate_keys:
                if key not in mapping:
                    continue
                key_coordinate_type = (
                    "qwen"
                    if key == "bbox_2d" or "999" in key or "1000" in key
                    else coordinate_type
                )
                mapping[key] = VLMAnalyzer._map_coordinate_value(
                    mapping[key],
                    coordinate_type=key_coordinate_type,
                    original_size=original_size,
                    sent_size=sent_size,
                )
        analysis["model_coordinate_space"] = analysis.get("coordinate_space") or (
            "normalized_0_1000" if coordinate_type == "qwen" else "absolute_sent_image_pixels"
        )
        analysis["coordinate_space"] = "absolute_original_pixels"

    @staticmethod
    def _map_coordinate_value(
        value: Any,
        *,
        coordinate_type: str,
        original_size: Tuple[int, int],
        sent_size: Tuple[int, int],
    ) -> Any:
        def map_axis(raw: Any, axis: int) -> int:
            number = float(raw)
            original_dimension = original_size[axis]
            denominator = (
                QWEN_COORDINATE_SCALE
                if coordinate_type == "qwen"
                else max(1, sent_size[axis] - 1)
            )
            return round(number / denominator * max(0, original_dimension - 1))

        if isinstance(value, str):
            cleaned = value
            for char in "[]()<>":
                cleaned = cleaned.replace(char, " ")
            parts = [part for part in cleaned.replace(",", " ").split() if part]
            try:
                value = [float(part) for part in parts]
            except ValueError:
                return value

        if isinstance(value, dict):
            mapped = dict(value)
            try:
                if all(key in value for key in ("x", "y", "width", "height")):
                    x1 = map_axis(value["x"], 0)
                    y1 = map_axis(value["y"], 1)
                    x2 = map_axis(float(value["x"]) + float(value["width"]), 0)
                    y2 = map_axis(float(value["y"]) + float(value["height"]), 1)
                    mapped.update(
                        {
                            "x": min(x1, x2),
                            "y": min(y1, y2),
                            "width": abs(x2 - x1),
                            "height": abs(y2 - y1),
                        }
                    )
                    return mapped
                if all(key in value for key in ("x1", "y1", "x2", "y2")):
                    mapped.update(
                        {
                            "x1": map_axis(value["x1"], 0),
                            "y1": map_axis(value["y1"], 1),
                            "x2": map_axis(value["x2"], 0),
                            "y2": map_axis(value["y2"], 1),
                        }
                    )
                    return mapped
                if "x" in value and "y" in value:
                    mapped["x"] = map_axis(value["x"], 0)
                    mapped["y"] = map_axis(value["y"], 1)
                    return mapped
            except (TypeError, ValueError):
                return value
            return mapped

        if isinstance(value, (list, tuple)):
            mapped_items: List[Any] = []
            for index, item in enumerate(value):
                try:
                    mapped_items.append(map_axis(item, index % 2))
                except (TypeError, ValueError):
                    mapped_items.append(item)
            return mapped_items
        return value

    @staticmethod
    def _parse_json_response(content: str) -> Any:
        content = content.strip()
        if "```json" in content:
            content = content.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in content:
            content = content.split("```", 1)[1].split("```", 1)[0].strip()
        decoder = json.JSONDecoder()
        for start, char in enumerate(content):
            if char not in "[{":
                continue
            try:
                parsed, _ = decoder.raw_decode(content[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, (dict, list)):
                return parsed
        raise ValueError("No JSON object or array found in the model response")


class AndroidController:
    """ADB-backed Android device tool."""

    def __init__(self, config: ExplorerConfig):
        self.config = config

    def connect(self) -> None:
        self._run_adb(["start-server"], include_device=False)
        state = self._adb("get-state", check=False).strip()
        if state != "device":
            devices = self._run_adb(["devices"], include_device=False, check=False).strip()
            raise RuntimeError(
                f"ADB 设备不可用：{self.config.device_name or '<default>'}\n"
                f"adb get-state 返回：{state or '<empty>'}\n"
                f"adb devices:\n{devices}"
            )
        if self.config.app_package:
            self.restart_app()
        print(f"已连接到设备: {self.config.device_name or '<default adb device>'}")

    def launch_app(self, *, reset_task: bool = False) -> None:
        if self.config.app_activity:
            component = self._build_component(self.config.app_package, self.config.app_activity)
        else:
            resolved = self._adb("shell", "cmd", "package", "resolve-activity", "--brief",
                                 "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER",
                                 self.config.app_package)
            component = next((line.strip() for line in resolved.splitlines()
                              if line.strip().split("/", 1)[0] == self.config.app_package
                              and "/" in line.strip()), "")
        if not component or component.split("/", 1)[0] != self.config.app_package:
            raise RuntimeError("Cannot resolve a launcher Activity owned by the target package")
        flags = ["-f", "0x10008000"] if reset_task else []  # NEW_TASK | CLEAR_TASK
        output = self._adb("shell", "am", "start", "-n", component, *flags)
        if "error" in output.casefold():
            raise RuntimeError(f"Target Activity launch failed: {output.strip()}")
        time.sleep(2)

    def restart_app(self) -> None:
        self._adb("shell", "am", "force-stop", self.config.app_package)
        self.launch_app(reset_task=True)

    def capture_screenshot(self) -> Tuple[str, str, Tuple[int, int]]:
        time.sleep(self.config.screenshot_delay)
        screenshot_png = self._adb_bytes("exec-out", "screencap", "-p")
        if not screenshot_png:
            raise RuntimeError("ADB 截图失败：未收到 PNG 数据")
        with Image.open(BytesIO(screenshot_png)) as image:
            screen_size = image.size
        return (
            base64.b64encode(screenshot_png).decode("utf-8"),
            perceptual_hash_png(
                screenshot_png,
                crop_top_ratio=self.config.fingerprint_crop_top_ratio,
                crop_bottom_ratio=self.config.fingerprint_crop_bottom_ratio,
            ),
            screen_size,
        )

    def get_current_activity(self) -> str:
        # Task membership is not foreground ownership: a browser can share the
        # target app's task. Prefer the top resumed Activity, not a keyboard window.
        activity_output = self._adb("shell", "dumpsys", "activity", "activities", check=False)
        activity = self._parse_focused_component(activity_output)
        if activity:
            return activity
        window_output = self._adb("shell", "dumpsys", "window", check=False)
        return self._parse_focused_component(window_output)

    def click_element(self, bounds: Dict[str, int]) -> bool:
        try:
            x = int(bounds["x"]) + int(bounds["width"]) // 2
            y = int(bounds["y"]) + int(bounds["height"]) // 2
            self._adb("shell", "input", "tap", str(x), str(y))
            time.sleep(1)
            return True
        except Exception as exc:
            print(f"点击失败: {exc}")
            return False

    def input_text(self, bounds: Dict[str, int], text: str, mode: str = "append", *,
                   focus=True, current_value: Optional[str] = None) -> bool:
        if text and (not text.isascii() or not text.isprintable() or "%" in text):
            print("输入失败: adb input text 仅支持不含 % 的可打印 ASCII 测试文本")
            return False
        if mode not in {"replace", "append", "clear"}:
            return False
        if mode in {"replace", "clear"} and (not isinstance(current_value, str) or len(current_value) > 512):
            return False
        if focus and not self.click_element(bounds):
            return False
        try:
            if mode in {"replace", "clear"} and current_value:
                delete_count = len(current_value) + INPUT_CLEAR_DELETE_MARGIN
                for keys in [("KEYCODE_MOVE_END",), ("KEYCODE_DEL",) * delete_count]:
                    output = self._adb("shell", "input", "keyevent", *keys)
                    if any(word in output.casefold() for word in ("error", "unknown command", "usage:")):
                        return False
                time.sleep(0.5)
            if mode == "clear" or not text:
                return True
            encoded = text.replace(" ", "%s")
            remote_command = f"input text {shlex.quote(encoded)}"
            self._adb("shell", remote_command)
            time.sleep(1)
            return True
        except Exception as exc:
            print(f"输入失败: {exc}")
            return False

    def go_back(self) -> bool:
        try:
            self._adb("shell", "input", "keyevent", "KEYCODE_BACK")
            time.sleep(0.5)
            return True
        except Exception as exc:
            print(f"返回失败: {exc}")
            return False

    def swipe(self, start: Tuple[int, int], end: Tuple[int, int], duration_ms: int = 300) -> bool:
        try:
            self._adb(
                "shell",
                "input",
                "swipe",
                str(start[0]),
                str(start[1]),
                str(end[0]),
                str(end[1]),
                str(duration_ms),
            )
            time.sleep(1)
            return True
        except Exception as exc:
            print(f"滑动失败: {exc}")
            return False

    def execute_action(self, action: ExplorationAction, screen_size: Tuple[int, int]) -> bool:
        if action.action_type == "click":
            return self.click_element(action.bounds)
        if action.action_type == "input":
            locator = getattr(self, "input_locator", None)
            if callable(locator):
                return self._execute_observed_input(action, locator)
            return self.input_text(action.bounds, action.input_text, action.input_mode)
        if action.action_type == "swipe":
            width, height = screen_size
            start = action.swipe_start or {"x": width // 2, "y": int(height * 0.75)}
            end = action.swipe_end or {"x": width // 2, "y": int(height * 0.25)}
            return self.swipe(
                (int(start["x"]), int(start["y"])),
                (int(end["x"]), int(end["y"])),
                action.duration_ms,
            )
        print(f"不支持的动作类型: {action.action_type}")
        return False

    def _execute_observed_input(self, action, locator) -> bool:
        """Never type replacement text until current screenshot evidence confirms empty."""
        text = action.input_text
        if (action.input_mode not in {"replace", "append", "clear"}
                or (text and (not text.isascii() or not text.isprintable() or "%" in text))):
            return False
        if not self.click_element(action.bounds):
            return False
        observed = locator(self.capture_screenshot(), action)
        if (not isinstance(observed, InputFieldObservation) or observed.value is None
                or not self.click_element(observed.bounds)):
            return False
        if action.input_mode in {"replace", "clear"} and observed.value:
            checker = getattr(self, "input_empty_checker", None)
            if not callable(checker):
                return False
            if not self.input_text(observed.bounds, "", "clear", focus=False, current_value=observed.value):
                return False
            # Reuse the one located box. Command success does not prove empty.
            if checker(self.capture_screenshot(), action, observed.bounds) is not True:
                return False
        if action.input_mode == "clear":
            return True
        return self.input_text(observed.bounds, text, "append", focus=False)

    def quit(self) -> None:
        return None

    def _adb(self, *args: str, check: bool = True) -> str:
        output = self._run_adb(list(args), check=check)
        return output.decode("utf-8", errors="replace")

    def _adb_bytes(self, *args: str, check: bool = True) -> bytes:
        return self._run_adb(list(args), check=check)

    def _run_adb(
        self,
        args: List[str],
        *,
        include_device: bool = True,
        check: bool = True,
    ) -> bytes:
        cmd = [self.config.adb_path]
        if include_device and self.config.device_name:
            cmd.extend(["-s", self.config.device_name])
        cmd.extend(args)
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.config.adb_timeout,
            check=False,
        )
        if check and proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ADB 命令失败: {' '.join(cmd)}\n{stderr}")
        return proc.stdout

    @staticmethod
    def _build_component(package: str, activity: str) -> str:
        if "/" in activity:
            return activity
        return f"{package}/{activity}"

    @staticmethod
    def _parse_focused_component(output: str) -> str:
        for marker in ("topResumedActivity", "mResumedActivity", "ResumedActivity:",
                       "mCurrentFocus", "mFocusedApp"):
            for line in output.splitlines():
                if marker in line:
                    match = re.search(r"([\w.]+/[\w.$]+)", line)
                    if match:
                        return match.group(1)
        return ""
