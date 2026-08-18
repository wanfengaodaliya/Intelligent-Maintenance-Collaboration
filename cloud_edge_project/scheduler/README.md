# Scheduler 调度器说明

## 当前职责

当前 HTTP 调度服务首先实现发送器的边缘节点申请流程：

```text
边缘节点上报实时状态
→ 网络模块提交链路快照（当前只预留接收）
→ 发送器提交任务级调度请求
→ 调度器过滤并评分边缘节点
→ 调度器向候选边缘节点请求任务确认
→ 节点返回 ACCEPTED
→ 调度器保存任务绑定并向发送器返回该轴承的 MQTT Topic
```

实时节点状态和链路快照只保存在内存中。任务分配、分配尝试和执行结果保存在 SQLite 中。

## HTTP 接口

调度服务默认运行在 `127.0.0.1:8003`。

```text
GET  /health
POST /scheduler/edge-nodes/status
POST /scheduler/link-snapshots
POST /scheduler/decide
POST /scheduler/tasks/result
```

发送器调用：

```text
POST http://127.0.0.1:8003/scheduler/decide
```

请求示例：

```json
{
  "device_id": "machine_01",
  "sender_id": "sender_01",
  "task_id": "sd_01_tk_0001",
  "bearing_id": "bearing_01",
  "packet_size_bytes": 100000,
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
  "target_topic": "edge/edge_1/input"
}
```

调度器只接受上述八字段单轴承请求。旧的 `bearings` 数组请求会返回 `INVALID_REQUEST`；无法表示为新单轴承接口的历史任务在重复查询时会返回 `TASK_ID_CONFLICT`。

## 节点配置

默认只注册当前真实边缘节点：

```text
edge_1 → http://127.0.0.1:8001 → edge/edge_1/input
```

部署时可通过 `SCHEDULER_EDGE_NODES_JSON` 整体替换。例如：

```json
{
  "edge_1": {
    "control_url": "http://10.0.0.11:8001",
    "target_topic": "edge/edge_1/input"
  }
}
```

调度器向选中节点发送：

```text
POST {control_url}/edge/tasks
```

边缘节点接口由边缘模块提供，本目录只负责发出请求和校验 ACK。
该接口必须以 `task_id` 为幂等键；重复收到同一任务时应返回原确认结果，不能重复创建任务上下文。

网络模块尚未提交某个“发送器—边缘节点”快照时，调度器暂时使用 50 分的中性网络分；收到有效快照后，最终评分会改用实际 RTT、吞吐量和 MQTT 发布成功率。

## 运行

项目统一启动：

```powershell
python start_all.py
```

只启动调度器：

```powershell
python start_all.py --service scheduler_service
```

也可以直接运行：

```powershell
python scheduler/api.py
```

## 旧任务调度逻辑

`rule_scheduler.py` 仍保留边缘初检后的 edge/cloud 规则代码。本次没有修改该部分，也没有继续占用 `/scheduler/decide` 接口。
