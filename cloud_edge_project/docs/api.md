# 云边协同项目接口文档

> 版本：V0.3，基于 `2026.7.6.docx` 中数据输入负责人确定的传感器数据接口，以及 V0.2 版 `api.md` 修改整理。  
> 当前目标：第一阶段先面向一个工业设备轴承检测场景跑通完整链路。本文档作为后续代码实现、联调、测试脚本和前端展示的统一接口依据。

---

## 1. 单一场景说明

第一阶段只跑通一个工业设备轴承检测场景。

```text
发送器/采集端
  -> POST /edge/infer
边缘推理服务
  -> POST /scheduler/decide
调度服务
  -> 如果 route = cloud，POST /cloud/infer
云端推理服务
  -> POST /logs/task_trace
日志/可视化服务
```

这个场景的数据类型固定为：

```text
data_type = bearing_timeseries
```

数据含义是：采集端每 50 ms 发送一包轴承时序数据，包含振动原始采样数组、电流、温度、转速和负载。系统最终输出设备状态判断、置信度、调度路径、总时延和日志记录。

第一阶段重点不是实现复杂调度或复杂模型，而是先跑通以下主链路：

```text
SensorPacket -> EdgeResult -> ScheduleDecision -> CloudResult（可选） -> TaskTrace -> Dashboard
```

说明：当 `route = cloud` 时，主流程继续调用云端服务并使用 `CloudResult` 生成最终日志；当 `route = edge` 或 `fallback_edge` 时，不再定义新的边缘最终结果结构，而是直接把 `EdgeResult` 整理进 `TaskTrace` 的最终字段。

---

## 2. 服务与端口约定

| 服务 | 端口 | 主要接口 | 负责人 |
|---|---:|---|---|
| 边缘推理服务 | 8001 | `POST /edge/infer`, `GET /health` | 贾 |
| 调度服务 | 8003 | `POST /scheduler/decide`, `GET /health` | 彭 |
| 云端推理服务 | 8004 | `POST /cloud/infer`, `GET /health` | 贾 |
| 日志与指标服务 | 8006 | `POST /logs/task_trace`, `GET /dashboard/metrics`, `GET /dashboard/tasks`, `GET /health` | 刘 |

本地联调地址示例：

```text
http://127.0.0.1:8001/edge/infer
http://127.0.0.1:8003/scheduler/decide
http://127.0.0.1:8004/cloud/infer
http://127.0.0.1:8006/logs/task_trace
```

注意：

1. 这些是后端接口地址，不是普通网页地址。
2. `POST` 接口需要用程序、Postman、curl 或测试脚本发送 JSON。
3. 不能只靠浏览器点击打开 `POST` 接口。
4. 端口可以先按本文档固定，后续再统一放入配置文件。

---

## 3. ID 约定

第一阶段统一使用 `packet_id` 作为整条链路的唯一追踪 ID。

同一个数据包从发送器进入系统后，经过边缘推理、调度决策、云端推理以及日志记录时，所有接口请求和返回结果都必须保留相同的 `packet_id`，用于后续链路追踪、结果对齐和日志查询。

第一阶段暂不使用 `task_id`。

如果后续扩展为“一个任务包含多个数据包”的复杂场景，再引入 `task_id`，并规定：

- `task_id`：表示上层任务 ID；
- `packet_id`：表示单个传感器数据包 ID。

示例：

```text
第一阶段：
packet_id = batch_000001

后续扩展阶段：
task_id = task_20260707_0001
packet_id = batch_000001
```

---

## 4. 时间格式约定

第一阶段同时存在两类时间字段，需要明确区分。

### 4.1 采样时间

传感器原始采样时间继续使用纳秒时间戳：

```json
{
  "start_timestamp_ns": 1781920800000000000,
  "end_timestamp_ns": 1781920800050000000
}
```

说明：

- `start_timestamp_ns`：当前数据包第一个采样点时间，单位 ns；
- `end_timestamp_ns`：当前数据包覆盖时间段的结束时间，单位 ns；
- `start_timestamp_ns` 必须小于 `end_timestamp_ns`；
- 第一阶段 `end_timestamp_ns - start_timestamp_ns` 对应约 50 ms。

### 4.2 日志时间

日志记录时间统一使用 ISO 8601 字符串，字段名为 `log_timestamp`。

推荐格式：

```json
{
  "log_timestamp": "2026-07-07T10:00:05+08:00"
}
```

不建议继续使用：

```json
{
  "timestamp": "2026-07-07 10:00:05"
}
```

原因是旧格式没有时区信息，后续跨机器部署、日志排序和端到端时延统计时容易混乱。

---

## 5. 公共数据结构

### 5.1 SensorPacket：传感器数据包

这是数据输入负责人确定的“发送器 -> 边缘节点”数据格式，也是整个项目第一阶段的原始输入。

正式 JSON 示例：

```json
{
  "packet_id": "batch_000001",
  "device_id": "K001",
  "sensor_id": "sensor_K001",
  "sequence_number": 1,
  "start_timestamp_ns": 1781920800000000000,
  "end_timestamp_ns": 1781920800050000000,
  "duration_ms": 50,
  "data": {
    "data_type": "bearing_timeseries",
    "vibration_sample_rate_hz": 16000,
    "vibration_sample_count": 800,
    "vibration": [0.012, 0.018, 0.009, 0.021],
    "current": 1.34,
    "temperature": 45.8,
    "speed": 899.7,
    "load": 0.7
  }
}
```

说明：上面示例中的 `vibration` 数组为了展示只写了 4 个数。正式联调数据中，`vibration` 数组长度必须等于 `vibration_sample_count`，第一阶段固定为 800。

字段说明：

| 字段 | 类型 | 必须 | 说明 |
|---|---|---:|---|
| `packet_id` | string | 是 | 数据包编号，用于唯一标识和日志追踪 |
| `device_id` | string | 是 | 被检测设备编号 |
| `sensor_id` | string | 是 | 采集终端或传感器编号 |
| `sequence_number` | int | 是 | 自增序号，用于检测丢包、重复和乱序 |
| `start_timestamp_ns` | int | 是 | 当前数据包第一个采样点时间，单位 ns |
| `end_timestamp_ns` | int | 是 | 当前数据包覆盖时间段的结束时间，单位 ns |
| `duration_ms` | int | 是 | 当前数据包覆盖时长，第一阶段固定为 50 ms |
| `data` | object | 是 | 具体检测数据 |

`data` 字段说明：

| 字段 | 类型 | 必须 | 说明 |
|---|---|---:|---|
| `data_type` | string | 是 | 数据类型，第一阶段固定为 `bearing_timeseries` |
| `vibration_sample_rate_hz` | int | 是 | 振动采样频率，第一阶段固定为 16000 Hz |
| `vibration_sample_count` | int | 是 | 一条数据包内的振动点数量，第一阶段固定为 800 点 |
| `vibration` | number[] | 是 | 50 ms 内连续振动原始采样数组 |
| `current` | number | 是 | 当前瞬时电流 |
| `temperature` | number | 是 | 当前设备或轴承温度 |
| `speed` | number | 是 | 当前设备转速，建议单位 rpm |
| `load` | number | 是 | 当前设备负载率，建议范围 0 到 1 |

---

### 5.2 EdgeResult：边缘推理结果

边缘节点收到 `SensorPacket` 后，先做轻量推理，输出初步诊断。

```json
{
  "packet_id": "batch_000001",
  "device_id": "K001",
  "edge_node_id": "edge_1",
  "model_name": "edge_bearing_mock",
  "label": "abnormal",
  "confidence": 0.72,
  "risk_level": "medium",
  "need_cloud": true,
  "edge_latency_ms": 38.0
}
```

| 字段 | 类型 | 必须 | 说明 |
|---|---|---:|---|
| `packet_id` | string | 是 | 对应原始数据包编号，必须与请求中的 `packet_id` 一致 |
| `device_id` | string | 是 | 被检测设备编号 |
| `edge_node_id` | string | 是 | 执行推理的边缘节点 |
| `model_name` | string | 是 | 边缘模型名称或版本 |
| `label` | string | 是 | 初步判断结果，第一阶段建议取值 `normal` / `abnormal` |
| `confidence` | number | 是 | 边缘判断置信度，范围 0 到 1 |
| `risk_level` | string | 是 | 风险等级，第一阶段建议取值 `low` / `medium` / `high` |
| `need_cloud` | boolean | 是 | 边缘模型是否建议上云，最终仍由调度器决定 |
| `edge_latency_ms` | number | 是 | 边缘推理耗时，单位 ms |

---

### 5.3 ScheduleRequest：调度请求

调度器不一定需要完整的原始振动数组，但需要原始数据包的关键信息、边缘结果、网络状态和节点状态。

```json
{
  "packet": {
    "packet_id": "batch_000001",
    "device_id": "K001",
    "sensor_id": "sensor_K001",
    "sequence_number": 1,
    "duration_ms": 50,
    "data_type": "bearing_timeseries",
    "vibration_sample_count": 800,
    "payload_size_kb": 12.5
  },
  "edge_result": {
    "label": "abnormal",
    "confidence": 0.72,
    "risk_level": "medium",
    "need_cloud": true,
    "edge_latency_ms": 38.0
  },
  "network_state": {
    "latency_ms": 30.0,
    "bandwidth_mbps": 20.0,
    "packet_loss": 0.01,
    "cloud_available": true
  },
  "node_state": {
    "edge_cpu_usage": 0.55,
    "edge_memory_usage": 0.62,
    "cloud_queue_length": 3
  }
}
```

`packet.payload_size_kb` 表示当前数据包序列化后的近似大小，单位 KB。它用于调度器估算上云传输成本。

第一阶段建议由边缘服务或主流程脚本在调用 `/scheduler/decide` 前计算或估算该字段。如果暂时不方便精确计算，可以先按 `vibration_sample_count` 粗略估算；调度器可以读取该字段，但不应在第一阶段强依赖它，否则会增加联调阻塞点。

---

### 5.4 ScheduleDecision：调度决策结果

调度器根据边缘结果、网络状态和节点状态决定后续路径。

```json
{
  "packet_id": "batch_000001",
  "route": "cloud",
  "target_node": "cloud_1",
  "upload_required": true,
  "reason": "edge confidence is low and cloud is available",
  "estimated_total_latency_ms": 145.0
}
```

| 字段 | 类型 | 必须 | 说明 |
|---|---|---:|---|
| `packet_id` | string | 是 | 对应原始数据包编号 |
| `route` | string | 是 | 调度路径，第一阶段只使用 `edge` / `cloud` / `fallback_edge` |
| `target_node` | string | 是 | 目标执行节点 |
| `upload_required` | boolean | 是 | 是否需要上传云端 |
| `reason` | string | 是 | 调度原因，便于联调和展示 |
| `estimated_total_latency_ms` | number | 是 | 预计端到端时延，单位 ms |

第一阶段推荐调度规则：

```text
如果 confidence >= 0.8 -> edge
如果 confidence < 0.8 且 cloud_available = true -> cloud
如果 confidence < 0.8 且 cloud_available = false -> fallback_edge
```

说明：

- `edge`：边缘结果直接作为最终结果；
- `cloud`：上传到云端模型进行进一步推理；
- `fallback_edge`：原本需要上云，但云端不可用，因此退化使用边缘结果。

第一阶段不实现以下路径：

```text
fog
edge_cloud
```

后续如果引入雾节点或边云协同推理，再扩展 `route` 的取值。

---

### 5.5 CloudResult：云端推理结果

当调度器决定上云时，云端模型输出最终或更高置信度诊断。

```json
{
  "packet_id": "batch_000001",
  "device_id": "K001",
  "cloud_node_id": "cloud_1",
  "model_name": "cloud_bearing_mock",
  "label": "abnormal",
  "confidence": 0.93,
  "risk_level": "high",
  "cloud_latency_ms": 86.0,
  "decision": {
    "action": "send_alert",
    "description": "设备存在高风险异常，建议停机检查"
  }
}
```

| 字段 | 类型 | 必须 | 说明 |
|---|---|---:|---|
| `packet_id` | string | 是 | 对应原始数据包编号 |
| `device_id` | string | 是 | 被检测设备编号 |
| `cloud_node_id` | string | 是 | 执行推理的云端节点 |
| `model_name` | string | 是 | 云端模型名称或版本 |
| `label` | string | 是 | 云端判断结果，第一阶段建议取值 `normal` / `abnormal` |
| `confidence` | number | 是 | 云端判断置信度，范围 0 到 1 |
| `risk_level` | string | 是 | 风险等级，第一阶段建议取值 `low` / `medium` / `high` |
| `cloud_latency_ms` | number | 是 | 云端推理耗时，单位 ms |
| `decision` | object | 是 | 最终处理建议 |

`decision.action` 第一阶段建议取值：

```text
none
record_only
send_alert
stop_machine_check
```

---

### 5.6 TaskTrace：链路日志

日志记录完整链路结果，后续指标统计和可视化都基于它。

```json
{
  "packet_id": "batch_000001",
  "device_id": "K001",
  "sensor_id": "sensor_K001",
  "sequence_number": 1,
  "data_type": "bearing_timeseries",
  "route": "cloud",
  "edge_label": "abnormal",
  "edge_confidence": 0.72,
  "cloud_label": "abnormal",
  "cloud_confidence": 0.93,
  "final_label": "abnormal",
  "final_confidence": 0.93,
  "risk_level": "high",
  "edge_latency_ms": 38.0,
  "network_latency_ms": 30.0,
  "cloud_latency_ms": 86.0,
  "total_latency_ms": 154.0,
  "success": true,
  "error_code": null,
  "log_timestamp": "2026-07-07T10:00:05+08:00"
}
```

| 字段 | 类型 | 必须 | 说明 |
|---|---|---:|---|
| `packet_id` | string | 是 | 对应原始数据包编号 |
| `device_id` | string | 是 | 被检测设备编号 |
| `sensor_id` | string | 是 | 采集终端编号 |
| `sequence_number` | int | 是 | 数据包序号 |
| `data_type` | string | 是 | 数据类型 |
| `route` | string | 是 | 实际调度路径 |
| `edge_label` | string | 是 | 边缘模型判断结果 |
| `edge_confidence` | number | 是 | 边缘模型置信度 |
| `cloud_label` | string 或 null | 否 | 云端模型判断结果，没有上云时为 null |
| `cloud_confidence` | number 或 null | 否 | 云端模型置信度，没有上云时为 null |
| `final_label` | string | 是 | 最终诊断结果 |
| `final_confidence` | number | 是 | 最终置信度 |
| `risk_level` | string | 是 | 最终风险等级 |
| `edge_latency_ms` | number | 是 | 边缘推理耗时 |
| `network_latency_ms` | number 或 null | 否 | 网络传输耗时，没有上云时可为 0 或 null |
| `cloud_latency_ms` | number 或 null | 否 | 云端推理耗时，没有上云时为 null |
| `total_latency_ms` | number | 是 | 端到端总时延 |
| `success` | boolean | 是 | 任务是否成功完成 |
| `error_code` | string 或 null | 否 | 失败原因代码，成功时为 null |
| `log_timestamp` | string | 是 | 日志记录时间，ISO 8601 格式 |

如果没有上云，`TaskTrace` 示例：

```json
{
  "packet_id": "batch_000002",
  "device_id": "K001",
  "sensor_id": "sensor_K001",
  "sequence_number": 2,
  "data_type": "bearing_timeseries",
  "route": "edge",
  "edge_label": "normal",
  "edge_confidence": 0.91,
  "cloud_label": null,
  "cloud_confidence": null,
  "final_label": "normal",
  "final_confidence": 0.91,
  "risk_level": "low",
  "edge_latency_ms": 32.0,
  "network_latency_ms": 0,
  "cloud_latency_ms": null,
  "total_latency_ms": 32.0,
  "success": true,
  "error_code": null,
  "log_timestamp": "2026-07-07T10:00:06+08:00"
}
```

---

## 6. 核心接口

### 6.1 边缘推理接口：POST /edge/infer

调用方：发送器、数据生成器或测试脚本。  
接收方：边缘推理服务。

请求体使用 `SensorPacket`。

响应体使用 `EdgeResult`。

地址：

```text
POST http://127.0.0.1:8001/edge/infer
```

请求示例：

```json
{
  "packet_id": "batch_000001",
  "device_id": "K001",
  "sensor_id": "sensor_K001",
  "sequence_number": 1,
  "start_timestamp_ns": 1781920800000000000,
  "end_timestamp_ns": 1781920800050000000,
  "duration_ms": 50,
  "data": {
    "data_type": "bearing_timeseries",
    "vibration_sample_rate_hz": 16000,
    "vibration_sample_count": 800,
    "vibration": [0.012, 0.018, 0.009, 0.021],
    "current": 1.34,
    "temperature": 45.8,
    "speed": 899.7,
    "load": 0.7
  }
}
```

响应示例：

```json
{
  "packet_id": "batch_000001",
  "device_id": "K001",
  "edge_node_id": "edge_1",
  "model_name": "edge_bearing_mock",
  "label": "abnormal",
  "confidence": 0.72,
  "risk_level": "medium",
  "need_cloud": true,
  "edge_latency_ms": 38.0
}
```

实现要求：

1. 返回的 `packet_id` 必须与请求中的 `packet_id` 一致。
2. 如果输入字段缺失或格式错误，返回统一错误响应。
3. 第一阶段模型可以先用 mock 规则，但返回字段必须完整。

---

### 6.2 调度决策接口：POST /scheduler/decide

调用方：边缘服务或主流程脚本。  
接收方：调度服务。

请求体使用 `ScheduleRequest`。

地址：

```text
POST http://127.0.0.1:8003/scheduler/decide
```

请求示例：

```json
{
  "packet": {
    "packet_id": "batch_000001",
    "device_id": "K001",
    "sensor_id": "sensor_K001",
    "sequence_number": 1,
    "duration_ms": 50,
    "data_type": "bearing_timeseries",
    "vibration_sample_count": 800,
    "payload_size_kb": 12.5
  },
  "edge_result": {
    "label": "abnormal",
    "confidence": 0.72,
    "risk_level": "medium",
    "need_cloud": true,
    "edge_latency_ms": 38.0
  },
  "network_state": {
    "latency_ms": 30.0,
    "bandwidth_mbps": 20.0,
    "packet_loss": 0.01,
    "cloud_available": true
  },
  "node_state": {
    "edge_cpu_usage": 0.55,
    "edge_memory_usage": 0.62,
    "cloud_queue_length": 3
  }
}
```

响应示例：

```json
{
  "packet_id": "batch_000001",
  "route": "cloud",
  "target_node": "cloud_1",
  "upload_required": true,
  "reason": "edge confidence is low and cloud is available",
  "estimated_total_latency_ms": 145.0
}
```

实现要求：

1. 第一阶段只返回 `edge`、`cloud`、`fallback_edge` 三种路径。
2. `edge_result.need_cloud` 可以作为参考，但最终路径由调度器决定。
3. 当 `confidence >= 0.8` 时，即使云端可用，也优先返回 `edge`。
4. 当 `confidence < 0.8` 且 `cloud_available = false` 时，返回 `fallback_edge`，不允许返回 `cloud`。
5. `estimated_total_latency_ms` 可以先用规则估算，后续再替换成更复杂算法。

---

### 6.3 云端推理接口：POST /cloud/infer

调用方：主流程脚本、边缘服务或调度后的执行模块。  
接收方：云端推理服务。

只有当 `ScheduleDecision.route = cloud` 时调用。

地址：

```text
POST http://127.0.0.1:8004/cloud/infer
```

请求示例：

```json
{
  "packet": {
    "packet_id": "batch_000001",
    "device_id": "K001",
    "sensor_id": "sensor_K001",
    "sequence_number": 1,
    "start_timestamp_ns": 1781920800000000000,
    "end_timestamp_ns": 1781920800050000000,
    "duration_ms": 50,
    "data": {
      "data_type": "bearing_timeseries",
      "vibration_sample_rate_hz": 16000,
      "vibration_sample_count": 800,
      "vibration": [0.012, 0.018, 0.009, 0.021],
      "current": 1.34,
      "temperature": 45.8,
      "speed": 899.7,
      "load": 0.7
    }
  },
  "edge_result": {
    "label": "abnormal",
    "confidence": 0.72,
    "risk_level": "medium"
  }
}
```

响应示例：

```json
{
  "packet_id": "batch_000001",
  "device_id": "K001",
  "cloud_node_id": "cloud_1",
  "model_name": "cloud_bearing_mock",
  "label": "abnormal",
  "confidence": 0.93,
  "risk_level": "high",
  "cloud_latency_ms": 86.0,
  "decision": {
    "action": "send_alert",
    "description": "设备存在高风险异常，建议停机检查"
  }
}
```

实现要求：

1. 返回的 `packet_id` 必须与请求中的 `packet.packet_id` 一致。
2. 云端响应可作为最终结果写入日志。
3. 第一阶段云端模型可以先用 mock 规则，但要体现出与边缘模型不同的置信度或处理建议。

---

### 6.4 日志记录接口：POST /logs/task_trace

调用方：主流程脚本或服务编排模块。  
接收方：日志与指标服务。

地址：

```text
POST http://127.0.0.1:8006/logs/task_trace
```

请求体使用 `TaskTrace`。

请求示例：

```json
{
  "packet_id": "batch_000001",
  "device_id": "K001",
  "sensor_id": "sensor_K001",
  "sequence_number": 1,
  "data_type": "bearing_timeseries",
  "route": "cloud",
  "edge_label": "abnormal",
  "edge_confidence": 0.72,
  "cloud_label": "abnormal",
  "cloud_confidence": 0.93,
  "final_label": "abnormal",
  "final_confidence": 0.93,
  "risk_level": "high",
  "edge_latency_ms": 38.0,
  "network_latency_ms": 30.0,
  "cloud_latency_ms": 86.0,
  "total_latency_ms": 154.0,
  "success": true,
  "error_code": null,
  "log_timestamp": "2026-07-07T10:00:05+08:00"
}
```

响应示例：

```json
{
  "packet_id": "batch_000001",
  "saved": true,
  "log_path": "logs/task_trace.jsonl"
}
```

实现要求：

1. 日志建议采用 JSONL，一行保存一个 `TaskTrace`。
2. 如果保存失败，返回统一错误响应，`error_code` 建议为 `LOG_SAVE_FAILED`。
3. 日志服务不负责重新推理，只负责保存和统计。

---

### 6.5 指标查询接口：GET /dashboard/metrics

调用方：前端页面或测试脚本。  
接收方：日志与指标服务。

地址：

```text
GET http://127.0.0.1:8006/dashboard/metrics
```

响应示例：

```json
{
  "total_packets": 100,
  "success_rate": 0.98,
  "avg_total_latency_ms": 142.5,
  "cloud_call_ratio": 0.42,
  "edge_only_ratio": 0.58,
  "fallback_edge_ratio": 0.00,
  "abnormal_ratio": 0.31
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `total_packets` | int | 已记录的数据包总数 |
| `success_rate` | number | 成功完成比例 |
| `avg_total_latency_ms` | number | 平均端到端时延 |
| `cloud_call_ratio` | number | 实际上云比例 |
| `edge_only_ratio` | number | 只在边缘完成的比例 |
| `fallback_edge_ratio` | number | 云端不可用时回退边缘的比例 |
| `abnormal_ratio` | number | 最终判断为异常的比例 |

---

### 6.6 任务查询接口：GET /dashboard/tasks

调用方：前端页面或测试脚本。  
接收方：日志与指标服务。

地址：

```text
GET http://127.0.0.1:8006/dashboard/tasks
```

响应示例：

```json
{
  "tasks": [
    {
      "packet_id": "batch_000001",
      "device_id": "K001",
      "route": "cloud",
      "final_label": "abnormal",
      "final_confidence": 0.93,
      "risk_level": "high",
      "total_latency_ms": 154.0,
      "success": true,
      "log_timestamp": "2026-07-07T10:00:05+08:00"
    }
  ]
}
```

建议后续支持的查询参数：

```text
GET /dashboard/tasks?limit=20
GET /dashboard/tasks?device_id=K001
GET /dashboard/tasks?route=cloud
GET /dashboard/tasks?final_label=abnormal
```

第一阶段可以先只实现无参数查询，返回最近若干条记录。

---

### 6.7 健康检查接口：GET /health

每个服务都必须实现。

地址示例：

```text
GET http://127.0.0.1:8001/health
GET http://127.0.0.1:8003/health
GET http://127.0.0.1:8004/health
GET http://127.0.0.1:8006/health
```

响应示例：

```json
{
  "service": "edge_service",
  "node_id": "edge_1",
  "status": "ok",
  "port": 8001,
  "model_backend": "mock"
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `service` | string | 服务名称 |
| `node_id` | string | 节点编号 |
| `status` | string | 服务状态，正常为 `ok` |
| `port` | int | 当前服务端口 |
| `model_backend` | string | 模型后端，第一阶段可为 `mock` |

---

## 7. 统一错误响应

所有 `POST` 接口在失败时统一返回以下格式。

```json
{
  "success": false,
  "packet_id": "batch_000001",
  "error_code": "INVALID_PACKET",
  "message": "vibration length does not match vibration_sample_count"
}
```

字段说明：

| 字段 | 类型 | 必须 | 说明 |
|---|---|---:|---|
| `success` | boolean | 是 | 是否成功，错误响应中固定为 false |
| `packet_id` | string 或 null | 否 | 如果请求中能解析出 `packet_id`，则返回；否则为 null |
| `error_code` | string | 是 | 错误代码 |
| `message` | string | 是 | 错误说明，便于联调定位问题 |

常用 `error_code`：

| error_code | 说明 |
|---|---|
| `INVALID_JSON` | 请求体不是合法 JSON |
| `INVALID_PACKET` | 输入数据格式错误 |
| `MISSING_FIELD` | 缺少必要字段 |
| `MODEL_INFER_FAILED` | 模型推理失败 |
| `SCHEDULER_FAILED` | 调度服务内部错误 |
| `CLOUD_UNAVAILABLE` | 云端不可用 |
| `LOG_SAVE_FAILED` | 日志保存失败 |
| `INTERNAL_ERROR` | 其他服务内部错误 |

HTTP 状态码建议：

| 状态码 | 场景 |
|---:|---|
| 200 | 请求成功 |
| 400 | 请求字段错误、JSON 格式错误、字段校验失败 |
| 500 | 服务内部错误 |
| 503 | 云端不可用或下游服务不可达 |

注意：正常响应不强制套一层 `success: true`，可以直接返回本文档定义的 `EdgeResult`、`ScheduleDecision`、`CloudResult` 等对象。错误响应必须统一格式。

---

## 8. 字段校验规则

第一阶段所有服务都应尽量遵守以下校验规则。

### 8.1 SensorPacket 校验

| 字段 | 规则 |
|---|---|
| `packet_id` | 不能为空，同一链路中必须保持一致 |
| `device_id` | 不能为空 |
| `sensor_id` | 不能为空 |
| `sequence_number` | 必须为整数，建议从 1 开始递增 |
| `start_timestamp_ns` | 必须为整数 |
| `end_timestamp_ns` | 必须为整数，且大于 `start_timestamp_ns` |
| `duration_ms` | 第一阶段固定为 50 |
| `data.data_type` | 第一阶段固定为 `bearing_timeseries` |
| `data.vibration_sample_rate_hz` | 第一阶段固定为 16000 |
| `data.vibration_sample_count` | 第一阶段固定为 800 |
| `data.vibration` | 必须为 number 数组，长度必须等于 `vibration_sample_count` |
| `data.current` | 必须为 number |
| `data.temperature` | 必须为 number |
| `data.speed` | 必须为 number，建议单位 rpm |
| `data.load` | 必须为 number，建议范围 0 到 1 |

### 8.2 结果字段校验

| 字段 | 规则 |
|---|---|
| `confidence` | 必须在 0 到 1 之间 |
| `edge_confidence` | 必须在 0 到 1 之间 |
| `cloud_confidence` | 没有上云时为 null；上云时必须在 0 到 1 之间 |
| `final_confidence` | 必须在 0 到 1 之间 |
| `label` | 第一阶段建议只使用 `normal` / `abnormal` |
| `risk_level` | 只能为 `low` / `medium` / `high` |
| `route` | 第一阶段只能为 `edge` / `cloud` / `fallback_edge` |
| `*_latency_ms` | 必须大于或等于 0，不能为负数 |
| `log_timestamp` | 必须为 ISO 8601 字符串 |

---

## 9. 第一阶段联调顺序

建议按以下顺序联调，不要一开始就全部并行接起来。

1. 周先生成一条 `SensorPacket` 示例数据，字段必须与本文档一致。
2. 贾实现 `POST /edge/infer`，用 mock 模型返回 `EdgeResult`。
3. 使用测试脚本检查 `/edge/infer` 是否能保留相同的 `packet_id`。
4. 彭实现 `POST /scheduler/decide`，先用规则调度返回 `ScheduleDecision`。
5. 当 `route = edge` 或 `fallback_edge` 时，主流程脚本直接把边缘结果整理为最终结果。
6. 当 `route = cloud` 时，调用 `POST /cloud/infer` 返回 `CloudResult`。
7. 主流程脚本把最终结果整理成 `TaskTrace`。
8. 刘实现 `POST /logs/task_trace`，把一次完整链路写入 `logs/task_trace.jsonl`。
9. 刘实现 `GET /dashboard/metrics` 和 `GET /dashboard/tasks`，从日志中读取统计结果。
10. 最后再把发送器、边缘服务、调度服务、云端服务、日志服务完整串起来。

---

## 10. 最小验收标准

第一阶段跑通后，至少应满足：

```text
1. 一条 SensorPacket 可以成功发送到 /edge/infer。
2. /edge/infer 返回 packet_id 一致的 EdgeResult。
3. /scheduler/decide 能根据 confidence 和 cloud_available 返回 edge/cloud/fallback_edge。
4. 如果 route = cloud，/cloud/infer 能返回 CloudResult。
5. 如果 route = edge 或 fallback_edge，系统能直接使用 EdgeResult 生成最终结果。
6. /logs/task_trace 能记录 packet_id、route、edge_confidence、cloud_confidence、final_label、total_latency_ms、success。
7. /dashboard/metrics 能统计成功率、平均时延、云端调用比例、边缘完成比例。
8. /dashboard/tasks 能返回最近若干条链路日志。
9. 所有服务的 /health 返回 status = ok。
10. 任一 POST 接口收到错误数据时，能返回统一错误响应。
```

---

## 11. 统一约定

1. 第一阶段统一使用 `packet_id` 作为主链路追踪 ID。
2. 第一阶段暂不使用 `task_id`。
3. 正式传输 JSON 时不能带注释。
4. 字段名必须与本文档一致，不要自行改成 `temp`、`score`、`path` 等别名。
5. `duration_ms` 第一阶段固定为 50。
6. `data_type` 第一阶段固定为 `bearing_timeseries`。
7. `vibration_sample_rate_hz` 第一阶段固定为 16000。
8. `vibration_sample_count` 第一阶段固定为 800。
9. `route` 第一阶段只使用 `edge`、`cloud`、`fallback_edge`。
10. 第一阶段不实现 `fog`、`edge_cloud`、`/consistency/resolve`。
11. 日志时间统一使用 `log_timestamp`，格式为 ISO 8601。
12. 端口和服务地址后续应放入配置文件，不要在业务代码中写死。
13. 如果字段需要变更，先修改本文档，再通知全体成员同步代码。

---

## 12. curl 调用格式示例

### 12.1 调用边缘推理服务的格式

```bash
curl -X POST http://127.0.0.1:8001/edge/infer \
  -H "Content-Type: application/json" \
  -d '{
    "packet_id": "batch_000001",
    "device_id": "K001",
    "sensor_id": "sensor_K001",
    "sequence_number": 1,
    "start_timestamp_ns": 1781920800000000000,
    "end_timestamp_ns": 1781920800050000000,
    "duration_ms": 50,
    "data": {
      "data_type": "bearing_timeseries",
      "vibration_sample_rate_hz": 16000,
      "vibration_sample_count": 800,
      "vibration": [0.012, 0.018, 0.009, 0.021],
      "current": 1.34,
      "temperature": 45.8,
      "speed": 899.7,
      "load": 0.7
    }
  }'
```

注意：该示例用于展示 HTTP 调用格式，`vibration` 为了阅读方便只写了 4 个点。若边缘服务开启严格字段校验，该示例会因为数组长度不等于 `vibration_sample_count = 800` 被拒绝；正式联调时应使用周生成的 800 点样例 JSON。

### 12.2 调用健康检查接口

```bash
curl http://127.0.0.1:8001/health
curl http://127.0.0.1:8003/health
curl http://127.0.0.1:8004/health
curl http://127.0.0.1:8006/health
```
