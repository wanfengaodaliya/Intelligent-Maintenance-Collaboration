# 发送器—调度器单轴承接口设计

## 目标

严格采用已确认图片中的接口：一个发送器的一次调度请求只描述一个设备上的一个轴承，并且只申请一个边缘节点。调度器统一评分后只选择 Top-1；Top-1 拒绝、超时或调用失败时，本次申请直接失败，不尝试第二名。

本次修改同时对齐发送器配置、任务编号、调度请求、调度响应、MQTT 数据包身份字段以及调度器向边缘节点发送的任务描述。旧的三轴承数组请求不再作为公开接口接受。

## 发送器固定身份

每个发送器配置固定包含以下三个身份字段：

```json
{
  "device_id": "machine_01",
  "sender_id": "sender_01",
  "bearing_id": "bearing_01"
}
```

同一次任务的调度请求、调度响应和 MQTT 数据包必须使用完全相同的 `device_id`、`sender_id`、`bearing_id` 和 `task_id`。

## 任务编号

`sender_id` 必须符合 `sender_<发送器编号>`，例如 `sender_01`。任务编号由发送器本地持久化计数器生成，格式严格为：

```text
sd_<发送器编号>_tk_<四位任务序号>
```

示例：

```text
sender_01 的第 1 个任务 -> sd_01_tk_0001
sender_01 的第 2 个任务 -> sd_01_tk_0002
sender_02 的第 1 个任务 -> sd_02_tk_0001
```

调度器不仅校验字符串格式，还校验 `task_id` 中的发送器编号与请求的 `sender_id` 一致。四位任务序号范围为 `0001` 至 `9999`；超过范围时发送器明确报错，不回绕覆盖旧任务。

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

发送器必须校验响应中的四个身份字段都与原请求一致，并且 `target_topic` 是非空字符串；任何不一致都视为无效调度响应，不开始发送 MQTT 数据。

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

## MQTT 数据包

发送器生成的每个 MQTT 数据包增加并携带：

- `device_id`
- `bearing_id`
- 新格式 `task_id`
- 原有 `sender_id`

这样边缘入口可使用调度任务中登记的身份信息校验实际数据包。现有数据内容、80 包发送过程、QoS 和目标 Topic 行为不变。

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

发送器侧：

- `sender/config.py` 和 `config/local.json`
- `sender/ids.py`
- `sender/controller.py`
- `sender/packet.py`
- `sender/scheduler_client.py`
- `tools/mock_scheduler.py`
- 对应 README 和新增接口测试

调度器侧：

- `scheduler/assignment_scheduler.py`
- `scheduler/task_repository.py`
- `scheduler/test_scheduler.py`
- 调度器 README 中的请求与响应示例

边缘服务现有接口无需修改；使用单轴承下发结构验证兼容性即可。

本次不处理节点容量预留、未知链路策略、ACK 对账、安全认证等与接口调整无直接关系的生产化问题。

## 验收标准

1. `sender_01` 首次生成任务编号 `sd_01_tk_0001`，后续依次递增。
2. 发送器产生的请求字段和值严格符合图片。
3. 调度器拒绝旧 `bearings` 数组、错误任务编号、身份不一致和非 80 的包数量。
4. 调度器只调用 Top-1，一次请求只分配一个节点，失败不回退。
5. 调度器向边缘节点下发一个 `expected_bearing_ids` 项和一个 `assigned_bearings` 项。
6. 调度器成功响应严格包含图片中的五个字段，不包含 `edge_node_id` 或批量 `assignments`。
7. 发送器拒绝身份字段不一致或缺少 `target_topic` 的响应。
8. MQTT 数据包包含与调度请求一致的设备、轴承、发送器和任务身份。
9. 调度器、发送器以及相关边缘接口测试通过。
