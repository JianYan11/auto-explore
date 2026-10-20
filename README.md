# auto-explore：云端 Android App 并发探索

通过 ADB 操作腾讯云 Android World sandbox，以视觉模型理解界面，记录页面、状态、动作和验证证据。多个 App 使用独立 Worker 进程和独占设备并发运行，可选独立模型生成 App 功能总结。

云实例和 ADB 隧道由 AGR 管理；仓库内的环境管理器负责连接就绪检查与 App 清理。App 的启动、退出、恢复和路径重放仍由 harness 管理。

## 安装与配置

在仓库根目录执行，使用 Python 3.10+、macOS/Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env  # 首次配置时执行；已有 .env 请保留
```

编辑 `.env` 填写模型地址、模型名和密钥。新配置推荐 `MODEL_BASE_URL`、`MODEL_NAME`、`MODEL_API_KEY`；原有 `QWEN_*` 配置继续兼容。进程环境变量优先于 `.env`，任务 JSON 中的探索选项优先于默认配置。

模型总结能力保留：`SUMMARY_MODEL=` 表示仅采集证据；填写与执行模型不同的模型名即可启用。总结复用模型服务地址，`SUMMARY_API_KEY` 可独立配置，未配置时使用执行模型的 Key。任务中填写 `"summary_model": ""` 会显式关闭该任务的总结。可运行 `python summarize_app.py --help` 从旧图谱补建功能目录。

`.tools/` 存放本机下载的 AGR 和 Android platform-tools，均不提交 Git。ADB 按 `ADB_PATH` → `.tools/platform-tools/adb` → PATH 查找；没有默认设备，必须通过 `--device` 或 `ANDROID_SERIAL` 指定当前云沙箱地址。

## 并发探索

先按 [云沙箱操作指南](docs/cloud-sandbox.md) 创建实例并连接 ADB。将以下端口占位符替换为 `adb devices -l` 中的真实地址：

```bash
python explore_batch.py env add --device 127.0.0.1:PORT_A
python explore_batch.py env add --device 127.0.0.1:PORT_B
python explore_batch.py env add --device 127.0.0.1:PORT_C
python explore_batch.py enqueue examples/cloud_tasks.json
python explore_batch.py run --concurrency 3
python explore_batch.py status
```

示例包含 Tasks、Markor、Draw 三个 App，每个探索 3 步，继承 `.env` 的总结配置。使用前确认这三个 App 已安装到所有候选设备；未固定设备端口，重新连接后重新登记设备，并为新批次选择新的 `--db`。

GLM 示例关闭 embedding 语义检索，保留视觉判重；若启用语义检索，需要额外配置支持 embedding 的模型服务。

单 App 调试：

```bash
python explore_android_app.py --device 127.0.0.1:PORT_A --package org.tasks \
  --max-steps 3 --no-semantic-dedup --output output/single-task
```

## 目录与结果

```text
auto-explore/
  explore_batch.py          # 任务队列 CLI
  explore_android_app.py    # 单任务探索 CLI
  summarize_app.py          # 已有图谱的功能目录重建
  visualize_graph.py        # 离线图谱可视化导出
  orchestration/            # 任务、调度、Worker、SQLite、设备适配器与锁
  harness/                  # 探索核心、证据、恢复、模型调用与总结
  visualization/            # 图谱查看器模板
  examples/                 # 可复用任务清单
  docs/                     # 架构、云沙箱操作与详细行为契约
  tests/                    # 离线单元及进程集成测试
  .tools/                   # 忽略：本机 AGR / ADB
  .venv/                    # 忽略：Python 依赖
  .env                      # 忽略：本机模型配置
  output/                   # 忽略：队列、日志、图谱、截图和诊断证据
```

默认队列为 `output/batch/queue.sqlite3`，每次执行的结果位于 `artifacts/<task_id>/<attempt_id>/`。`run` 在队列排空后退出，云端常驻实例需另行释放。

之前的三 App 联调、诊断证据和图谱查看器保留在 `output/glm-smoke-20261020/`。导出或打开已有图谱不需要运行 sandbox：

```bash
python visualize_graph.py --report output/glm-smoke-20261020/verification.json \
  --output output/glm-smoke-20261020/visualizations
python -m http.server 8000 --bind 127.0.0.1 --directory output/glm-smoke-20261020
```

浏览器打开 <http://127.0.0.1:8000/visualizations/index.html>。

## 开发与文档

```bash
python -m unittest discover -s tests -t . -v
```

测试使用模拟设备、模型客户端和真实子进程，不需要云实例或模型配额。

- [架构与旧功能迁移](docs/architecture.md)
- [云沙箱创建、连接与释放](docs/cloud-sandbox.md)
- [队列、任务格式、故障恢复](docs/batch-exploration.md)
- [Harness 页面、状态和总结契约](docs/harness-reference.md)
