# 前置上下文阈值设计

## 1. 目标

将原始上下文补传从“触发包前 10 包、后 10 包，严格收齐 20 包”调整为：

- 只请求触发包前 20 个连续包；
- 不再使用 `task_id` 判断补传批次是否属于本次复核；
- 以相同 `sender_id`、连续序号和连续时间戳作为上下文归属与连续性条件；
- 优先等待 20 包全部到达；截止时间到达时，连续有效前置包达到 16 包也可进入聚合。

## 2. 固定参数

```text
before_packet_count = 20
after_packet_count = 0
minimum_context_packet_count = 16
```

触发包不计入上述 16 或 20 个上下文包。完整聚合窗口为“前置 20 包 + 触发包”，共 21 包；降级聚合窗口为“前置 16～19 包 + 触发包”。

## 3. 上下文选择规则

上下文包必须同时满足：

1. `sender_id` 与触发包相同；
2. `sequence_number` 位于 `anchor_sequence_number - 20` 至 `anchor_sequence_number - 1`；
3. 实际用于聚合的包形成紧邻触发包的连续后缀，例如 `-16 ... -1`；
4. 相邻包时间戳连续；
5. 包数据、采样参数、身份字段和内容哈希校验通过。

`task_id` 仅表示包所属业务任务，用于存储和追溯，不参与请求匹配、连续性判断或聚合资格判断。补传批次的 `task_id` 可以不同于触发包的 `task_id`。

批次内的包继续继承批次公共字段 `task_id` 和 `sender_id`。若前置 20 包跨越多个任务，边缘按实际 `task_id` 拆成多个批次；这些批次共享同一个 `request_id`、`sender_id` 和锚点字段。

## 4. 状态机

### 4.1 截止时间前

- 连续前置包少于 20：保持 `pending_context`，即使已经达到 16 包也不提前形成聚合资格；
- 连续前置 20 包全部入库：立即设置 `complete`，形成一次性终态聚合资格。

### 4.2 截止时间到达

- 连续前置包为 20：保持 `complete`；
- 连续前置包为 16～19：设置 `partial_context`，形成一次性终态降级聚合资格；
- 连续前置包少于 16：设置 `insufficient_context`，不进入聚合；
- 总包数达到阈值但连续后缀不足 16：设置 `insufficient_context`，不得用更早的非连续包凑数。

`complete`、`partial_context` 和 `insufficient_context` 都是不可逆终态，其中 `complete` 和 `partial_context` 是一次性终态聚合资格状态。`pending_context` 和 `insufficient_context` 不具备聚合资格。

## 5. 请求与接口调整

### 5.1 云端请求边缘

`RawContextRequest` 发送：

```json
{
  "before_packet_count": 20,
  "after_packet_count": 0
}
```

其余请求字段不变。请求中的 `task_id` 仍记录触发包所属任务，但不再限制补传批次的任务。

### 5.2 边缘上传批次

只允许 `context_position="before"`。批次最多仍携带 10 包，因此完整 20 包至少分两个批次上传。`after` 批次应作为无效信封拒绝。

批次级请求匹配只比较：

- `request_id`；
- `sender_id`；
- `anchor_packet_id`；
- `anchor_sequence_number`。

不比较批次 `task_id` 与请求 `task_id`。

### 5.3 云端对边缘回执

成功回执仍只包含：

```json
{
  "request_id": "ctx_req_000001",
  "batch_id": "ctx_req_000001:before:1",
  "status": "accepted",
  "context_status": "pending_context",
  "results": []
}
```

`context_status` 可为 `pending_context`、`complete`、`partial_context` 或 `insufficient_context`。`review_id` 和 `context_ready` 继续只在云端内部使用，不返回边缘。

## 6. 数据库与聚合交接

数据库状态约束增加 `partial_context`。请求表和复核表必须同步保存相同的上下文状态。

达到 `complete` 或在截止时间扫描时达到 `partial_context` 后，持久化状态表明该 `review_id` 具备一次性聚合资格。下游聚合模块消费 `review_id`，按相对位置读取已关联的连续前置包和触发包并按 `sequence_number` 排序；聚合作业的调用和幂等性均由下游聚合模块负责。

本模块只建立正确的聚合资格与持久化状态，不入队、不调用聚合作业，也不修改趋势、谐波等聚合算法。因此本模块测试只覆盖资格状态持久化，不声称覆盖实际聚合触发。

## 7. 错误与终态规则

- `sender_id` 不匹配：`CONTEXT_REQUEST_MISMATCH`；
- 上传 `after` 批次：`INVALID_CONTEXT_BATCH`；
- 序号超出前置 20 包范围：`CONTEXT_SEQUENCE_OUT_OF_RANGE`；
- 截止时间前边缘明确报告缺失：保留缺失记录，但由云端按连续后缀和 16 包阈值决定 `partial_context` 或 `insufficient_context`，不得无条件立即判定不足；
- 终态后到达的批次不改变或重复产生聚合资格；聚合作业幂等由下游负责，补传幂等、冲突和不覆盖原数据规则保持不变。

## 8. 测试要求

必须覆盖：

1. 创建请求时为前 20、后 0；
2. 不同 `task_id`、相同 `sender_id` 的批次可以入库；
3. 不同 `sender_id` 仍被拒绝；
4. `after` 批次被拒绝；
5. 前 20 包全部到达时立即 `complete`；
6. 16～19 包到达后在截止时间前仍为 `pending_context`；
7. 截止时间到达且连续包为 16～19 时转为 `partial_context`；
8. 截止时间到达且连续包少于 16 时转为 `insufficient_context`；
9. 总数达到 16 但紧邻触发包的连续后缀不足 16 时不可聚合；
10. `complete` 和 `partial_context` 不可回退且不重复产生资格；
11. 现有幂等、冲突、时间戳和边缘回执字段测试继续通过；
12. 正式方案文档与代码接口、状态和测试保持一致。
