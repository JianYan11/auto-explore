# 保存 Android 当前界面中的可见图片区域

`probe_screen_images.py` 支持两条路径：

1. **UI XML + 截图**：定位 `ImageView` 等候选控件，裁剪其屏幕区域；仅当 XML 属性实际暴露 HTTP(S) 图片 URL 时，才尝试下载并验证图片字节。即使下载成功，也不能自动证明它与当前屏幕上的图片一一对应。
2. **仅截图 + 手动区域**：适用于抖音等播放页，UIAutomator 可能因画面持续变化而报 `could not get idle state`。不读取 XML，只保存屏幕上指定区域的像素。输出永远不是 App 的原始图片文件。

需要 Python 3.10+、Pillow，以及已连接的 ADB 模拟器。坐标使用设备原始分辨率；可通过 `adb shell wm size` 查询。

```powershell
# 对当前前台页面寻找可见图片控件与 URL
python probe_screen_images.py --device emulator-5554 --package com.example.app

# 对动画/短视频页面保存指定屏幕区域，可重复传入 --region
python probe_screen_images.py --device emulator-5554 --screenshot-only --no-download --region 925,2010,1075,2180
```

每次运行都会创建独立目录，包含 `screen.png`、`annotated.png`、`crops/` 和 `report.json`。仅截图模式不生成 `screen.xml`。`report.json` 的 `kind: screenshot_crop_not_original` 是刻意标记：裁剪图可能含圆角、遮罩、文字、视频帧或压缩损失，不应当作素材原文件。

若必须取 App 缓存里的原始图片字节，可运行 `probe_cached_images.py --check` 检查 root ADB 条件，详情见 `CACHED_IMAGE_PROBE.md`。Google Play 系统镜像通常不允许 root；商业 App 通常也不允许 `run-as`。即使能读取缓存，也要另做视觉匹配才能证明缓存图属于当前页面。

测试：`python -m pytest tests/test_screen_image_probe.py -q`。
