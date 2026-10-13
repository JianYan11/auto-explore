#!/usr/bin/env python3
"""Export actual image files from an Android app's on-device cache via root ADB.

This is a cache probe, not an HTTPS interceptor or a screenshot extractor.
Only Pillow is required. It never clears caches, changes certificates or launches apps.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
import warnings
from io import BytesIO
from pathlib import Path, PurePosixPath


PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*")
EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif",
              "BMP": ".bmp", "TIFF": ".tiff", "ICO": ".ico", "AVIF": ".avif"}
LIMITATIONS = [
    "Exported files are cached image bytes, not screenshots and not necessarily full-resolution originals.",
    "Caches may contain images from previous pages; correspondence to the current screen is unverified.",
    "No images found does not prove that network capture is impossible.",
    "Memory-only images, APK assets, encrypted/wrapped caches, databases and unsupported formats are not extracted.",
    "Default scope is the current Android user's internal and external app cache directories.",
    "A running app can replace or remove cache files during the scan; this is not an atomic snapshot.",
]


class ProbeError(RuntimeError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def find_adb(explicit: str) -> str:
    if explicit:
        return explicit
    on_path = shutil.which("adb")
    if on_path:
        return on_path
    for root in (os.getenv("ANDROID_HOME"), os.getenv("ANDROID_SDK_ROOT"),
                 str(Path(os.getenv("LOCALAPPDATA", "")) / "Android" / "Sdk")):
        if root:
            for name in ("adb.exe", "adb"):
                candidate = Path(root) / "platform-tools" / name
                if candidate.is_file():
                    return str(candidate)
    raise ProbeError("adb_missing", "未找到 adb；请用 --adb 指定 Android SDK 的 adb 路径。")


class Adb:
    def __init__(self, executable: str, device: str = "", timeout: float = 30):
        self.executable, self.device, self.timeout = executable, device, timeout

    def run(self, *args: str, targeted: bool = True) -> bytes:
        command = [self.executable]
        if targeted and self.device:
            command += ["-s", self.device]
        result = subprocess.run(command + list(args), capture_output=True, timeout=self.timeout)
        if result.returncode:
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
            raise ProbeError("adb_error", f"ADB {args[0]} 失败: {detail[:1000]}")
        return result.stdout

    def shell(self, command: str) -> bytes:
        # Shell v2 with no PTY preserves binary bytes and propagates remote exit status.
        return self.run("shell", "-T", command)

    def select_device(self) -> None:
        devices = {}
        for line in self.run("devices", targeted=False).decode().splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1] in {"device", "offline", "unauthorized", "recovery"}:
                devices[fields[0]] = fields[1]
        if self.device:
            if devices.get(self.device) != "device":
                raise ProbeError("device_unavailable", f"设备 {self.device} 不可用: {devices.get(self.device, '未连接')}")
        elif len(devices) == 1 and next(iter(devices.values())) == "device":
            self.device = next(iter(devices))
        elif not devices:
            raise ProbeError("no_device", "没有连接的设备。请先启动 Android Studio 模拟器，再重新运行。")
        else:
            raise ProbeError("device_selection_required", f"请用 --device 选择在线设备，当前设备: {devices}")

    def inspect(self, request_root: bool) -> dict:
        info = {"serial": self.device}
        for key, prop in (("android_version", "ro.build.version.release"),
                          ("api_level", "ro.build.version.sdk"), ("build_type", "ro.build.type"),
                          ("emulator", "ro.kernel.qemu")):
            info[key] = self.shell(f"getprop {prop}").decode().strip()
        info["initial_uid"] = self.shell("id -u").decode().strip()
        if request_root and info["initial_uid"] != "0":
            if info["emulator"] != "1":
                raise ProbeError("not_emulator", "--root 仅用于 Android 模拟器；不会尝试修改真机的 root 状态。")
            info["adb_root_message"] = self.run("root").decode(errors="replace").strip()
            self.run("wait-for-device")
        # adb root can return exit code zero even when the production build rejects it.
        info["uid"] = self.shell("id -u").decode().strip()
        info["root_available"] = info["uid"] == "0"
        return info

    def app_scope(self, package: str) -> tuple[str, str]:
        user = self.shell("am get-current-user").decode().strip()
        if not user.isdigit():
            raise ProbeError("user_detection_failed", f"无法确认 Android 用户: {user}")
        if not package:
            activity = self.shell("dumpsys activity activities").decode(errors="replace")
            for line in activity.splitlines():
                if "mResumedActivity" in line or "topResumedActivity" in line:
                    match = re.search(r"\bu\d+\s+([A-Za-z][A-Za-z0-9_.]*)/", line)
                    if match:
                        package = match.group(1)
                        break
        if not PACKAGE.fullmatch(package):
            raise ProbeError("package_required", "无法识别前台 App，请用 --package 指定包名。")
        paths = self.shell(f"pm path --user {user} {shlex.quote(package)}").decode().strip()
        if not any(line.startswith("package:") for line in paths.splitlines()):
            raise ProbeError("package_not_installed", f"当前 Android 用户 {user} 未安装 {package}。")
        return package, user

    def list_files(self, directory: str) -> tuple[list[str], dict]:
        quoted = shlex.quote(directory)
        exists = self.shell(f"if [ -d {quoted} ]; then echo present; else echo missing; fi").strip()
        if exists != b"present":
            return [], {"path": directory, "status": "missing"}
        raw = self.shell(f"find {quoted} -type f -print0")
        files = []
        root = PurePosixPath(directory)
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            path = entry.decode("utf-8", errors="strict")
            candidate = PurePosixPath(path)
            if ".." in candidate.parts or not candidate.is_relative_to(root):
                raise ProbeError("unexpected_path", f"缓存索引返回了范围外路径: {path}")
            files.append(path)
        return sorted(files), {"path": directory, "status": "listed", "file_count": len(files)}

    def read(self, path: str, limit: int) -> bytes:
        quoted = shlex.quote(path)
        # Do not follow a file changed into a symlink after enumeration.
        return self.shell(f"test ! -L {quoted} && head -c {limit} {quoted}")


def looks_like_image(data: bytes) -> bool:
    return (data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a",
                             b"BM", b"II*\0", b"MM\0*", b"\0\0\x01\0"))
            or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
            or (data[4:8] == b"ftyp" and any(brand in data[8:64] for brand in (b"avif", b"avis"))))


def verify_image(data: bytes) -> dict:
    from PIL import Image

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as image:
            image_format, size = image.format, image.size
            if image_format not in EXTENSIONS:
                raise ValueError(f"Unsupported image format: {image_format}")
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()  # Reject truncated raster data, even if its header is valid.
    return {"format": image_format, "size": list(size), "extension": EXTENSIONS[image_format]}


def scan_images(source, files: list[str], output: Path, *, max_files: int = 1000,
                max_bytes: int = 20 * 1024 * 1024, max_images: int = 200,
                max_total_bytes: int = 200 * 1024 * 1024) -> dict:
    """Validate candidate file bytes, deduplicate them, and preserve exact contents."""
    image_dir = output / "images"
    image_dir.mkdir(parents=True, exist_ok=False)
    summary = {"listed_files": len(files), "scanned_files": 0, "image_candidates": 0,
               "exported_images": 0, "duplicate_images": 0, "exported_bytes": 0,
               "invalid_images": 0, "read_errors": 0, "oversized_images": 0}
    records, seen, limits = [], {}, []
    for path in files[:max_files]:
        if summary["exported_images"] >= max_images:
            limits.append("max_images")
            break
        summary["scanned_files"] += 1
        if summary["scanned_files"] % 50 == 0:
            print(f"已检查 {summary['scanned_files']} / {min(len(files), max_files)} 个文件……", flush=True)
        try:
            if not looks_like_image(source.read(path, 64)):
                continue
            summary["image_candidates"] += 1
            data = source.read(path, max_bytes + 1)
        except (OSError, ProbeError, subprocess.TimeoutExpired) as exc:
            summary["read_errors"] += 1
            records.append({"source": path, "status": "read_error", "error": str(exc)})
            continue
        if len(data) > max_bytes:
            summary["oversized_images"] += 1
            records.append({"source": path, "status": "too_large"})
            continue
        try:
            metadata = verify_image(data)
        except Exception as exc:
            summary["invalid_images"] += 1
            records.append({"source": path, "status": "invalid_or_unsupported_image", "error": str(exc)})
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            summary["duplicate_images"] += 1
            records.append({"source": path, "status": "duplicate", "file": seen[digest], "sha256": digest})
            continue
        if summary["exported_bytes"] + len(data) > max_total_bytes:
            limits.append("max_total_bytes")
            break
        filename = f"images/{digest}{metadata.pop('extension')}"
        (output / filename).write_bytes(data)
        seen[digest] = filename
        summary["exported_images"] += 1
        summary["exported_bytes"] += len(data)
        records.append({"source": path, "status": "exported", "file": filename,
                        "bytes": len(data), "sha256": digest, **metadata,
                        "screen_correspondence": "unverified"})
        print(f"已导出 {summary['exported_images']}: {metadata['format']} {metadata['size']} ({len(data)} bytes)", flush=True)
    if len(files) > max_files and summary["scanned_files"] >= max_files:
        limits.append("max_files")
    verdict = ("cached_images_exported" if summary["exported_images"] else
               "inconclusive_scan_errors" if summary["read_errors"] else
               "inconclusive_limits" if limits or summary["oversized_images"] else
               "no_valid_images_in_scanned_files")
    return {"verdict": verdict, "summary": summary, "limits_reached": limits, "files": records}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通过 root ADB 直接导出 App 缓存中的图片文件，不使用截图或抓包。")
    parser.add_argument("--adb", default=os.getenv("ADB_PATH", ""), help="adb 可执行文件路径")
    parser.add_argument("--device", default=os.getenv("ANDROID_SERIAL", ""), help="设备序列号；多个设备时必须指定")
    parser.add_argument("--package", default="", help="App 包名；默认识别当前前台 App")
    parser.add_argument("--root", action="store_true", help="在模拟器上尝试 adb root；不会刷机")
    parser.add_argument("--check", action="store_true", help="只检查设备及 root 状态，不扫描图片")
    parser.add_argument("--include-files", action="store_true", help="同时扫描该 App 的 files 目录（部分 App 在此存图片）")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "output" / "cached_image_probe")
    parser.add_argument("--timeout", type=float, default=30, help="每次 ADB 操作的超时秒数")
    parser.add_argument("--max-files", type=int, default=1000, help="最多检查的缓存文件数")
    parser.add_argument("--max-images", type=int, default=200, help="最多导出的不同图片数")
    parser.add_argument("--max-mb", type=int, default=20, help="每个候选文件最多读取的 MiB 数")
    parser.add_argument("--max-total-mb", type=int, default=200, help="导出图片的总大小上限 MiB")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if min(args.timeout, args.max_files, args.max_images, args.max_mb, args.max_total_mb) <= 0:
        parser.error("超时和各项上限必须大于 0")
    if args.package and not PACKAGE.fullmatch(args.package):
        parser.error("--package 必须是有效的 Android 包名")
    output = args.output.resolve() / (time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    report = {"method": "root_adb_cache_files", "limitations": LIMITATIONS}
    exit_code = 1
    try:
        adb = Adb(find_adb(args.adb), args.device, args.timeout)
        adb.select_device()
        info = adb.inspect(args.root)
        report["device"] = info
        print(f"设备 {info['serial']}，Android {info['android_version']}，ADB UID={info['uid']}", flush=True)
        if not info["root_available"]:
            raise ProbeError("root_unavailable", "没有 root ADB 权限。可加 --root 重试；Google Play 镜像请换用 AOSP/Google APIs 镜像。")
        if args.check:
            report["verdict"] = "root_available"
            exit_code = 0
        else:
            try:
                import PIL  # noqa: F401
            except ImportError as exc:
                raise ProbeError("pillow_missing", "缺少 Pillow，请运行 python -m pip install Pillow。") from exc
            package, user = adb.app_scope(args.package)
            report.update(package=package, android_user=user, directories=[])
            print(f"读取 {package} 的缓存，请先在 App 中打开目标页面并等待图片加载。", flush=True)
            bases = [f"/data/user/{user}/{package}", f"/storage/emulated/{user}/Android/data/{package}"]
            files = []
            for leaf in (["cache", "files"] if args.include_files else ["cache"]):
                for base in bases:
                    directory = f"{base}/{leaf}"
                    try:
                        found, record = adb.list_files(directory)
                        files.extend(found)
                    except (OSError, UnicodeError, ProbeError, subprocess.TimeoutExpired) as exc:
                        record = {"path": directory, "status": "error", "error": str(exc)}
                    report["directories"].append(record)
            files = list(dict.fromkeys(files))
            print(f"发现 {len(files)} 个缓存文件；最多检查 {args.max_files} 个。", flush=True)
            report.update(scan_images(adb, files, output, max_files=args.max_files,
                                      max_images=args.max_images, max_bytes=args.max_mb * 1024 * 1024,
                                      max_total_bytes=args.max_total_mb * 1024 * 1024))
            if any(item["status"] == "error" for item in report["directories"]) and not report["summary"]["exported_images"]:
                report["verdict"] = "inconclusive_scan_errors"
            if not files and not any(item["status"] == "listed" for item in report["directories"]) and report["verdict"] != "inconclusive_scan_errors":
                report["verdict"] = "no_cache_directories"
            exit_code = 0 if report["summary"]["exported_images"] else 2
    except KeyboardInterrupt:
        report.update(verdict="interrupted", error="用户中断了测试；images 目录可能包含已导出的图片。")
        exit_code = 130
    except (OSError, ValueError, ProbeError, subprocess.TimeoutExpired) as exc:
        report.update(verdict=getattr(exc, "status", "probe_error"), error=str(exc))
        print(f"测试未完成: {exc}", file=sys.stderr)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果: {report['verdict']}")
    print(f"报告: {output / 'report.json'}")
    if "summary" in report:
        print(f"导出图片: {report['summary']['exported_images']}；图片目录: {output / 'images'}")
        print("缓存图片可能来自历史页面，需自行核对；没找到图片不代表无法通过网络抓取。")
    return exit_code


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    raise SystemExit(main())
