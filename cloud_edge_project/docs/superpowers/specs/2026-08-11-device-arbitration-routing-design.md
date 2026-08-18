# 设备级本地/云端仲裁调度设计

## 目标与依据

本设计以 `D:\揭榜挂帅\DDPG_v2\resourses\6-ai交流\调度器任务调度职责与模块接口说明.md` 为接口契约依据，在当前 `cloud_edge_project` 中补齐文档“职责二：设备级本地/云端仲裁选择”。

当前项目已经实现包级三路径调度：`DIRECT_FINAL_TO_SUMMARY`、`CLOUD_REVIEW_NOW`、`EDGE_PROVISIONAL_AND_DEFER_CLOUD`。本轮不重写包级路由，只在必要处补充临时结果状态字段。本轮重点新增设备级三路径调度：

- `LOCAL_FINAL`
- `CLOUD_ARBITRATION_NOW`
- `LOCAL_PROVISIONAL_AND_DEFER_CLOUD`

调度器仍然只承担控制平面职责：保存数据引用、状态和调度决策；不接收、不转发三轴承原始高采样数据。

## 当前代码边界

保留现有入口和职责：

- `POST /scheduler/decide`：发送器申请边缘节点，继续由 `AssignmentScheduler` 处理。
- `POST /scheduler/packet-route`：包级云边路径选择，继续由 `PacketRouter` 处理。
- `POST /scheduler/cloud-upload-results`：包级延后上云结果回写，继续由现有包级仓库处理。
- `POST /cloud/device-arbitration`：当前已有云端设备仲裁能力，但它属于 cloud 侧能力，不代表 scheduler 已经完成设备级仲裁调度。

新增能力放在 scheduler 内部，与包级路由并列，而不复用 `/scheduler/decide`。

## 新增接口

### 汇总模块到调度器：设备级仲裁路径评估

```text
POST /scheduler/device-arbitration-route
```

请求由汇总模块在收齐一个设备窗口的轴承级结果后发送。调度器只接收轻量结果和引用，不接收原始数据。

核心字段：

```jsonc
{
  "device_id": "device_01",
  "task_id": "task_00001",
  "summary_module_id": "summary_01",
  "expected_bearing_count": 3,
  "received_bearing_count": 3,
  "bearing_results": [
    {
      "bearing_id": "bearing_01",
      "bearing_result_id": "bearing_result_01",
      "result": "warning",
      "confidence": 0.88,
      "risk_level": "MEDIUM",
      "action_level": 1,
      "result_status": "FINAL"
    }
  ],
  "comparison": {
    "conflict": true,
    "conflict_type": "ACTION_LEVEL_DIVERGENCE",
    "action_level_min": 0,
    "action_level_max": 3,
    "action_level_span": 3,
    "aggregate_confidence": 0.72,
    "low_confidence_bearing_count": 1,
    "provisional_bearing_count": 1,
    "data_complete": true
  },
  "task_complexity": 0.28,
  "local_arbitration_supported": true,
  "source_refs": {
    "bearing_results_ref": "summary-store://task_00001/bearings",
    "provisional_result_ref": "summary-store://task_00001/device-result-v1"
  }
}
```

`source_refs` 是实现补充字段，用于让调度器生成云端设备仲裁控制指令。若请求未提供，调度器可以根据 `summary_module_id`、`task_id` 生成默认引用。

### 云端或汇总模块到调度器：设备级仲裁结果回写

```text
POST /scheduler/device-cloud-arbitration-results
```

该接口用于延后设备级云仲裁任务的最终状态回写。它与包级 `/scheduler/cloud-upload-results` 分离，避免把 `packet_id`、`sequence_number` 等包级字段强塞到设备级任务中。

## 路由规则

第一步只判断业务必要性：

```text
comparison.conflict == false
AND aggregate_confidence >= confidence_threshold
AND task_complexity <= 1 - confidence_threshold
AND data_complete == true
AND local_arbitration_supported == true
```

以上条件全部成立时，返回：

```text
route = LOCAL_FINAL
needs_cloud_arbitration = false
deferred_cloud_arbitration = false
```

任一条件不成立时，判定业务上需要云端设备级仲裁，原因码从以下集合生成：

- `RESULT_CONFLICT`
- `LOW_AGGREGATE_CONFIDENCE`
- `HIGH_COMPLEXITY`
- `INCOMPLETE_BEARING_RESULTS`
- `LOCAL_ARBITRATION_UNSUPPORTED`
- `HAS_PROVISIONAL_BEARING_RESULT`

第二步仅对“需要云端设备级仲裁”的窗口检查执行条件。复用现有 `CloudNodeRegistry` 的云节点状态和链路快照：

- 云节点不存在、离线、状态过期、模型未加载、`queue_length` 超阈值：不能立即上云。
- 链路不存在、不可用、未连接、吞吐过低、P95 RTT 过高、丢包率过高：不能立即上云。
- 状态未知或过期一律按不能立即上云处理。

执行条件满足时返回：

```text
route = CLOUD_ARBITRATION_NOW
needs_cloud_arbitration = true
deferred_cloud_arbitration = false
```

执行条件不满足时返回：

```text
route = LOCAL_PROVISIONAL_AND_DEFER_CLOUD
needs_cloud_arbitration = true
deferred_cloud_arbitration = true
```

## 返回结构

```jsonc
{
  "decision_id": "decision_device_xxx",
  "device_id": "device_01",
  "task_id": "task_00001",
  "route": "LOCAL_PROVISIONAL_AND_DEFER_CLOUD",
  "needs_cloud_arbitration": true,
  "deferred_cloud_arbitration": true,
  "reason_codes": ["RESULT_CONFLICT", "CLOUD_OVERLOADED"],
  "defer_reason": "CLOUD_OVERLOADED",
  "input_snapshot": {
    "conflict": true,
    "aggregate_confidence": 0.72,
    "task_complexity": 0.28,
    "network_snapshot_id": "edge_01_to_cloud_01",
    "cloud_status_message_id": "status_1"
  },
  "local_instruction": {
    "execute_local_arbitration": true,
    "result_status": "PROVISIONAL",
    "decision_mode": "LOCAL_FALLBACK",
    "use_conservative_action": true
  },
  "target": {
    "summary_module_id": "summary_01",
    "cloud_node_id": "cloud_01",
    "endpoint": "/cloud/device-arbitration"
  },
  "retry_required": true,
  "created_at_ns": 1800000000000000000
}
```

`LOCAL_FINAL` 时 `local_instruction.result_status` 为 `FINAL`，`decision_mode` 为 `LOCAL_ARBITRATION`，`retry_required=false`。`CLOUD_ARBITRATION_NOW` 时 `execute_local_arbitration=false`。

## 持久化设计

新增设备级延后上云仓库，不复用现有包级 `deferred_cloud_task` 表：

```text
scheduler/deferred_device_repository.py
```

表名建议：

```text
deferred_device_arbitration_task
```

保存字段包括：

- `decision_id`
- `cloud_task_id`
- `device_id`
- `task_id`
- `summary_module_id`
- `route`
- `reason_codes_json`
- `defer_reason`
- `cloud_status_message_id`
- `network_snapshot_id`
- `bearing_results_ref`
- `provisional_result_ref`
- `cloud_node_id`
- `endpoint`
- `state`
- `attempt_count`
- `next_retry_at_ns`
- `created_at_ns`
- `updated_at_ns`
- `expires_at_ns`
- `arbitration_result_json`
- `task_payload_json`

状态机沿用包级延后仓库思路：

```text
PENDING -> DISPATCHING -> WAITING_RESULT -> SUCCEEDED
                         -> PENDING
                         -> PERMANENT_FAILED
                         -> EXPIRED
```

## 调度与重试

新增设备级 dispatcher：

```text
scheduler/deferred_device_dispatcher.py
```

它从设备级延后仓库领取到期任务，调用汇总模块或当前结果持有方的设备级云仲裁任务接口。调度器只发送控制指令：

```jsonc
{
  "decision_id": "decision_device_xxx",
  "cloud_task_id": "cloud_device_task_xxx",
  "device_id": "device_01",
  "task_id": "task_00001",
  "trigger_reasons": ["RESULT_CONFLICT"],
  "source": {
    "holder_id": "summary_01",
    "bearing_results_ref": "summary-store://task_00001/bearings",
    "provisional_result_ref": "summary-store://task_00001/device-result-v1"
  },
  "target": {
    "cloud_node_id": "cloud_01",
    "endpoint": "/cloud/device-arbitration"
  },
  "created_at_ns": 1800000000000000000
}
```

具体数据面由汇总模块或结果存储把三轴承结果和上下文交给云端；调度器不搬运原始数据。

## 配置

设备级路由默认复用包级阈值：

- `confidence_threshold = 0.80`
- `max_cloud_queue_length = 5`
- `min_uplink_mbps = 2.0`
- `max_rtt_p95_ms = 100.0`
- `max_loss_rate = 0.10`
- `default_cloud_node_id = cloud_01`
- `cloud_endpoint = /cloud/device-arbitration`
- `summary_module_id = summary_01`

如果后续需要独立调整设备级阈值，可在 `configs/local.yaml` 增加 `device_arbitration_routing` 配置段；本轮先保持与包级一致，减少联调变量。

## 测试

新增测试覆盖：

- 不冲突、高 `aggregate_confidence`、低复杂度、数据完整且支持本地仲裁时返回 `LOCAL_FINAL`。
- 存在冲突时需要云端设备仲裁。
- `aggregate_confidence < 0.80` 时需要云端设备仲裁。
- `task_complexity != 1 - aggregate_confidence` 时拒绝请求。
- 云在线、模型就绪、`queue_length <= 5` 且链路良好时返回 `CLOUD_ARBITRATION_NOW`。
- 网络不可用、网络差、云过载、状态过期或未知时返回 `LOCAL_PROVISIONAL_AND_DEFER_CLOUD`。
- 设备级延后仓库创建幂等、冲突请求拒绝、成功结果回写、失败重试和过期状态。
- API 路径出现在 FastAPI OpenAPI 文档中。

## 不在本轮范围

- 不实现汇总模块内部的 1 秒窗口聚合。
- 不实现三轴承原始数据上传。
- 不改变云端 `/cloud/device-arbitration` 的业务仲裁规则。
- 不把设备级逻辑合并进 `/scheduler/decide`。
- 不引入 `load_status` 或 CPU/GPU/memory 等额外负载判断；云端负载只看 `queue_length`。
