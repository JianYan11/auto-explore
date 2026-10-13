"""UI-KOBE-style description retrieval; embeddings never decide page identity."""

from __future__ import annotations

import hashlib
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .config import ExplorerConfig
from .contracts import PageState, ScreenCapture


def validated_embedding(values: object) -> list[float]:
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("Embedding must be a non-empty numeric vector")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        raise ValueError("Embedding contains non-numeric values")
    vector = [float(value) for value in values]
    norm = math.hypot(*vector)
    if not all(math.isfinite(value) for value in vector) or not math.isfinite(norm) or norm == 0:
        raise ValueError("Embedding must be finite with a non-zero norm")
    return vector


def cosine_similarity(first: list[float], second: list[float]) -> float | None:
    if len(first) != len(second):
        return None
    try:
        first = validated_embedding(first)
        second = validated_embedding(second)
    except (ValueError, OverflowError):
        return None
    first_norm, second_norm = math.hypot(*first), math.hypot(*second)
    return max(-1.0, min(1.0, sum(
        (a / first_norm) * (b / second_norm) for a, b in zip(first, second)
    )))


@dataclass
class SemanticScreen:
    description: str = ""
    state: dict = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)
    model: str = ""
    error: str = ""
    comparison_state: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BestSemanticMatch:
    page: PageState
    similarity: float
    compared_count: int


class SemanticRetriever:
    def __init__(self, config: ExplorerConfig, analyzer: Any):
        self.config = config
        self.analyzer = analyzer
        # Old injected analyzers remain usable without the new optional protocol.
        self.enabled = bool(config.semantic_dedup) and all(
            callable(getattr(analyzer, method, None))
            for method in ("describe_screen", "embed_description")
        )
        self.cache: OrderedDict[str, SemanticScreen] = OrderedDict()

    def describe(self, capture: ScreenCapture) -> SemanticScreen:
        if not self.enabled:
            return SemanticScreen()
        # A perceptual hash can collide on different controls; cache exact input only.
        key = hashlib.sha256(capture.image_b64.encode("utf-8")).hexdigest()
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        result = SemanticScreen()
        try:
            raw = self.analyzer.describe_screen(capture.image_b64, capture.screen_size)
            description = raw.get("page_description")
            if not isinstance(description, str) or not description.strip():
                raise ValueError("Missing page_description")
            result.description = description.strip()
            raw_state = raw.get("state", {})
            if isinstance(raw_state, dict):
                result.state = {
                    key: value for key, value in raw_state.items()
                    if value is None or isinstance(value, (str, bool, int))
                    or (isinstance(value, float) and math.isfinite(value))
                }
            result.embedding = validated_embedding(
                self.analyzer.embed_description(result.description)
            )
            result.model = self.config.embedding_model
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            print(f"  语义检索失败，停止本次节点匹配: {result.error}")
        self.cache[key] = result
        if len(self.cache) > 128:
            self.cache.popitem(last=False)
        return result

    def best_match(
        self,
        candidate: SemanticScreen,
        pages: list[PageState],
        app_package: str,
    ) -> BestSemanticMatch | None:
        """Return the highest-similarity node from the same App package only."""

        best_page: PageState | None = None
        best_similarity = -1.0
        compared_count = 0
        for page in pages:
            page_package = page.app_package
            if not page_package and "/" in page.activity:
                page_package = page.activity.split("/", 1)[0].strip()
            if page_package != app_package:
                continue
            if not candidate.model or candidate.model != page.embedding_model:
                continue
            similarity = cosine_similarity(candidate.embedding, page.description_embedding)
            if similarity is None:
                continue
            compared_count += 1
            if best_page is None or similarity > best_similarity:
                best_page = page
                best_similarity = similarity
        if best_page is None:
            return None
        return BestSemanticMatch(best_page, best_similarity, compared_count)

    def apply_refined_descriptions(
        self,
        candidate: SemanticScreen,
        existing_page: PageState,
        existing_description: str,
        new_description: str,
        existing_screenshot_b64: str = "",
    ) -> None:
        """Apply verifier-provided disambiguating names and refresh their vectors."""

        if existing_description and existing_description != existing_page.page_description:
            existing_page.page_description = existing_description
            existing_page.description_embedding = validated_embedding(
                self.analyzer.embed_description(existing_description)
            )
            existing_page.embedding_model = self.config.embedding_model
            if existing_screenshot_b64:
                cache_key = hashlib.sha256(
                    existing_screenshot_b64.encode("utf-8")
                ).hexdigest()
                cached = self.cache.get(cache_key)
                if cached is not None:
                    cached.description = existing_page.page_description
                    cached.embedding = list(existing_page.description_embedding)
                    cached.model = existing_page.embedding_model
        if new_description and new_description != candidate.description:
            candidate.description = new_description
            candidate.embedding = validated_embedding(
                self.analyzer.embed_description(new_description)
            )
            candidate.model = self.config.embedding_model
