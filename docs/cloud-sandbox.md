# 云端 Android World 操作指南

在仓库根目录执行。AGR 管理云实例和连接，auto-explore 管理探索任务；创建环境工具本身不等于创建实例。一个工具可作为多个实例的模板。下列创建、删除命令由操作者按需执行。

## 本机工具与凭据

已下载的可执行文件迁到 `.tools/bin/agr` 和 `.tools/platform-tools/adb`，其他机器可自行安装 AGR 及 Android platform-tools 后放入 PATH。仓库不包含本地 AVD 安装或启动流程。

```bash
export PATH="$PWD/.tools/bin:$PWD/.tools/platform-tools:$PATH"
export ADB_PATH="$PWD/.tools/platform-tools/adb"
agr --help
adb version
```

AGR 使用 `~/.agr/config.toml` 中现有凭据；模型使用仓库 `.env`，两者独立。迁移目录不需要重新填写密钥。新机器按所安装 AGR 版本的帮助和腾讯云接入指引初始化凭据，不把密钥写入任务 JSON 或命令参数。

## 创建三个实例

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

`examples/cloud_tasks.json` 不固定 serial，App 必须安装到所有可能分配的设备。如需绑定，任务中填写 `devices` 为当前连接地址。`run` 排空后退出，实例仍存在；环境 `reset` 仅清理目标 App，不释放云资源。

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
