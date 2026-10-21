# 单机多 App 并发探索

在现有 `explore_android_app.py` 外增加持久化任务队列、调度器、Worker 和云沙箱环境管理，支持自动创建实例及 ADB 连接，也支持已有连接。
调度单位是一次完整的 App 探索；`harness/` 内部仍负责页面、状态、动作和恢复路径。

## 快速运行

在仓库根目录使用 Python 3.10+，支持 macOS/Linux。队列、状态查询和离线测试只依赖标准库；
实际探索继续使用原仓库的 `openai`、`Pillow` 和模型配置。

如果还没有 Python 环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

推荐使用托管模式。先配置 AGR 凭据及一个 Android World 工具模板，目标 App 必须已在镜像中安装：

```bash
python explore_batch.py enqueue examples/cloud_tasks.json
python explore_batch.py run --tool-id sdt-YOUR_TOOL_ID --region ap-guangzhou --concurrency 2
python explore_batch.py status
```

复制 `examples/cloud_tasks.json` 并替换为实际包名。示例继承 `.env` 中的总结模型设置，保留图谱和探索证据；
需要功能目录时填写与执行模型不同的 `summary_model`。

```bash
python explore_batch.py enqueue examples/cloud_tasks.json
python explore_batch.py run --tool-id sdt-YOUR_TOOL_ID --concurrency 2 --adb /absolute/path/to/adb
python explore_batch.py status
```

探索和离线总结入口会在导入 harness 前加载仓库根目录的可选 `.env` 文件；已有进程环境变量优先，
子进程继承配置。`.env` 存在时需要安装 `python-dotenv`；没有 `.env` 的队列操作仍只需标准库。
从 `.env.example` 复制配置并在本机填写密钥，不要提交 `.env`。
推荐使用 `MODEL_API_KEY`、`MODEL_BASE_URL`、`MODEL_NAME`；模型密钥兼容原有的 `QWEN_API_KEY`、`MODELSCOPE_API_KEY` 或 `OPENAI_API_KEY`，
服务地址可用原有的 `QWEN_BASE_URL` 等配置。
任务 JSON 不接受 API Key，也不接受任意命令或任意额外 CLI 参数。

### GLM 与 Android World 沙箱

`.env.example` 使用智谱官方 `glm-5.3-flash`。`MODEL_API_PROFILE=auto` 根据模型名识别
GLM/Qwen；`openai` 强制仅发送通用参数。GLM-5.3 使用开启思考与
`MODEL_REASONING_EFFORT=low`（也可为 `high`、`max`），不发送 Qwen 的 `enable_thinking`。
其他 GLM 版本保留服务端默认思考配置，不假定支持 5.3 的参数。
GLM-5.3 单次输出上限至少 8192 token，容纳复杂页面 JSON；截断结果与纯思考内容不会用于执行动作。
GLM-5.3 请求依据：[官方模型文档](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)。
`SUMMARY_MODEL=` 或未设置时不调用独立总结模型；填写模型名即可启用总结能力。

`examples/cloud_tasks.json` 是三 App、每个 3 步的并发联调清单，关闭 embedding 语义检索，保留视觉判重和图谱，总结模型继承 `.env`。
托管模式默认可分配到任意匹配工具的逻辑容量槽位，`devices` 留空。外部设备模式若填写 `devices`，使用当前 ADB 隧道地址，重新连接时端口可能改变。所有 App 必须已安装。

```bash
python explore_batch.py --db output/glm-batch/queue.sqlite3 enqueue examples/cloud_tasks.json
python explore_batch.py --db output/glm-batch/queue.sqlite3 run --tool-id sdt-YOUR_TOOL_ID \
  --concurrency 3 --adb "$PWD/.tools/platform-tools/adb"
python explore_batch.py --db output/glm-batch/queue.sqlite3 status
```

同一清单每次 `enqueue` 都会创建新任务，不要为查询进度重复入队。
托管模式每个 Attempt 自动创建独占实例、连接 ADB、探索、回收。队列排空后退出；即使使用常驻工具也显式释放实例。状态库保留资源记录，回收失败时运行 `env cleanup`。完整参数和故障处理见 [云沙箱指南](cloud-sandbox.md)。

真实 GLM 联调中，10 步状态探索曾在 Draw 的状态恢复阶段停止为 `blocked`，
同批 Tasks/Markor 长任务及 Draw 连续探索复测由操作者取消，诊断证据均保留。
短程清单用 3 步连续探索验证调度和结果闭环，不代表完整功能覆盖或恢复算法已解决；
`blocked` 仍会由 Worker 标为失败，不能以进程退出码 0 当作成功。

默认数据库是 `output/batch/queue.sqlite3`，产物在数据库旁边的 `artifacts/`。
自定义队列时，所有命令都把全局 `--db` 放在子命令之前：

```bash
python explore_batch.py --db output/my-batch/queue.sqlite3 enqueue my-tasks.json
python explore_batch.py --db output/my-batch/queue.sqlite3 run --tool-id sdt-YOUR_TOOL_ID --concurrency 1 --adb adb
```

`run` 会处理已有队列和运行期间新入队的任务，队列排空后退出。它不是永久驻留服务。
`status`、`enqueue`、`cancel` 可以在另一个终端运行。

## 任务格式

清单是一个非空 JSON 数组；整个清单校验成功才会一次性入队。

```json
[
  {
    "package": "org.tasks",
    "devices": ["127.0.0.1:PORT_A"],
    "priority": 10,
    "timeout_seconds": 1800,
    "max_attempts": 2,
    "retry_delay_seconds": 5,
    "resource_key": "test-account-07",
    "options": {
      "max_steps": 200,
      "screenshot_delay": 2,
      "summary_model": "",
      "memory_scope": "tasks-v1-test"
    }
  }
]
```

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `package` | 必填 | 目标 App 包名 |
| `devices` | `[]` | 可使用的环境 ID；托管模式留空，外部设备模式可填 serial |
| `priority` | `0` | 数字越大越先分配，同优先级按入队顺序 |
| `timeout_seconds` | `1800` | 从 Worker 开始准备环境到探索结束的时间预算 |
| `max_attempts` | `1` | 包括首次执行的自动尝试次数上限 |
| `retry_delay_seconds` | `5` | 可重试失败后的固定等待时间 |
| `resource_key` | `""` | 非空且相同的资源键不能同时执行，可用于隔离共享账号 |
| `options` | `{"max_steps":200}` | 允许的单任务探索配置 |

`resource_key` 由提交者声明，系统不会自动识别 App 实际使用的账号。
`memory_scope` 仍然只是历史经验范围，不提供账号或环境隔离。

支持的 `options`：

- 字符串：`activity`、`model`、`summary_model`、`embedding_model`、`memory_scope`。
- 非负整数：`max_steps`、`max_depth`、`coverage_checkpoint_steps`、`verification_retries`、`replay_attempts`。
- 正整数：`max_pages`、`max_states_per_page`、`max_actions_per_state`、`continuous_steps`、`max_replay_steps`、`coverage_checkpoint_top_k`。
- 非负秒数：`screenshot_delay`、`summary_budget_seconds`。
- 正秒数：`summary_timeout`、`embedding_timeout`。
- 布尔值：`no_semantic_dedup`、`page_exploration`、`no_save_analysis`、`no_back`。

未知选项直接拒绝。设备、ADB、输出目录和日志由外层管理，不能被任务覆盖。
`max_steps: 0` 仍表示不限制步数，但外层总时间预算继续生效。

## 执行与结果

```text
Task: queued → active → succeeded / failed / cancelled
                    └→ retry_wait → active

Attempt: preparing → running → finalizing → succeeded / failed / cancelled
         └──────────── 调度器意外退出 ────────────────→ lost

Environment: available → busy → available
                          └→ quarantined → 人工恢复 → available
```

一个 Task 可以有多个 Attempt，每个 Attempt 独占一台设备，结果目录永不复用：

```text
artifacts/<task_id>/<attempt_id>/
  assignment.json    # 不含密钥的任务配置
  environment.json   # 稳定环境 ID、本次 InstanceId 和 ADB serial
  worker.log         # 探索子进程 stdout/stderr，包括初始化错误
  app_graph.json     # 原探索器产物
  runs/              # 原探索器的证据、截图与内部 run_id
  result.json        # 外层结果、终止原因、设备清理错误
```

成功要求退出码为 0、图谱包名匹配、有有效根页面和 run_id，且终止原因为
`frontier_exhausted` 或 `budget_limited`。预算内正常结束可以算成功，但不表示穷尽所有功能。
`blocked`、损坏图谱和退出码非零都不会被误判为成功。
总结模型是否完成不是这版的成功条件；需要时查看原探索产物。

### 离线查看图谱

导出已有图谱，不连接设备或调用模型：

```bash
python visualize_graph.py --report output/glm-smoke-20261020/verification.json \
  --output output/glm-smoke-20261020/visualizations
```

也可以传入一个或多个 `app_graph.json` 路径代替 `--report`。
打开输出目录的 `index.html`，可切换 App、页面图/状态图，点击节点查看截图，
点击动作查看前后截图及执行/到达验证结果。截图内嵌，页面不依赖外部网络；
`manifest.json` 保存源图谱路径和缺失截图提示。导出器只读取各图谱所在运行目录内的截图。
页面图保留同页回环；状态图只展示动作端点引用的状态，不补画未记录的恢复路径。
页面内的未连接观测截图可以在节点详情中选择查看。

普通进程失败、无效图谱和总预算超时可以按 `max_attempts` 重试；参数错误（退出码 2）、
缺少 App、设备准备失败和探索阻塞不自动重试。每次重试重新启动探索，不恢复旧页面队列。
图谱校验与产物发布成功后，设备清理失败不会撤销该探索结果，但设备会被隔离。

命令退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 队列已排空，状态库中没有失败或取消任务 |
| `1` | 状态库中有失败或取消任务 |
| `2` | 输入/运行错误、没有可用兼容环境，或存在未回收云资源 |
| `130` | 收到 SIGINT/SIGTERM，已停止本轮运行 |

`run` 返回 JSON 包含任务、环境状态计数和 `cleanup_pending` 数量；详细记录用 `status` 查询。探索成功但回收失败仍保留成功图谱，同时以退出码 2 提示资源欠账。

## 环境策略与故障恢复

托管模式为每次尝试创建全新实例，结束后删除；它不复用设备上的登录或数据，也不重置服务端账号状态。启动时先回收该数据库的遗留实例，或使用 `env cleanup` 显式重试。旧版队列会原地迁移，保留任务历史。

下述 `restart` / `clear-data` 策略用于外部设备模式：不传 `--tool-id`，不设置 `CLOUD_SANDBOX_TOOL_ID`，通过 `env add` 登记已连接的云设备。默认 `restart` 策略在任务结束时 force-stop 目标 App，保留登录和 App 数据。
它不承诺干净基线；重试可能看到上一轮修改过的数据。

如果明确需要每次清空目标 App 的本地数据，可以登记：

```bash
python explore_batch.py env add --device 127.0.0.1:PORT_A --reset-policy clear-data
```

该策略在任务前后执行 `pm clear <package>`，会删除目标 App 的本地登录和数据。
它不会重置其他 App、系统设置、SD 卡或服务端数据。当前不安装 APK、不负责登录；外部设备模式也不会创建或销毁操作者的实例。

正常取消：

```bash
python explore_batch.py cancel <task_id>
```

排队任务立即取消；准备中/运行中任务由 Worker 停止。进入最终结果提交和清理后，取消不再改变该任务。
对 `run` 按 Ctrl+C 或发送 SIGTERM，会取消当前执行，尚未分配的任务保留在队列。
探索进程先收到 SIGINT，默认允许 10 秒收尾，然后升级为 SIGTERM/SIGKILL 并清理其进程组。
`--stop-grace` 可调整 SIGINT 收尾时间。超时后的进程终止和设备清理有额外的有界耗时。

设备准备或清理失败时进入 `quarantined`。重启调度器时，遗留活跃 Attempt 标为 `lost`，
相关任务标为失败、设备隔离；不会仅凭旧心跳超时把设备重新分配。

修复连接、确认旧探索进程及其相关子进程已停止后，先停止当前 `run`，再执行：

```bash
python explore_batch.py env recover --device 127.0.0.1:PORT_A --adb /absolute/path/to/adb
python explore_batch.py retry <task_id>
python explore_batch.py run --concurrency 2 --adb /absolute/path/to/adb
```

`env recover` 必须取得设备锁，再检查 Android 已启动并应用登记的清理策略。
还活着的探索子进程持有继承的设备锁时，恢复会拒绝执行。重新 `env add` 不会解除隔离。
`retry` 仅接受失败/取消任务，保留旧 Attempt 和证据，显式允许一次新的执行；自动重试上限仍按累计 Attempt 数判断。

## 模块边界与单机决策

| 文件 | 职责 |
| --- | --- |
| `models.py`、`errors.py` | 与框架无关的任务、分配、结果、校验规则和错误契约 |
| `ports.py` | 状态库和环境管理接口，可注入替代实现 |
| `store.py` | SQLite 持久化队列、事务领取、状态迁移、资源唯一约束 |
| `scheduler.py` | 有界并发、优先级、设备匹配、任务派发、启动时巡检 |
| `worker.py` | 准备、执行、心跳、结果校验、发布与清理 |
| `process_runner.py` | 独立进程组、取消、超时及有界终止 |
| `environments.py` | 已连接云沙箱的 ADB 就绪检查与 App 重置 |
| `cloud_environments.py` | 托管实例的准备、分配绑定、补偿回收和重启巡检 |
| `agr.py` | 腾讯云 AGR 控制面、ADB 隧道及结构化结果适配 |
| `locks.py` | 协调器锁和可传递给探索子进程的设备锁 |
| `cli.py` | 参数解析与具体组件组装 |

单机版选择 SQLite WAL，队列、执行记录与环境占用在同一个本地数据库里提交，不增加服务依赖。
`BEGIN IMMEDIATE` 串行化短分配事务，数据库唯一索引保证同一任务、同一设备和同一非空资源键的独占。
ADB、模型调用和子进程运行都不在数据库事务中进行。

同一数据库只允许一个协调器；同一用户启动的不同队列也共享设备锁目录
`~/.cache/auto-explore/device-locks/`。锁覆盖使用相同 ADB 服务标识的这些批量程序，
不限制手动 ADB、其他工具或其他用户。运行期间不要让外部程序同时操控已分配设备，也不要删除锁文件。
不同批次应使用一致的 ADB 服务配置；这版不管理自定义多 ADB 服务拓扑。

Worker 线程仅监督独立 Python 进程，不共享 `AndroidExplorer` 实例或进程级日志重定向。
心跳用于观测，单机恢复依赖协调器锁、设备锁和持久化记录，不实现分布式租约。
模型侧暂以 `--concurrency` 限制同时运行的探索数量，没有全局 RPM/TPM 限流器。
不包含 Docker、多机、自动扩容、远端产物存储或原探索器的磁盘断点续跑。

## 离线验证

```bash
python -m unittest discover -s tests -t . -v
```

测试使用模拟 ADB 和真实 Python 子进程，不调用模型、不连接设备。
覆盖多任务重叠、设备独占、并发上限、优先级和资源互斥、任务取消、超时进程组清理、
重试、无效图谱、离线设备隔离、清理失败、数据库恢复、继承设备锁、协调器 SIGKILL 与 CLI SIGTERM。
真实云沙箱和模型联调仍需在具备上述运行条件的机器上执行。
