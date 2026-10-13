"""Deterministic screenshot fingerprints and fast visual similarity checks."""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from io import BytesIO
from typing import Any, Dict

from PIL import Image, ImageChops, ImageFilter, ImageStat


_COMPARE_SIZE = (128, 256)
_DHASH_SIZE = (17, 16)


@dataclass(frozen=True)
class VisualComparison:
    """Result of comparing normalized application-content screenshots."""

    available: bool
    same_screen: bool
    candidate_hash: str = ""
    existing_hash: str = ""
    hash_distance: int = -1
    mean_difference: float = 1.0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _crop_application_content(
    image: Image.Image,
    *,
    crop_top_ratio: float,
    crop_bottom_ratio: float,
) -> Image.Image:
    """Remove system chrome while always retaining a non-empty content region."""

    width, height = image.size
    top = max(0, min(height - 1, round(height * crop_top_ratio)))
    bottom = max(top + 1, min(height, height - round(height * crop_bottom_ratio)))
    return image.crop((0, top, width, bottom))


def _normalized_content(
    image: Image.Image,
    size: tuple[int, int],
    *,
    crop_top_ratio: float,
    crop_bottom_ratio: float,
) -> Image.Image:
    content = _crop_application_content(
        image.convert("L"),
        crop_top_ratio=crop_top_ratio,
        crop_bottom_ratio=crop_bottom_ratio,
    )
    # Downsampling and a slight blur suppress transient tap dots and animation noise.
    return content.resize(size, Image.Resampling.LANCZOS).filter(
        ImageFilter.GaussianBlur(radius=0.6)
    )


def _dhash(normalized: Image.Image) -> str:
    image = normalized.resize(_DHASH_SIZE, Image.Resampling.LANCZOS)
    pixels = list(image.getdata())
    bits = 0
    for y in range(_DHASH_SIZE[1]):
        row = y * _DHASH_SIZE[0]
        for x in range(_DHASH_SIZE[0] - 1):
            bits = (bits << 1) | int(pixels[row + x] > pixels[row + x + 1])
    return f"dhash256:{bits:064x}"


def _open_png(image_bytes: bytes) -> Image.Image:
    with Image.open(BytesIO(image_bytes)) as opened:
        return opened.convert("RGB")


def perceptual_hash_png(
    image_bytes: bytes,
    *,
    crop_top_ratio: float = 0.07,
    crop_bottom_ratio: float = 0.05,
) -> str:
    """Return a system-chrome-free perceptual fingerprint for a PNG screenshot."""

    image = _open_png(image_bytes)
    normalized = _normalized_content(
        image,
        _COMPARE_SIZE,
        crop_top_ratio=crop_top_ratio,
        crop_bottom_ratio=crop_bottom_ratio,
    )
    return _dhash(normalized)


def _hash_distance(first: str, second: str) -> int:
    first_hex = first.rsplit(":", 1)[-1]
    second_hex = second.rsplit(":", 1)[-1]
    return (int(first_hex, 16) ^ int(second_hex, 16)).bit_count()


def compare_screenshot_b64(
    candidate_b64: str,
    existing_b64: str,
    *,
    crop_top_ratio: float = 0.07,
    crop_bottom_ratio: float = 0.05,
    hash_distance_threshold: int = 3,
    mean_difference_threshold: float = 0.00025,
) -> VisualComparison:
    """Conservatively identify near-identical screens before invoking a VLM."""

    try:
        candidate = _normalized_content(
            _open_png(base64.b64decode(candidate_b64, validate=True)),
            _COMPARE_SIZE,
            crop_top_ratio=crop_top_ratio,
            crop_bottom_ratio=crop_bottom_ratio,
        )
        existing = _normalized_content(
            _open_png(base64.b64decode(existing_b64, validate=True)),
            _COMPARE_SIZE,
            crop_top_ratio=crop_top_ratio,
            crop_bottom_ratio=crop_bottom_ratio,
        )
        candidate_hash = _dhash(candidate)
        existing_hash = _dhash(existing)
        hash_distance = _hash_distance(candidate_hash, existing_hash)
        difference = ImageChops.difference(candidate, existing)
        mean_difference = ImageStat.Stat(difference).mean[0] / 255.0
        same_screen = (
            hash_distance <= hash_distance_threshold
            and mean_difference <= mean_difference_threshold
        )
        relation = "matched" if same_screen else "different"
        return VisualComparison(
            available=True,
            same_screen=same_screen,
            candidate_hash=candidate_hash,
            existing_hash=existing_hash,
            hash_distance=hash_distance,
            mean_difference=mean_difference,
            reason=(
                f"fast visual {relation}: hash_distance={hash_distance} "
                f"(max {hash_distance_threshold}), mean_difference={mean_difference:.6f} "
                f"(max {mean_difference_threshold:.6f})"
            ),
        )
    except Exception as exc:
        return VisualComparison(
            available=False,
            same_screen=False,
            reason=f"fast visual comparison unavailable: {exc}",
        )
