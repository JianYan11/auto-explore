# Android cached image probe

`probe_cached_images.py` tests whether actual image files can be retrieved from an
app's on-device cache. It reads files through root ADB, validates the image data,
and writes the original bytes without re-encoding. It does not capture screenshots
or intercept HTTPS. It needs Python 3.10+ and Pillow, with no model API key.

## Quick start (Windows)

Start an Android Studio AOSP or Google APIs emulator. Standard Google Play images
reject `adb root`. Install the target app, open the relevant screen and wait for
its images to load. Run these commands from `auto-explore`:

```powershell
python -m pip install Pillow
python probe_cached_images.py --check --root
python probe_cached_images.py --root
```

The last command detects the foreground app. To select a device and package
explicitly (replace both example values):

```powershell
adb devices
python probe_cached_images.py --root --device emulator-5554 --package com.example.app
```

For this workspace's existing Conda environment:

```powershell
conda run --no-capture-output -n mobilegym python probe_cached_images.py --root
```

Use `--adb "D:\Android\SDK\platform-tools\adb.exe"` if ADB is not on PATH.
If more than one device is connected, specify `--device`. `ANDROID_SERIAL` and
`ADB_PATH` are also supported. `--check` alone is a read-only root status check;
`--root` explicitly asks to restart the emulator's ADB daemon as root. The script
does not attempt root on a physical device or install a root framework. Root ADB
remains enabled afterwards; use `adb -s <serial> unroot` to turn it off if desired.

## Results

Each run creates a new directory under `output/cached_image_probe/`:

```text
<timestamp>_<unique-id>/
  report.json
  images/
    <sha256>.jpg
    <sha256>.png
    <sha256>.webp
```

The report includes Android version, root status, package, Android user, scanned
directories, counts, limits, per-file failures, and each image's source path,
dimensions, SHA-256 and local filename. Identical files are stored once.
Extensionless and hashed filenames are supported; recognition uses content,
not the filename. PNG, JPEG, WebP, GIF, BMP, TIFF, ICO and AVIF are recognized,
subject to the codecs supported by the installed Pillow version.

| Result | Meaning |
| --- | --- |
| `cached_images_exported` | At least one validated cached image was exported. |
| `root_available` | `--check` succeeded; no image capture was attempted. |
| `no_device` | Start or connect an emulator, then retry. |
| `root_unavailable` | Try `--root`, or use a root-capable emulator image. |
| `no_cache_directories` | No cache directory was found in the default scope. |
| `no_valid_images_in_scanned_files` | None of the scanned files was a supported, valid image. |
| `inconclusive_scan_errors` | Read/listing errors prevented a conclusive negative result. |
| `inconclusive_limits` | A scan/size bound was reached before any image could be exported. |

Exit codes: `0` = image(s) exported or root check succeeded; `1` = setup failure;
`2` = scan completed without an export; `130` = interrupted. On interruption,
already-written images are retained, but per-file results may be incomplete.

The default directories are the selected Android user's internal app `cache`
and external app `cache`. Some apps keep image caches inside `files`; expand
the scope explicitly if needed:

```powershell
python probe_cached_images.py --root --include-files --max-files 3000
```

Only validated image files are exported, including in `--include-files` mode.
The script never clears app data, installs certificates, changes proxies,
restarts the app, or navigates the screen. Cached files can disappear while it runs.

Defaults: inspect up to 1,000 files; export up to 200 unique images; read at most
20 MiB plus one sentinel byte per candidate; export at most 200 MiB in total.
Every file first receives a 64-byte header check. Use `--max-files`, `--max-images`,
`--max-mb`, `--max-total-mb` and `--timeout` to change the bounds. Limits and errors
are reported; the script does not claim exhaustive coverage.

## Interpretation and limitations

Successful extraction proves that cached image files are accessible. These may
be server thumbnails or app-processed images, and may belong to earlier pages.
Their correspondence to the current screen and original upload resolution are
not verified. This does not retrieve image URLs.

A negative result does **not** prove that image capture is impossible. Images
may be memory-only, packed in the APK, stored outside the selected directories,
inside databases, encrypted, or wrapped with custom headers. Those cases are
not extracted. HTTPS response capture remains a separate approach; this probe
does not test certificate pinning or HTTPS decryptability.

Offline checks:

```powershell
python -m pytest tests/test_cached_image_probe.py -q
```

References: [Android emulator root support](https://developer.android.com/studio/run/managing-avds#system-image)
and [Android app-specific storage](https://developer.android.com/training/data-storage/app-specific).
