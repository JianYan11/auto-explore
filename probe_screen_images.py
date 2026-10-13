#!/usr/bin/env python3
"""Probe Android UI image URLs and export separately labeled rendered-pixel crops.

Requires Pillow, but no model/API key. Leave the device on the target screen.
This does not launch/restart the App or modify the exploration harness.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw


BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
IMAGE_ID = re.compile(r"image|img|avatar|thumbnail|thumb|cover|poster|banner|icon|logo|photo|picture", re.I)
EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif",
              "BMP": ".bmp", "TIFF": ".tiff", "ICO": ".ico", "AVIF": ".avif"}


def capture_screenshot(adb: str, device: str, timeout: float) -> bytes:
    """Capture composited screen pixels without requiring an idle UI hierarchy."""
    command = [adb] + (["-s", device] if device else [])
    result = subprocess.run(command + ["exec-out", "screencap", "-p"],
                            capture_output=True, timeout=timeout)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ADB screencap failed: {detail}")
    with Image.open(BytesIO(result.stdout)) as image:
        image.verify()
    return result.stdout


def capture_current_screen(adb: str, device: str, timeout: float) -> tuple[bytes, bytes]:
    """Return a fresh XML dump and native-resolution PNG without navigating."""
    command = [adb] + (["-s", device] if device else [])

    def run(*args: str) -> bytes:
        result = subprocess.run(command + list(args), capture_output=True, timeout=timeout)
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ADB {' '.join(args[:2])} failed: {detail}")
        return result.stdout

    if run("get-state").strip() != b"device":
        raise RuntimeError("ADB device is unavailable; check adb devices.")
    # A unique filename prevents a failed dump from reusing an old screen's XML.
    remote = f"/sdcard/screen_image_probe_{uuid.uuid4().hex}.xml"
    try:
        run("shell", "uiautomator", "dump", remote)
        xml = run("exec-out", "cat", remote)
        ET.fromstring(xml)
        png = capture_screenshot(adb, device, timeout)
        return xml, png
    finally:
        try:
            run("shell", "rm", "-f", remote)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass


def image_nodes(root: ET.Element, package: str = "", id_regex: str = "") -> list[dict]:
    """Find candidate controls, retaining evidence instead of claiming certainty."""
    extra_id = re.compile(id_regex) if id_regex else None
    candidates = []
    for index, node in enumerate(root.iter("node")):
        attrs = dict(node.attrib)
        if package and attrs.get("package") != package:
            continue
        if attrs.get("visible-to-user", "true").lower() == "false":
            continue
        if attrs.get("password", "false").lower() == "true":
            continue
        reasons = []
        class_name = attrs.get("class", "").lower()
        resource_id = attrs.get("resource-id", "")
        if "image" in class_name:
            reasons.append("image_class")
        if IMAGE_ID.search(resource_id.rsplit("/", 1)[-1]):
            reasons.append("image_resource_id_heuristic")
        if extra_id and extra_id.search(resource_id):
            reasons.append("user_resource_id_regex")
        sources = []
        for name, value in attrs.items():
            for match in URL.finditer(value):
                sources.append({"attribute": name, "url": match.group(0)})
        if sources:
            reasons.append("http_url_in_attribute")
        if not reasons:
            continue
        match = BOUNDS.fullmatch(attrs.get("bounds", "").strip())
        candidates.append({
            "node_index": index,
            "attributes": attrs,
            "candidate_reasons": reasons,
            "bounds": list(map(int, match.groups())) if match else None,
            "url_sources": sources,
        })
    return candidates


def download_image(url: str, destination: Path, timeout: float, max_bytes: int) -> dict:
    """Validate image bytes; never save a login/error HTML page as an image."""
    request = Request(url, headers={"User-Agent": "AndroidScreenImageProbe/1.0"})
    with urlopen(request, timeout=timeout) as response:
        final_url = response.geturl()
        if not final_url.lower().startswith(("http://", "https://")):
            raise ValueError("Unsupported redirect scheme")
        data = response.read(max_bytes + 1)
        content_type = response.headers.get("Content-Type", "")
    if len(data) > max_bytes:
        raise ValueError(f"Response exceeds {max_bytes} bytes")
    with Image.open(BytesIO(data)) as image:
        image_format, size = image.format, image.size
        image.verify()
    # verify() is format-dependent; decode too, so truncated images fail.
    with Image.open(BytesIO(data)) as image:
        image.load()
    extension = EXTENSIONS.get(image_format)
    if not extension:
        raise ValueError(f"Unsupported image format: {image_format}")
    path = destination.with_suffix(extension)
    path.write_bytes(data)  # Preserve source bytes, rather than re-encoding.
    return {"status": "downloaded", "file": path.name, "bytes": len(data),
            "format": image_format, "size": list(size), "content_type": content_type,
            "final_url": final_url, "screen_correspondence": "unverified"}


def probe_snapshot(
    xml: bytes | None, png: bytes, output: Path, *, package: str = "", id_regex: str = "",
    download: bool = True, timeout: float = 10, max_bytes: int = 10 * 1024 * 1024,
    max_downloads: int = 20, manual_regions: list[tuple[int, int, int, int]] | None = None,
) -> dict:
    """Write XML, screenshot, candidate downloads, crops and a machine-readable report.

    `output` must be new, so a failed/empty run cannot expose stale image files.
    XML and screenshot must be from the same screen at native resolution.
    """
    root = ET.fromstring(xml) if xml is not None else None
    nodes = image_nodes(root, package, id_regex) if root is not None else []
    with Image.open(BytesIO(png)) as source:
        screen = source.convert("RGB")
    output.mkdir(parents=True, exist_ok=False)
    if xml is not None:
        (output / "screen.xml").write_bytes(xml)
    (output / "screen.png").write_bytes(png)
    (output / "crops").mkdir()
    (output / "downloads").mkdir()
    annotated = screen.copy()
    draw = ImageDraw.Draw(annotated)
    width, height = screen.size
    downloads: dict[str, dict] = {}
    attempts = 0
    for number, node in enumerate(nodes, start=1):
        node["candidate_id"] = number
        bounds = node["bounds"]
        node["crop"] = {"status": "invalid_or_missing_bounds"}
        if bounds and bounds[0] < bounds[2] and bounds[1] < bounds[3]:
            x1, y1 = max(0, bounds[0]), max(0, bounds[1])
            x2, y2 = min(width, bounds[2]), min(height, bounds[3])
            node["crop"] = {"status": "outside_screenshot"}
            if x1 < x2 and y1 < y2:
                crop_bounds = [x1, y1, x2, y2]
                filename = f"crops/node_{number:03d}.png"
                screen.crop(crop_bounds).save(output / filename)
                node["crop"] = {"status": "saved", "file": filename,
                                "kind": "screenshot_crop_not_original",
                                "bounds": crop_bounds, "clipped": crop_bounds != bounds}
                draw.rectangle((x1, y1, x2 - 1, y2 - 1), outline="#ff2d55", width=3)
                draw.text((x1 + 3, y1 + 3), str(number), fill="#ff2d55",
                          stroke_width=1, stroke_fill="white")
        for source in node["url_sources"]:
            url = source["url"]
            if url in downloads:
                continue
            result = {"url": url}
            if not download:
                result["status"] = "disabled"
            elif attempts >= max_downloads:
                result["status"] = "download_limit_reached"
            else:
                attempts += 1
                try:
                    result.update(download_image(
                        url, output / "downloads" / f"image_{attempts:03d}", timeout, max_bytes,
                    ))
                    result["file"] = "downloads/" + result["file"]
                except Exception as exc:
                    result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            downloads[url] = result
    regions = []
    for number, bounds in enumerate(manual_regions or [], start=1):
        x1, y1 = max(0, bounds[0]), max(0, bounds[1])
        x2, y2 = min(width, bounds[2]), min(height, bounds[3])
        result = {"requested_bounds": list(bounds), "status": "outside_screenshot"}
        if x1 < x2 and y1 < y2:
            clipped = [x1, y1, x2, y2]
            filename = f"crops/region_{number:03d}.png"
            screen.crop(clipped).save(output / filename)
            result.update(status="saved", file=filename, bounds=clipped,
                          clipped=clipped != list(bounds),
                          kind="screenshot_crop_not_original")
            draw.rectangle((x1, y1, x2 - 1, y2 - 1), outline="#00d4ff", width=3)
        regions.append(result)
    annotated.save(output / "annotated.png")
    downloaded = sum(item["status"] == "downloaded" for item in downloads.values())
    crops = (sum(node["crop"]["status"] == "saved" for node in nodes)
             + sum(region["status"] == "saved" for region in regions))
    verdict = ("screenshot_only_no_original_image_url" if root is None else
               "image_url_downloaded_correspondence_unverified" if downloaded else
               "no_http_url_in_xml_candidates" if not downloads else
               "downloads_disabled" if not download else "no_image_download_succeeded")
    report = {
        "verdict": verdict,
        "capture_mode": "screenshot_only" if root is None else "xml_and_screenshot",
        "screen_size": [width, height], "package_filter": package,
        "total_xml_nodes": sum(1 for _ in root.iter("node")) if root is not None else 0,
        "summary": {"candidate_nodes": len(nodes), "unique_http_urls": len(downloads),
                    "downloaded_images": downloaded, "screenshot_crops": crops},
        "limitations": [
            "resource-id identifies a View, not an image file or download URL.",
            "Crops contain rendered screen pixels, including padding/overlays; they are not original assets.",
            "URL downloads are image candidates, not proof of correspondence or original resolution.",
            "Custom drawing, WebView and merged accessibility nodes may hide individual images.",
            "XML and screenshot capture are not atomic; keep the screen still and use native resolution.",
            "Only HTTP(S) URLs and Pillow-supported raster images are tested; no App cookies/auth are copied.",
        ],
        "nodes": nodes, "manual_regions": regions, "downloads": list(downloads.values()),
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="测试 Android 界面 XML 图片下载，并单独保存截图裁剪。")
    parser.add_argument("--adb", default=os.getenv("ADB_PATH", "adb"), help="ADB 路径")
    parser.add_argument("--device", default=os.getenv("ANDROID_SERIAL", ""), help="设备序列号")
    parser.add_argument("--package", default="", help="只处理指定包名的节点，不启动 App")
    parser.add_argument("--xml", type=Path, help="已有的 UIAutomator XML；与 --screenshot 一起使用")
    parser.add_argument("--screenshot", type=Path, help="与 XML 对应的原始分辨率截图")
    parser.add_argument("--screenshot-only", action="store_true",
                        help="跳过容易卡住的 UIAutomator；只能保存屏幕像素，不能发现原图 URL")
    parser.add_argument("--region", action="append", default=[], metavar="X1,Y1,X2,Y2",
                        help="裁剪屏幕像素区域；可重复指定，坐标使用设备原始分辨率")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "output" / "screen_image_probe",
                        help="输出根目录，每次创建独立子目录")
    parser.add_argument("--id-regex", default="", help="额外候选控件 resource-id 的正则表达式")
    parser.add_argument("--no-download", action="store_true", help="只检查 XML 和裁剪，不访问 URL")
    parser.add_argument("--timeout", type=float, default=10, help="HTTP 请求超时秒数")
    parser.add_argument("--adb-timeout", type=float, default=30, help="每次 ADB 操作超时秒数")
    parser.add_argument("--max-mb", type=int, default=10, help="单个下载的最大 MB 数")
    parser.add_argument("--max-downloads", type=int, default=20, help="最多尝试的不同 URL 数")
    args = parser.parse_args(argv)
    if args.xml and (not args.screenshot or args.screenshot_only):
        parser.error("--xml 需要 --screenshot，且不能与 --screenshot-only 同用")
    if args.screenshot and not args.xml and not args.screenshot_only:
        parser.error("单独使用 --screenshot 时需要 --screenshot-only")
    if min(args.timeout, args.adb_timeout, args.max_mb, args.max_downloads) <= 0:
        parser.error("超时和下载上限必须大于 0")
    try:
        re.compile(args.id_regex)
        regions = []
        for value in args.region:
            parts = [int(part.strip()) for part in value.split(",")]
            if len(parts) != 4 or parts[0] >= parts[2] or parts[1] >= parts[3]:
                raise ValueError(f"无效 --region: {value}")
            regions.append(tuple(parts))
        if args.screenshot_only:
            xml, png = None, (args.screenshot.read_bytes() if args.screenshot else
                              capture_screenshot(args.adb, args.device, args.adb_timeout))
        elif args.xml:
            xml, png = args.xml.read_bytes(), args.screenshot.read_bytes()
        else:
            print("读取当前页面 XML 和截图，请保持页面静止……")
            xml, png = capture_current_screen(args.adb, args.device, args.adb_timeout)
        name = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        output = args.output.resolve() / name
        report = probe_snapshot(
            xml, png, output, package=args.package, id_regex=args.id_regex,
            download=not args.no_download, timeout=args.timeout,
            max_bytes=args.max_mb * 1024 * 1024, max_downloads=args.max_downloads,
            manual_regions=regions,
        )
    except (OSError, RuntimeError, ValueError, ET.ParseError, re.error, subprocess.TimeoutExpired) as exc:
        print(f"测试未完成: {exc}", file=sys.stderr)
        return 1
    counts = report["summary"]
    print(f"候选节点: {counts['candidate_nodes']}；HTTP(S) 链接: {counts['unique_http_urls']}；"
          f"下载图片: {counts['downloaded_images']}；截图裁剪: {counts['screenshot_crops']}")
    if args.screenshot_only:
        print("仅截图模式未读取 UI XML，因此不能据此判断或下载原图。")
    elif not counts["unique_http_urls"]:
        print("该页面 XML 未暴露候选图片下载地址，无法仅凭它下载原图。")
    elif counts["downloaded_images"]:
        print("已下载有效图片文件；是否对应屏幕中的图片，需要对照截图确认。")
    else:
        print("未下载到图片，请查看 report.json 中各 URL 的状态和原因。")
    print("crops 中的文件只是截图裁剪，不代表下载到了原图。")
    print(f"结果目录: {output}")
    return 0  # A completed probe can legitimately find no downloadable images.


if __name__ == "__main__":
    raise SystemExit(main())
