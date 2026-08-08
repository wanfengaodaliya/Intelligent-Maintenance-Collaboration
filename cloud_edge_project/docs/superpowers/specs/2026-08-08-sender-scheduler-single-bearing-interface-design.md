# 发送器—调度器单轴承接口设计

## 目标

严格采用已确认图片中的接口：一个发送器的一次调度请求只描述一个设备上的一个轴承，并且只申请一个边缘节点。调度器统一评分后只选择 Top-1；Top-1 拒绝、超时或调用失败时，本次申请直接失败，不尝试第二名。

本次严格只修改调度器：调整调度请求校验、调度响应、内部持久化以及调度器向边缘节点发送的任务描述。旧的三轴承数组请求不再作为公开接口接受。发送器、MQTT 数据包和边缘服务代码均留待后续修改。

## 任务编号

调度器要求 `sender_id` 符合 `sender_<发送器编号>`，例如 `sender_01`；要求 `task_id` 严格符合：

```text
sd_<发送器编号>_tk_<四位任务序号>
```

示例：

```text
sender_01 的第 1 个任务 -> sd_01_tk_0001
sender_01 的第 2 个任务 -> sd_01_tk_0002
sender_02 的第 1 个任务 -> sd_02_tk_0001
```

调度器不仅校验字符串格式，还校验 `task_id` 中的发送器编号与请求的 `sender_id` 一致。四位任务序号范围为 `0001` 至 `9999`。任务编号的实际生成逻辑不在本次调度器修改范围内。

## 调度请求

发送器调用：

```text
POST /scheduler/decide
Content-Type: application/json
```

请求结构严格为：

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

字段约束：

- `device_id`、`sender_id`、`task_id`、`bearing_id` 必须是非空字符串。
- `task_id` 必须符合已确认的发送器编号和四位序号规则。
- `packet_size_bytes`、`expected_duration_ms`、`created_timestamp_ns` 必须是正整数。
- `expected_packet_count` 必须等于当前系统和边缘入口共同约定的 `80`。
- 不接受旧的 `bearings` 数组请求。

调度所需吞吐量按单个轴承计算：

```text
packet_size_bytes * expected_packet_count * 8
------------------------------------------------
expected_duration_ms / 1000 * 1,000,000
```

## 调度响应

成功响应严格为：

```json
{
  "device_id": "machine_01",
  "sender_id": "sender_01",
  "task_id": "sd_01_tk_0001",
  "bearing_id": "bearing_01",
  "target_topic": "edge/edge_1/input"
}
```

公开响应不返回 `edge_node_id`。调度器仍可在数据库内部保存 `edge_node_id`，用于幂等、结果校验和稳定性评分。

发送器后续应校验响应中的四个身份字段都与原请求一致，并且 `target_topic` 是非空字符串；本次不修改现有发送器响应校验代码。

## 调度器到边缘节点

调度器选中 Top-1 后调用现有边缘任务入口：

```text
POST {control_url}/edge/tasks
```

任务描述改成只包含本次轴承：

```json
{
  "task_id": "sd_01_tk_0001",
  "target_edge_node_id": "edge_1",
  "task_type": "BEARING_EDGE_INFERENCE",
  "input_ref": {
    "device_id": "machine_01",
    "expected_bearing_ids": ["bearing_01"],
    "assigned_bearings": [
      {
        "bearing_id": "bearing_01",
        "sender_id": "sender_01",
        "expected_packet_count": 80
      }
    ]
  },
  "dispatched_at_ns": 1781920800000000000
}
```

该结构满足现有边缘入口对轴承、发送器和数据包数量的校验，不再出现同一个 `sender_id` 被重复写入三个轴承的问题。

## 调度与错误语义

- 候选节点仍使用现有过滤、统一评分和排序逻辑。
- 每次请求只取排序后的第一个节点。
- 只向该节点调用一次任务登记接口。
- Top-1 返回 `REJECTED`、ACK 超时、无效 ACK 或调用异常时，task 和 attempt 均记录失败，本次 HTTP 请求返回失败。
- 不尝试第二名节点。
- 相同 `task_id`、相同请求内容在已经成功分配后再次请求，返回原来的单节点绑定。
- 相同 `task_id` 携带不同的设备、发送器、轴承或任务参数时，返回 `TASK_ID_CONFLICT`。

## 持久化兼容

调度器数据库以单个 `bearing_id` 和 `expected_packet_count` 作为新任务身份的一部分，并继续保存内部 `edge_node_id` 和 `target_topic`。

为避免破坏已有 SQLite 文件，现有历史扩展列可以保留；但新接口不再读取或生成三轴承批量分配。无法表示为新单轴承接口的历史批量任务在重复查询时返回 `TASK_ID_CONFLICT`，不会被静默转换。

## 修改范围

调度器侧：

- `scheduler/assignment_scheduler.py`
- `scheduler/task_repository.py`
- `scheduler/test_scheduler.py`
- 调度器 README 中的请求与响应示例

明确不修改：

- `sender_module/` 下的发送器配置、任务编号、请求构造、响应校验、MQTT 数据包和模拟调度器。
- `edge_service/` 下的边缘服务代码。
- 调度器之外的公共接口和业务模块。

现有发送器仍发送旧结构，因此在发送器后续完成接口修改之前，不能直接调用本轮修改后的调度器接口。边缘服务代码无需修改；调度器使用单轴承下发结构验证其现有接口兼容性即可。

本次不处理节点容量预留、未知链路策略、ACK 对账、安全认证等与接口调整无直接关系的生产化问题。

## 验收标准

1. 调度器接受图片中的八字段单轴承请求。
2. 调度器拒绝旧 `bearings` 数组、错误任务编号、发送器编号不一致和非 80 的包数量。
3. 调度器只调用 Top-1，一次请求只分配一个节点，失败不回退。
4. 调度器向边缘节点下发一个 `expected_bearing_ids` 项和一个 `assigned_bearings` 项。
5. 调度器成功响应严格包含图片中的五个字段，不包含 `edge_node_id` 或批量 `assignments`。
6. 相同请求幂等返回原绑定，相同 `task_id` 的不同请求返回 `TASK_ID_CONFLICT`。
7. 调度器测试以及调度器到现有边缘任务入口的契约测试通过。
8. `sender_module/` 和 `edge_service/` 没有代码改动。
