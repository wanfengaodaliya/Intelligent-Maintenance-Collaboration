# 云端原始上下文请求接口调整设计

## 目标

云端向边缘发送原始上下文请求时，严格使用边缘确定的六字段接口，同时保留云端完成补传接收、连续性校验和超时收口所需的内部状态。

## 对外请求

```json
{
  "request_id": "ctx_req_000001",
  "sender_id": "sender_01",
  "anchor_packet_id": "batch_00001_71",
  "anchor_end_generate_timestamp_ns": 1781920800050000000,
  "before_packet_count": 20,
  "requested_at_ns": 1781920800800000000
}
```

不得向边缘发送 `task_id`、`anchor_sequence_number`、`after_packet_count` 或 `deadline_at_ns`。

## 数据来源与内部状态

- `anchor_end_generate_timestamp_ns` 从已落库触发包的 `raw_packet_index.end_generate_timestamp_ns` 读取。
- 读取键为 `(sender_id, anchor_packet_id)`；发送重试仍从该不可变触发包索引读取，因此时间戳保持一致。
- `task_id`、`anchor_sequence_number`、`after_packet_count=0`、`minimum_context_packet_count=16` 和 `deadline_at_ns=requested_at_ns+3s` 继续保存在云端 `raw_context_request` 中。
- 上述内部字段继续服务于批次归属、相对位置、前 20 包完整判定、16 包阈值和超时收口，不改变边缘补传批次接收接口。

## 异常处理

正常生产链路在创建请求前已经持久化触发包。若无法从索引找到触发包时间戳，不得发送缺字段或空时间戳请求；请求记为 `dispatch_failed`，错误码为 `ANCHOR_RAW_PACKET_NOT_FOUND`，可在触发包恢复后按原 `request_id` 重试。

## 验收

- 传输层收到的请求键集合与六字段接口完全相同。
- 时间戳等于触发包原始数据的 `end_generate_timestamp_ns`。
- 发送失败重试复用原 `request_id`，并重发相同时间戳。
- 原有前 20 包、16 包阈值、跨任务接收和数据库写入行为保持不变。
