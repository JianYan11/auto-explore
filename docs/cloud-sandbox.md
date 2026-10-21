# 云端 Android World 操作指南

在仓库根目录执行。创建环境工具本身不等于创建实例；一个工具可作为多个实例的模板。推荐由 auto-explore 环境管理器通过 AGR 自动管理实例与 ADB 连接。下方也保留手动连接模式供调试使用。

## 本机工具与凭据

已下载的可执行文件迁到 `.tools/bin/agr` 和 `.tools/platform-tools/adb`，其他机器可自行安装 AGR 及 Android platform-tools 后放入 PATH。仓库不包含本地 AVD 安装或启动流程。

```bash
export PATH="$PWD/.tools/bin:$PWD/.tools/platform-tools:$PATH"
export ADB_PATH="$PWD/.tools/platform-tools/adb"
agr --help
adb version
```

AGR 使用 `~/.agr/config.toml` 中现有凭据；模型使用仓库 `.env`，两者独立。迁移目录不需要重新填写密钥。新机器按所安装 AGR 版本的帮助和腾讯云接入指引初始化凭据，不把密钥写入任务 JSON 或命令参数。

## 自动创建、连接、探索与释放（推荐）

配置一个已有 Android World 工具 ID；凭据继续由 AGR 读取，不写入任务清单：

```bash
BATCH_DB=output/cloud-managed/queue.sqlite3
python explore_batch.py --db "$BATCH_DB" enqueue examples/cloud_tasks.json
python explore_batch.py --db "$BATCH_DB" run \
  --tool-id sdt-YOUR_TOOL_ID --region ap-guangzhou --concurrency 3
python explore_batch.py --db "$BATCH_DB" status
```

也可在仓库 `.env` 配置 `CLOUD_SANDBOX_TOOL_ID`、`CLOUD_SANDBOX_REGION`，命令行选项优先。AGR 按 `--agr` / `AGR_PATH` → `.tools/bin/agr` → PATH 查找。

不需要提前执行 `instance create`、`mobile connect` 或 `env add`。每个 Attempt 领取一个逻辑环境槽位，创建独占实例，等待 `RUNNING`，建立本地 ADB 隧道，检查 Android 启动和目标包，然后运行探索。目标 App 必须存在于云镜像中；当前不安装 APK、不自动登录。任务的 `devices` 留空，不能填写上次的临时 ADB 端口。

完成、取消、探索失败或超时后均断开隧道并删除该实例，确认 `STOPPED` 后才归还槽位。常驻工具也走显式删除；不会删除工具模板。每次重试创建全新实例，不继承上次实例的数据。`--concurrency` 限制整个创建、探索、回收过程；空队列不会申请云实例。

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| `--provision-timeout` | 180 秒 | 创建、等待 RUNNING 和连接隧道的上限，也受任务总预算限制 |
| `--boot-timeout` | 90 秒 | ADB Android 就绪检查上限 |
| `--cloud-command-timeout` | 30 秒 | 单次 AGR 命令上限 |
| `--cleanup-timeout` | 120 秒 | 每个实例的额外回收预算，不随任务取消中断 |
| `--stop-grace` | 10 秒 | 探索进程 SIGINT 收尾预算 |

创建时显式传递 TTL 为任务总预算加回收预算；限时工具可用它兜底，常驻工具忽略 TTL，仍依赖显式回收。本机或网络彻底中断时无法保证即时回收常驻实例；重启后的巡检和持久化记录用于补偿。

### 回收失败与异常退出

`status` 的 `cloud_resources` 保留每个 Attempt 的创建意图、实例 ID、本次 serial 和状态。`released` 表示云端与本机隧道均已确认回收；其他状态是未清理资源。`run` 若留有欠账，返回退出码 2，即使图谱本身有效。

```bash
python explore_batch.py --db "$BATCH_DB" env cleanup
python explore_batch.py --db "$BATCH_DB" status
```

`env cleanup` 只处理该数据库拥有的云资源。下次托管 `run` 也先巡检回收，成功后解除环境隔离；不会自动重跑遗失的任务，需要时再用 `retry <task_id>`。仍运行的旧探索进程持有锁时，先让其结束，再重试回收，不删除锁文件。

创建请求发出后先等待该有界命令返回实例 ID，再响应取消并回收；网络中断导致响应丢失时按工具 ID 和 `auto-explore-attempt` 元数据找回实例，避免回收过程中再次创建资源。如果无法唯一确认则保留 `cleanup_pending`，需在腾讯云和 AGR 诊断中核实该 Attempt 的创建结果，不能把查询失败当作“没有资源”。

适配器兼容 AGR 0.7.0 的 ADB 文本混入 JSON，以及 macOS 带空格路径导致的隧道退出问题。正常情况使用官方 disconnect；若旧版失败，仅在操作系统确认 PID 的可执行路径和完整命令确实属于该实例的 AGR 隧道后才停止它，并再次核对注册表。无法核验身份时保留本地连接欠账，云端仍尝试停止；不终止未验证身份的 PID，不修改其他实例连接。

SQLite 从旧版队列原地增加云资源表和环境 provider 字段，保留原任务与 Attempt 历史；无需重建旧数据库。请保留该数据库直至欠账全部释放。

## 手动创建三个实例（调试与外部设备模式）

将工具 ID 替换为控制台实际 ID，每次执行会产生一个新实例：

```bash
CLOUD_TOOL_ID=sdt-YOUR_TOOL_ID
for index in 1 2 3; do
  agr instance create --tool-id "$CLOUD_TOOL_ID" --wait -o json
done
agr instance list --tool-id "$CLOUD_TOOL_ID" -o json
```

确认返回的三个 InstanceId 状态为 RUNNING，然后逐个连接：

```bash
agr instance mobile connect INSTANCE_ID_A -o json
agr instance mobile connect INSTANCE_ID_B -o json
agr instance mobile connect INSTANCE_ID_C -o json
adb devices -l
```

记录各实例与本次 `127.0.0.1:PORT` 的对应关系。ADB serial 不等于 InstanceId；端口会随重连改变，不能写成固定仓库配置。隧道需在整个探索期间存活。

查看预装 App，再启动并截图验证：

```bash
adb -s 127.0.0.1:PORT_A shell pm list packages -3
adb -s 127.0.0.1:PORT_A shell monkey -p org.tasks -c android.intent.category.LAUNCHER 1
mkdir -p output/manual-check
adb -s 127.0.0.1:PORT_A exec-out screencap -p > output/manual-check/tasks.png
```

需要自行安装 APK 时，可使用 `adb -s SERIAL install path/to/app.apk`。App 的系统版本和 ABI 等兼容性由所选云镜像决定；先验证安装与启动，再入队。

## 登记并运行

为新一轮连接选择新的批次目录，使用当前端口登记设备：

```bash
BATCH_DB=output/cloud-batch/queue.sqlite3
python explore_batch.py --db "$BATCH_DB" env add --device 127.0.0.1:PORT_A
python explore_batch.py --db "$BATCH_DB" env add --device 127.0.0.1:PORT_B
python explore_batch.py --db "$BATCH_DB" env add --device 127.0.0.1:PORT_C
python explore_batch.py --db "$BATCH_DB" enqueue examples/cloud_tasks.json
python explore_batch.py --db "$BATCH_DB" run --concurrency 3
python explore_batch.py --db "$BATCH_DB" status
```

该模式运行 `run` 时不要指定 `--tool-id`，并确保未设置 `CLOUD_SANDBOX_TOOL_ID`（可用 `--tool-id ''` 覆盖）。`examples/cloud_tasks.json` 不固定 serial，App 必须安装到所有可能分配的设备。如需绑定，任务中填写 `devices` 为当前连接地址。外部模式只清理 App，排空后云实例仍存在，需要操作者手动释放。

## 释放

确认探索已结束，先断开本次三个实例的隧道，再删除实例：

```bash
agr instance mobile disconnect INSTANCE_ID_A -o json
agr instance mobile disconnect INSTANCE_ID_B -o json
agr instance mobile disconnect INSTANCE_ID_C -o json
agr instance delete INSTANCE_ID_A INSTANCE_ID_B INSTANCE_ID_C --yes --wait -o json
agr instance list --tool-id "$CLOUD_TOOL_ID" -o json
adb devices -l
```

只释放对应的实例，不删除用于后续创建的工具模板。若实例已结束导致 disconnect 报错，先用 `agr instance mobile list -o json` 核对记录和隧道，不批量终止其他连接。

## 腾讯云接口依据

- [启动实例](https://cloud.tencent.cn/document/product/1814/132322)：工具模板、异步创建与状态查询。
- [实例生命周期](https://cloud.tencent.cn/document/product/1814/132337)：仅 RUNNING 可用，STOPPED 为终态。
- [Mobile 沙箱 ADB](https://cloud.tencent.cn/document/product/1814/132412)：本机隧道、连接、断开与结构化返回。
- [停止与超时](https://cloud.tencent.com/document/product/1814/132338)：显式删除、常驻实例与 TTL 边界。
- [AGR 官方实现](https://github.com/TencentCloudAgentRuntime/ags-cli)：`--client-token`、Attempt 元数据及 `agr.v1` JSON 契约；适配器测试覆盖本机 AGR 0.7.0 的返回格式。
