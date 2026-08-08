# 数据发送器模块

本模块模拟一台设备上的三个独立数据发送器。每个发送器固定负责一个轴承，独立申请调度、建立 MQTT 连接并发送 80 个 50 ms 数据包。

## 1. 模块结构

```text
machine_01
├── sender_01 -> bearing_01 -> 独立 HTTP 调度请求 -> 独立 MQTT 连接
├── sender_02 -> bearing_02 -> 独立 HTTP 调度请求 -> 独立 MQTT 连接
└── sender_03 -> bearing_03 -> 独立 HTTP 调度请求 -> 独立 MQTT 连接
```

三个发送器在同一个 Python 程序中并行运行，但拥有不同的 `sender_id`、任务计数器、MQTT `client_id`、待确认队列和重试统计。它们可以连接同一个 Mosquitto Broker，也可以通过不同网络代理端口模拟不同链路。

## 2. 拉取和安装

### 2.1 拉取代码

```powershell
git clone https://github.com/sun-oom/Intelligent-Maintenance-Collaboration.git
cd Intelligent-Maintenance-Collaboration\cloud_edge_project\sender_module
```

如果已经克隆过仓库：

```powershell
git pull origin main
cd cloud_edge_project\sender_module
```

### 2.2 创建 Python 环境

要求 Python 3.10 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### 2.3 准备 Mosquitto

默认配置连接本机 `127.0.0.1:1883`。启动后可检查端口：

```powershell
Test-NetConnection 127.0.0.1 -Port 1883
```

`TcpTestSucceeded` 为 `True` 表示 Broker 可以连接。Mosquitto 是 MQTT Broker，`paho-mqtt` 是发送器使用的 Python MQTT 客户端库，两者不是同一个程序。

## 3. 配置

配置文件为 `config/local.json`。三个发送器分别配置调度器地址和 MQTT 地址，便于插入不同的网络模拟链路。

```json
{
  "device_id": "machine_01",
  "senders": [
    {
      "sender_id": "sender_01",
      "bearing_id": "bearing_01",
      "scheduler_url": "http://127.0.0.1:8003/scheduler/decide",
      "mqtt_host": "127.0.0.1",
      "mqtt_port": 1883
    },
    {
      "sender_id": "sender_02",
      "bearing_id": "bearing_02",
      "scheduler_url": "http://127.0.0.1:8003/scheduler/decide",
      "mqtt_host": "127.0.0.1",
      "mqtt_port": 1883
    },
    {
      "sender_id": "sender_03",
      "bearing_id": "bearing_03",
      "scheduler_url": "http://127.0.0.1:8003/scheduler/decide",
      "mqtt_host": "127.0.0.1",
      "mqtt_port": 1883
    }
  ],
  "scheduler_timeout_seconds": 2.0,
  "schedule_max_retries": 2,
  "mqtt_keepalive_seconds": 30,
  "qos": 1,
  "retain": false,
  "puback_warning_timeout_ms": 500,
  "packet_delivery_timeout_ms": 1000,
  "max_publish_retries": 2,
  "pending_queue_max_packets": 80,
  "task_duration_ms": 4000,
  "packet_interval_ms": 50,
  "expected_packet_count": 80,
  "log_dir": "../runtime/logs",
  "state_dir": "../runtime/state"
}
```

`qos` 固定为 `1`，表示 Broker 至少接收一次；`retain` 固定为 `false`，避免后来订阅的边缘节点误把旧数据当成新任务。

## 4. 启动

本地测试可分别打开三个终端。

终端一启动模拟调度器：

```powershell
python tools/mock_scheduler.py
```

注意：当前检出版本中仓库原有的 `scheduler/` 仍使用旧请求和响应结构，不能直接处理本 README 的新接口。联调时先使用上面的 `tools/mock_scheduler.py`；正式接入前，调度器负责同学需要将 `/scheduler/decide` 更新为第 5 节约定的结构。

终端二启动测试订阅器：

```powershell
python tools/test_subscriber.py
```

终端三启动三个发送器：

```powershell
python -m sender --config config/local.json `
  --source "sender_01=D:\data\bearing_01.mat" `
  --source "sender_02=D:\data\bearing_02.mat" `
  --source "sender_03=D:\data\bearing_03.mat"
```

正常运行约 4 秒。三个发送器各发送 80 包，总计 240 包。快速测试可增加 `--accelerated`，但加速模式不能用于计算真实端到端时延。

## 5. 调度接口

每个发送器独立调用一次：

```http
POST /scheduler/decide
Content-Type: application/json
```

请求示例：

```json
{
  "device_id": "machine_01",
  "sender_id": "sender_01",
  "task_id": "sd_01_tk_0001",
  "bearing_id": "bearing_01",
  "packet_size_bytes": 102400,
  "expected_packet_count": 80,
  "expected_duration_ms": 4000,
  "created_timestamp_ns": 1781920800000000000
}
```

成功响应：

```json
{
  "device_id": "machine_01",
  "sender_id": "sender_01",
  "task_id": "sd_01_tk_0001",
  "bearing_id": "bearing_01",
  "target_topic": "edge/edge_2/input"
}
```

发送器会核对四个 ID；任何 ID 不一致或 `target_topic` 为空，都视为调度失败。初次请求失败后最多重试两次，仍失败则记录 `scheduling_failed`，不发送 SensorPacket。

## 6. SensorPacket 标识

```json
{
  "device_id": "machine_01",
  "sender_id": "sender_01",
  "bearing_id": "bearing_01",
  "task_id": "sd_01_tk_0001",
  "packet_id": "sd_01_tk_0001_bearing_01_pkt_001",
  "sequence_number": 1,
  "end_generate_timestamp_ns": 1781920800050000000,
  "data": {}
}
```

- `task_id`：发送器节点编号加本地任务序号。三个发送器都从 1 计数也不会冲突。
- `packet_id`：任务、轴承和包序号组成的唯一编号。
- `sequence_number`：该任务中的第几包，范围为 1 至 80。
- `end_generate_timestamp_ns`：这一包数据准备完成的时间，也是端到端时延的起点。
- `data`：MAT 文件中按 50 ms 对齐后的原始信号，不包含故障标准答案。

## 7. 日志

本地日志用于当前阶段和日志服务不可用时的兜底：

- 每包日志：`runtime/logs/packet_logs.jsonl`
- 任务日志：`runtime/logs/task_logs.jsonl`

每行都是一条独立 JSON。未来日志服务提供 HTTP 接口后，可以在 `LocalLogSink.write_packet()` 和 `LocalLogSink.write_task()` 中增加远程提交，同时保留本地落盘。

### 7.1 任务状态

- `completed`：80 包全部收到 PUBACK。
- `partially_completed`：部分包确认，部分包失败或丢弃。
- `failed`：任务没有成功发送任何包，或发送过程发生致命错误。
- `scheduling_failed`：调度失败，尚未开始 MQTT 发送。

任务日志同时记录 `confirmed_packet_count`、`failed_packet_count` 和 `dropped_packet_count`。

任务日志关键字段：

- `device_id`：被监测设备编号。
- `sender_id`、`bearing_id`、`task_id`：说明这条任务属于哪台发送器、哪个轴承和哪一次任务。
- `target_topic`：调度器为该任务返回的 MQTT 主题；调度失败时为 `null`。
- `expected_packet_count`：本任务预计发送包数，当前固定为 80。
- `schedule_retry_count`：本次 HTTP 调度请求的重试次数，不包含第一次请求。
- `mqtt_reconnect_count`：任务期间 MQTT 断线后重新连接成功的次数。
- `mqtt_publish_retry_total`：本任务全部数据包的 MQTT 重发次数总和。
- `task_started_timestamp_ns`、`task_finished_timestamp_ns`：发送器任务开始和结束的本机时间，单位为纳秒。
- `replay_mode`：`realtime` 表示按 50 ms 节奏回放，`accelerated` 表示测试用快速回放。
- `error_code`：机器可读取的失败原因；成功时为 `null`。

### 7.2 每包发送状态

- `confirmed`：QoS 1 PUBACK 已返回，只表示 Broker 收到，不表示边缘完成推理。
- `failed`：重试后仍未获得确认。
- `dropped`：待发送队列已满，最早的旧包被发送器丢弃。

每包日志关键字段：

- `packet_size_bytes`：实际发布的 JSON 字节数，不是人工填写的平均值。
- `end_generate_timestamp_ns`：该包准备完成的时间。
- `mqtt_publish_timestamp_ns`：发送器首次调用 MQTT 发布的时间。
- `broker_ack_timestamp_ns`：收到 PUBACK 的时间；发布失败或丢弃时为 `null`。
- `mqtt_publish_retry_count`：该包的重发次数，不包含第一次发布。
- `publish_status`、`error_code`：该包最终状态和失败原因。

常见 `error_code`：

- `null`：发送成功，没有错误。
- `MQTT_NOT_CONNECTED`：发送时无法连接 Broker。
- `PUBACK_TIMEOUT`：超过单包送达期限仍未收到 PUBACK。
- `SEND_QUEUE_FULL`：缓存已满，旧包被丢弃。
- `SCHEDULER_REQUEST_FAILED`：调度请求重试后仍失败。
- `MQTT_TASK_ERROR`：MQTT 任务运行期间发生致命异常。
- `PACKET_DELIVERY_PARTIAL`：任务中存在失败或丢弃的数据包。
- `PACKET_DELIVERY_FAILED`：任务正常走完发送流程，但没有任何数据包得到确认。
- `SENDER_TASK_EXCEPTION`：单台发送器在数据读取或其他未预期步骤中异常；另外两台发送器仍继续运行。

## 8. 网络模块接入

网络模块位于通信链路中间，发送器无需导入网络模块的 Python 代码，只需要把目标地址改成网络模块提供的代理监听地址。

### 8.1 调度 HTTP 链路

```text
sender_01 -> HTTP代理 127.0.0.1:18001 -> 调度器 127.0.0.1:8003
sender_02 -> HTTP代理 127.0.0.1:18002 -> 调度器 127.0.0.1:8003
sender_03 -> HTTP代理 127.0.0.1:18003 -> 调度器 127.0.0.1:8003
```

发送器配置中的 `scheduler_url` 分别改成代理地址，但保留 `/scheduler/decide` 路径。

### 8.2 MQTT 链路

```text
sender_01 -> MQTT代理 127.0.0.1:11881 -> Mosquitto 127.0.0.1:1883
sender_02 -> MQTT代理 127.0.0.1:11882 -> Mosquitto 127.0.0.1:1883
sender_03 -> MQTT代理 127.0.0.1:11883 -> Mosquitto 127.0.0.1:1883
```

发送器配置中的 `mqtt_host` 和 `mqtt_port` 改成对应代理监听地址。代理内部仍转发原始 MQTT/TCP 数据，因此业务字段和 MQTT 主题不需要变化。

如果三个发送器共用同一种网络状态，它们可以连接同一个代理端口；如果要模拟三条不同链路，则必须使用三个独立代理端口。以上端口只是示例，最终以网络模块实际提供的监听端口为准。

## 9. 其他模块的接入位置

- 调度器：实现 `POST /scheduler/decide`，返回单个 `target_topic`。
- 边缘节点：订阅调度器分配的 MQTT 主题，根据 `packet_id` 去重并按 `task_id`、`sequence_number` 组装数据。
- 网络模块：代理各发送器的 `scheduler_url` 和 `mqtt_host:mqtt_port`。
- 日志模块：接收任务日志和每包日志；当前先由 `LocalLogSink` 写本地 JSONL。
- 真实传感器：将 `load_mat_record()` 替换为实时采集适配器，后续调度、MQTT和日志流程无需改变。

## 10. 自动化测试

```powershell
python -m unittest discover -s tests -v
```

测试覆盖配置校验、任务和数据包编号、MAT 切窗、调度响应校验、三个发送器并行运行、MQTT QoS 1、重试、PUBACK超时、缓存丢弃和本地日志。若本机 Mosquitto 正在运行，还会额外执行三台真实 Paho 客户端发布 240 包的集成测试。
