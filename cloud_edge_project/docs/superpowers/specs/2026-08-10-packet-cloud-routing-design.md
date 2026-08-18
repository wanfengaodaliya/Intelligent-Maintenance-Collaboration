# 包级云边路径调度改造设计

## 目标与依据

本设计以 `D:\揭榜挂帅\DDPG_v2\resourses\6-ai交流\调度器任务调度职责与模块接口说明.md` 为接口契约依据，在当前项目中补齐包级云边路径选择、云端节点状态感知、延后上云和边缘上传结果回报。

本轮只实现文档“职责一：包级云边路径选择”。一秒汇总、三个轴承设备级比较、设备级本地/云端仲裁和汇总模块实现不在本轮范围内。

以下已经确认的项目约束优先于文档中的可选设计：

- 保留现有 `POST /scheduler/decide`，继续负责发送器申请边缘节点。
- 原始数据由当前边缘节点直接调用现有 `POST /cloud/infer` 上传，不经过调度器。
- 云端与调度器之间只增加云端节点状态上报；不增加云端任务 ACK 或执行状态回调。
- `/cloud/infer`、`/cloud/raw-context-batches` 和 `/cloud/edge-feature-summaries` 的原有确认语义保持不变。
- 边缘节点把云端原有响应中的轻量上传结果报告给调度器。
- 调度器持久化延后任务元数据，边缘节点持久化原始包，最长保留 24 小时。

## 兼容边界

现有发送器到边缘节点分配流程保持不变：

```text
发送器 -> POST /scheduler/decide
调度器 -> POST /edge/tasks
调度器 -> 发送器返回 target_topic
```

现有八字段 `/scheduler/decide` 请求、五字段响应、Top-1 分配、边缘 ACK、SQLite 幂等和失败重试语义不得改变。

新包级路由发生在边缘节点已经产生当前 50 ms 包分析结果以后，不复用也不替换 `/scheduler/decide`。

## 新增和扩展接口

### 1. 边缘到调度器：包级分析结果

```text
POST /scheduler/packet-route
```

请求字段采用目标文档 6.5：

```jsonc
{
  "device_id": "machine_01",
  "task_id": "sd_01_tk_0001",
  "bearing_id": "bearing_01",
  "edge_node_id": "edge_1",
  "error": null,
  "input_ref": {
    "device_id": "machine_01",
    "bearing_id": "bearing_01",
    "sender_id": "sender_01",
    "packet_id": "batch_00001_001",
    "sequence_number": 1
  },
  "status": "SUCCEEDED",
  "started_at_ns": 1784784400320000000,
  "finished_at_ns": 1784784400440000000,
  "output": {
    "edge_result": "warning",
    "confidence": 0.72,
    "task_complexity": 0.28,
    "edge_risk_level": "medium",
    "model_version": "edge_v1.0"
  }
}
```

约束：

- 顶层和 `input_ref` 中的 `device_id`、`bearing_id` 必须一致。
- `task_id` 必须对应现有已成功分配的任务；`edge_node_id` 必须是该任务的已分配节点。
- `sender_id` 必须与该任务绑定的发送器一致。
- `sequence_number` 范围为 1 至 80。
- `status` 只能为 `SUCCEEDED`、`FAILED` 或 `TIMEOUT`。
- `SUCCEEDED` 时 `error` 必须为 `null`，并要求完整 `output`。
- `FAILED` 或 `TIMEOUT` 时必须给出非空错误码；可以不提供 `output`。
- `confidence` 范围为 0 至 1。
- 调度器复算 `1 - confidence`；请求中的 `task_complexity` 与复算结果误差大于 `1e-6` 时拒绝请求，但不修改原始 `confidence`。
- 当前边缘推理接口保持原响应；边缘任务处理层从任务上下文和推理结果组装本请求。

### 2. 云端到调度器：节点状态

```text
POST /scheduler/cloud-nodes/status
```

请求主体采用目标文档 6.3，并补充用于多节点识别和快照引用的 `cloud_node_id`、`status_message_id`：

```jsonc
{
  "status_message_id": "msg_cloud_status_000001",
  "cloud_node_id": "cloud_01",
  "reported_at_ns": 1784784400100000000,
  "health_status": "ONLINE",
  "resources": {
    "logical_cpu_count": 8,
    "cpu_utilization_percent": 50.0,
    "memory_available_mb": 6144,
    "gpu_available": true,
    "npu_available": false,
    "queue_length": 1
  },
  "models": [
    {
      "model_version": "cloud_v1.0",
      "model_load_status": "LOADED"
    }
  ],
  "network_to_scheduler": {
    "measured_at_ns": 1784784400000000000,
    "available_uplink_mbps_estimate": 85.0,
    "rtt_ms_avg": 8.2,
    "rtt_ms_p95": 12.6,
    "loss_rate": 0.001
  },
  "last_task_activity_ns": 1784784400095000000
}
```

调度判断只使用：

- `health_status`
- `resources.queue_length`
- 所需模型的 `model_load_status`
- `reported_at_ns` 的时效性

CPU、GPU、NPU、内存和 `network_to_scheduler` 仅作为状态记录，不参与包级路径判断。当前数据持有方到云端的网络质量只使用 6.4 的目标链路快照。

云端服务每 1 秒上报一次；状态超过 5 秒未更新即视为过期。一次上报失败不影响云端原有推理接口，下一周期继续上报。

### 3. 网络模块到调度器：边缘到云端链路

继续使用现有：

```text
POST /scheduler/link-snapshots
```

现有发送器到边缘节点的链路结构保持兼容；当请求符合目标文档 6.4 且 `target_id` 是云节点时，写入独立的云端链路状态表：

```jsonc
{
  "sent_at_ns": 1784784400160000000,
  "link_id": "edge_1_to_cloud_01",
  "source_id": "edge_1",
  "target_id": "cloud_01",
  "measurement_status": "AVAILABLE",
  "connected": true,
  "measured_at_ns": 1784784400155000000,
  "goodput_mbps": 36.5,
  "rtt_ms_p50": 38.0,
  "rtt_ms_p95": 65.0,
  "jitter_ms": 6.2,
  "loss_rate": 0.002,
  "expires_at_ns": 1784784402155000000
}
```

`measurement_status=UNAVAILABLE` 时，各测量指标使用 `null`，不得使用零值模拟良好网络。

### 4. 调度器到边缘：重新触发云端复核

```text
POST /edge/cloud-review-tasks
```

请求主体采用目标文档 7.3：

```jsonc
{
  "decision_id": "decision_packet_000001",
  "cloud_task_id": "cloud_packet_task_000001",
  "device_id": "machine_01",
  "task_id": "sd_01_tk_0001",
  "bearing_id": "bearing_01",
  "packet_id": "batch_00001_001",
  "trigger_reasons": ["LOW_CONFIDENCE", "HIGH_COMPLEXITY"],
  "source": {
    "holder_id": "edge_1",
    "raw_data_ref": "edge-cache://edge_1/sd_01_tk_0001/bearing_01/batch_00001_001",
    "context_ref": "edge-cache://edge_1/sd_01_tk_0001/bearing_01/window_0001"
  },
  "target": {
    "cloud_node_id": "cloud_01",
    "endpoint": "/cloud/infer"
  },
  "created_at_ns": 1784784400550000000
}
```

`cloud_task_id` 是调度器为本次云端复核控制任务生成的稳定标识，不表示新增了云端到调度器的 ACK。云端仍返回项目现有的 `review_id`；边缘在上传结果报告中把两者关联。

立即上云时，`POST /scheduler/packet-route` 的同步响应已经包含目标和控制字段，边缘可以直接上传。该边缘接口主要用于条件恢复后的延后任务重新触发。

### 5. 边缘到调度器：云端上传结果

```text
POST /scheduler/cloud-upload-results
```

该接口的调用方是边缘节点，不是云端：

```jsonc
{
  "decision_id": "decision_packet_000001",
  "cloud_task_id": "cloud_packet_task_000001",
  "device_id": "machine_01",
  "task_id": "sd_01_tk_0001",
  "bearing_id": "bearing_01",
  "packet_id": "batch_00001_001",
  "edge_node_id": "edge_1",
  "upload_status": "SUCCESS",
  "review_id": "review_001",
  "reason_code": null,
  "reported_at_ns": 1784784401400000000
}
```

`upload_status` 取值：

- `SUCCESS`：云端原有接口成功返回并给出有效 `review_id`。
- `RETRYABLE_FAILED`：超时、连接失败、云端 5xx 或其他可恢复失败。
- `PERMANENT_FAILED`：请求结构等不可重试错误。

接口以 `decision_id` 为幂等键。重复的相同结果返回原保存结果；相同 `decision_id` 的不同身份或冲突状态返回 409。

## 包级路径决策

调度器的固定判断顺序如下。

### 第一步：业务必要性

```text
task_complexity = 1 - confidence

confidence >= 0.80
AND task_complexity <= 0.20
    -> DIRECT_FINAL_TO_SUMMARY

否则
    -> 需要云端复核
```

高置信度直接路径不依赖网络或云端状态；调度器不因缺少云端状态而阻塞本地正式结果。

### 第二步：云端执行条件

需要云端复核时，以下条件必须全部满足：

- 云端 `health_status=ONLINE`。
- 所需云模型 `model_load_status=LOADED`。
- 云端状态年龄不超过 5 秒。
- `queue_length <= 5`。
- 边缘到云端链路 `measurement_status=AVAILABLE` 且 `connected=true`。
- `goodput_mbps >= 2.0`。
- `rtt_ms_p95 <= 100.0`。
- `loss_rate <= 0.10`。
- 链路快照未过期。

全部满足：

```text
CLOUD_REVIEW_NOW
```

任一条件不满足：

```text
EDGE_PROVISIONAL_AND_DEFER_CLOUD
```

原因码至少覆盖：

- `HIGH_CONFIDENCE`
- `LOW_COMPLEXITY`
- `LOW_CONFIDENCE`
- `HIGH_COMPLEXITY`
- `NETWORK_UNAVAILABLE`
- `NETWORK_POOR`
- `CLOUD_OFFLINE`
- `CLOUD_OVERLOADED`
- `MODEL_NOT_READY`
- `STATUS_STALE`

## 包级路径响应

响应采用目标文档 7.2：

```jsonc
{
  "decision_id": "decision_packet_000001",
  "device_id": "machine_01",
  "task_id": "sd_01_tk_0001",
  "bearing_id": "bearing_01",
  "packet_id": "batch_00001_001",
  "sequence_number": 1,
  "route": "EDGE_PROVISIONAL_AND_DEFER_CLOUD",
  "needs_cloud_review": true,
  "deferred_cloud_review": true,
  "reason_codes": ["LOW_CONFIDENCE", "NETWORK_POOR"],
  "defer_reason": "NETWORK_POOR",
  "input_snapshot": {
    "confidence": 0.72,
    "task_complexity": 0.28,
    "network_snapshot_id": "edge_1_to_cloud_01",
    "cloud_status_message_id": "msg_cloud_status_000001"
  },
  "target": {
    "summary_module_id": "summary_01",
    "target_topic": "summary/packet-results",
    "cloud_node_id": "cloud_01",
    "endpoint": "/cloud/infer"
  },
  "created_at_ns": 1784784400500000000
}
```

不适用于当前路径的 `target` 子字段使用 `null`，不伪造不存在的目标。

路由布尔值必须一致：

- `DIRECT_FINAL_TO_SUMMARY`：`needs_cloud_review=false`、`deferred_cloud_review=false`。
- `CLOUD_REVIEW_NOW`：`needs_cloud_review=true`、`deferred_cloud_review=false`。
- `EDGE_PROVISIONAL_AND_DEFER_CLOUD`：`needs_cloud_review=true`、`deferred_cloud_review=true`，并要求 `defer_reason`。

## 延后上云状态与持久化

调度器 SQLite 保存：

- `decision_id`
- 包和任务身份字段
- `cloud_task_id`
- 路由和原因码
- 云状态、网络状态快照标识
- `edge_node_id`
- `raw_data_ref`、`context_ref`
- 当前状态
- 尝试次数
- `next_retry_at_ns`
- 创建、更新时间和过期时间
- 成功时的 `review_id`

状态机：

```text
PENDING
  -> DISPATCHING
  -> WAITING_RESULT
  -> SUCCEEDED

可恢复失败 -> PENDING
不可恢复失败 -> PERMANENT_FAILED
超过 24 小时 -> EXPIRED
```

重试间隔为 5、10、20、40、60 秒，之后每 60 秒重试。到达 24 小时以前不因重试次数用尽而静默删除任务。

调度器进程重启后扫描非终态任务并恢复调度。同一 `decision_id` 同一时刻只允许一个派发租约，避免多个工作线程重复触发。

## 边缘原始包持久化

边缘节点使用磁盘持久化存储待云端复核的原始包，不使用调度器数据库保存 `values`。

唯一身份：

```text
task_id + bearing_id + packet_id
```

规则：

- 低置信度包在完成调度决策前不得从缓存删除。
- `CLOUD_REVIEW_NOW` 或延后路径均确保原始包已落盘后再返回可执行状态。
- 收到 `SUCCESS` 并成功报告调度器后删除对应原始包。
- `PERMANENT_FAILED` 或 24 小时过期后释放原始数据，但保留轻量失败记录。
- 重复上传使用相同 `decision_id`、包身份和云端现有幂等语义。

## 云端改动边界

云端只增加后台节点状态上报组件，不修改现有业务接口：

- `POST /cloud/infer`
- `POST /cloud/device-arbitration`
- `POST /cloud/raw-context-batches`
- `POST /cloud/edge-feature-summaries`
- `GET /cloud/reviews/{review_id}/summary`

云端状态上报失败只记录日志并在下一个周期重试，不影响 `/cloud/infer` 可用性。

## 错误与恢复

- 无云端状态、云端状态过期、无链路状态或链路状态过期：选择延后路径，不返回 500。
- 高置信度直接路径不要求云端或网络状态存在。
- 包身份与现有任务分配不一致：返回 409。
- 非法枚举、非法时间戳、复杂度计算不一致：返回 400。
- SQLite 暂时忙：返回 503；不得产生部分决策记录。
- 边缘重触发接口超时属于结果不明；保留同一节点和同一 `decision_id` 重试，不切换数据持有方。
- 云端调用失败由边缘映射成 `RETRYABLE_FAILED` 或 `PERMANENT_FAILED`，调度器不解析云端完整业务结果。

## 预计修改范围

调度器：

- `scheduler/api.py`
- `scheduler/node_registry.py` 或独立云状态/链路注册表
- 新增包级路径决策模块
- 新增延后上云持久化仓库和恢复工作器
- 配置和调度器 README
- 调度器单元及契约测试

边缘服务：

- `edge_service/app.py`
- 新增云端复核控制入口
- 新增待上传原始包持久化存储
- 新增现有 `/cloud/infer` 调用适配器
- 新增上传结果报告客户端
- 边缘单元及契约测试

云端服务：

- `cloud_service/app.py` 生命周期中启动状态上报
- 新增云端状态上报客户端
- 云端状态上报测试

明确不改：

- 发送器八字段任务申请及 MQTT Topic 流程
- 现有 `/scheduler/decide` 请求和响应
- 云端现有推理、上下文、摘要和仲裁接口
- 一秒窗口汇总及设备级仲裁

## 验收标准

1. 现有发送器—调度器—边缘节点分配测试保持通过。
2. 包级请求严格采用目标文档 6.5 字段并校验任务绑定与复杂度公式。
3. 三种文档路由在决策矩阵中均有测试。
4. 高置信度直接路径在云端和网络状态缺失时仍能成功。
5. 低置信度任务只在网络、云端健康、模型和队列全部满足时立即上云。
6. 状态未知或过期进入延后队列，不把未知值当作良好。
7. 云端上报完整接收文档 6.3 字段，但调度负载只使用 `queue_length`。
8. 调度器不持久化高采样原始数据。
9. 延后任务和边缘原始包在进程重启后可恢复。
10. 边缘成功调用现有 `/cloud/infer` 后报告 `review_id`，调度器将任务标记成功并停止重试。
11. 重复请求、重复派发和重复上传不会创建不同身份的任务。
12. 24 小时过期后释放原始包，并保留可审计的轻量终态记录。
