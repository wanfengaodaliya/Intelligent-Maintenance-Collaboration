# 边缘单包云端复核闭环设计
<!-- 本文档定义边缘单包路由、延期云复核和结果回报的闭环设计。 -->

## 1. 文档目的

本文定义如何把已经存在但尚未接通的边缘单包分析、调度器包级路由、延期云端复核、边缘原始包上传和结果回报组成一个可恢复、可测试的完整闭环。

本文是实现前的设计约束，不代表相关代码已经完成。获得用户确认后，下一阶段先编写测试驱动实施计划，再按照 RED、GREEN、REFACTOR 的顺序修改代码。

## 2. 问题背景与当前证据

当前检出的实际服务启动链是：

```text
start_all.py
  -> uvicorn scheduler.api:app
  -> uvicorn edge_service.app:app
  -> uvicorn cloud_service.app:app
```

代码中已经存在以下能力：

- `scheduler/packet_router.py`：包级三路径决策。
- `scheduler/packet_service.py`：包级决策与延期任务持久化。
- `scheduler/deferred_cloud_repository.py`：SQLite 延期任务状态机。
- `scheduler/deferred_dispatcher.py`：向边缘派发到期的云端复核控制消息。
- `edge_service/src/packet_routing_bridge.py`：把边缘单包结果适配为 `/scheduler/packet-route` 合同。
- `edge_service/src/cloud_review/`：边缘原始包落盘、云端上传和调度器结果回报。
- `POST /edge/cloud-review-tasks`：接收调度器的云端复核控制消息。

但是实际运行链存在四个断点：

1. 当前 `scheduler.api:app` 只注册初始边缘节点分配相关接口，没有实例化包级路由服务，也没有注册 `/scheduler/packet-route`、`/scheduler/cloud-nodes/status` 和 `/scheduler/cloud-upload-results`。
2. 当前调度器应用没有把 `DeferredCloudDispatcher` 放入 FastAPI 生命周期，因此 SQLite 中的 `PENDING` 任务不会自动派发。
3. `PacketRoutingBridge` 没有注入边缘运行时；单包推理完成后只进入当前边缘聚合流程，不会把正式单包结果提交给调度器。
4. 派发器在边缘 HTTP 调用返回后无条件执行 `mark_dispatched()`。边缘可能在同一个 HTTP 调用中先上传云端并同步回报 `RETRYABLE_FAILED`，使任务已经从 `DISPATCHING` 回到 `PENDING`，随后 `mark_dispatched()` 会抛出 `INVALID_DEFERRED_STATE`。

此外，边缘运行时默认把节点状态发往 `/scheduler/edge-status`，而当前调度器公开的是 `/scheduler/edge-nodes/status`，需要统一合同。

## 3. 目标

实现以下可验证闭环：

```text
边缘完成单包推理
  -> 边缘持久化原始包和单包结果
  -> 边缘提交 POST /scheduler/packet-route
  -> 调度器校验任务分配并选择包级路径
  -> 需要云端复核时写入 SQLite 延期任务
  -> 调度器生命周期内的派发器领取到期任务
  -> 调度器提交 POST /edge/cloud-review-tasks
  -> 边缘读取已落盘原始包并调用 POST /cloud/infer
  -> 边缘提交 POST /scheduler/cloud-upload-results
  -> 调度器进入成功、永久失败或退避重试状态
```

完成后应满足：

- 单包结果能从真实边缘运行时进入包级调度器，而不是只存在一个未调用的适配器类。
- 延期任务在调度器启动后自动恢复和派发。
- 同步回报不会与派发器产生状态覆盖或异常。
- 调度器不可达、边缘不可达、云端暂时失败和进程重启时，必要数据不会被静默丢失。
- 原有初始边缘节点分配合同和设备级三轴承仲裁不被混入本次改动。

## 4. 非目标和边界

本次明确不做以下改动：

- 不改变 `POST /scheduler/decide` 的请求、响应、节点筛选、预留、边缘确认和 `target_topic` 语义。
- 不把调度器变成高采样原始数据中转站；原始包由边缘数据持有方直接上传云端。
- 不修改设备级三轴承仲裁策略、三轴承结果聚合时机或设备级延期派发器。
- 不引入消息队列、事件总线或新的外部基础设施。
- 不改变当前包级置信度阈值、云队列阈值、网络阈值和 24 小时保留期。
- 不以健康接口或静态演示页面代替单包闭环验证。

## 5. 总体架构

### 5.1 调度器运行时容器

调度器应创建一个集中管理包级运行组件的运行时容器。容器负责持有并连接：

- `CloudNodeRegistry`
- `NodeRegistry`
- `TaskRepository`
- `AssignmentScheduler`
- `DeferredCloudRepository`
- `PacketRouter`
- `PacketRoutingService`
- `DeferredCloudDispatcher`

容器提供明确的 `start()` 和 `stop()` 生命周期：

- `start()` 启动节点监控器和包级延期派发器。
- 派发器启动时调用 `recover_non_terminal()`，恢复重启前处于 `DISPATCHING` 或 `WAITING_RESULT` 的非终态任务。
- `stop()` 先通知后台 worker 停止，再等待线程退出。
- 重复调用 `start()` 或 `stop()` 必须安全且幂等。

FastAPI 应用通过 lifespan 调用该容器，而不是在模块导入时启动业务后台线程。直接执行 `scheduler/api.py` 的标准库 HTTP 入口也必须调用同一运行时容器，避免两种启动方式行为不一致。

### 5.2 调度器 HTTP 接口

恢复并接通以下包级控制接口：

```text
POST /scheduler/packet-route
POST /scheduler/cloud-nodes/status
POST /scheduler/cloud-upload-results
```

`POST /scheduler/link-snapshots` 需要按负载字段区分两种链接：

- 现有发送器到边缘节点快照继续交给 `NodeRegistry`。
- 包含 `link_id`、`source_id`、`target_id` 的边缘到云端快照交给 `CloudNodeRegistry`。

包级接口错误映射应保留各领域错误码：

- 非法包级结果：400。
- 包身份与现有分配不一致：409。
- 延期任务身份或终态冲突：409。
- SQLite 暂时繁忙：503。
- 未分类内部错误：500。

### 5.3 边缘单包适配

边缘运行时应在每个 `PacketExecutionCompleted` 事件中完成以下顺序：

1. 从任务接入记录和校验缓存取得与完成事件一致的原始包身份及原始包内容。
2. 先把原始包和正式边缘结果写入 `CloudReviewStore`。
3. 组装正式包级路由请求并提交 `/scheduler/packet-route`。
4. 继续执行现有轴承窗口聚合，不把包级路由与轴承聚合合并为一个调度层。

包级适配器应直接消费 `PacketExecutionCompleted`，不再依赖已经失去时间戳和失败状态信息的旧简化结果。正式请求字段保持为：

```json
{
  "device_id": "device_01",
  "task_id": "task_01",
  "bearing_id": "bearing_01",
  "edge_node_id": "edge_01",
  "input_ref": {
    "device_id": "device_01",
    "bearing_id": "bearing_01",
    "sender_id": "sender_01",
    "packet_id": "packet_01",
    "sequence_number": 1
  },
  "status": "SUCCEEDED",
  "started_at_ns": 1784784400320000000,
  "finished_at_ns": 1784784400440000000,
  "error": null,
  "output": {
    "edge_result": "warning",
    "confidence": 0.72,
    "task_complexity": 0.28,
    "edge_risk_level": "medium",
    "model_version": "edge_v1.0"
  }
}
```

失败或超时完成事件必须发送 `status=FAILED` 或 `TIMEOUT`、非空 `error`，并省略 `output`。成功事件的 `task_complexity` 由边缘适配器计算为 `1 - confidence`；调度器仍会独立复算并校验误差。

### 5.4 边缘原始包持久化

调度器只保存 `raw_data_ref` 和 `context_ref`，不保存原始采样数组。边缘 `CloudReviewStore` 保存：

- 原始包。
- 与该包身份一致的边缘结果。
- 保存时间和过期时间。
- 云端调用成功后的轻量决策检查点。

原始包必须在提交包级路由请求之前落盘。这样即使调度器请求失败，数据仍可恢复和重试。

本次实现至少保证以下释放规则：

- 云端复核成功且调度器成功接收结果回报后释放原始包。
- 永久失败完成并成功回报调度器后释放原始包。
- 可重试失败继续保留原始包。
- 达到保留期后由既有清理 worker 释放过期数据。

### 5.5 云状态与边云链路

包级即时云复核需要同时具有新鲜的云节点状态和边缘到云端链路快照。本次恢复：

- 云服务周期性向 `/scheduler/cloud-nodes/status` 上报状态。
- 网络模块或测试夹具向 `/scheduler/link-snapshots` 上报边缘到云端链路。
- 无状态、状态过期、云离线、模型未加载、云队列超限、无链路或链路不合格时，调度器选择延期路径，而不是假定状态良好。

如果当前实际运行环境尚无生产边云链路上报源，包级闭环仍可进入可靠延期队列；验证时使用明确的真实接口请求注入链路快照，不伪造“生产已接通”的结论。

## 6. 包级路由语义

保留现有三条路径：

### 6.1 `DIRECT_FINAL_TO_SUMMARY`

条件：边缘结果置信度达到阈值且复杂度足够低。

结果：

- `needs_cloud_review=false`
- `deferred_cloud_review=false`
- 结果指令为边缘正式结果。
- 不创建延期任务。

### 6.2 `CLOUD_REVIEW_NOW`

条件：业务上需要云端复核，且云节点、模型、队列和边云链路均满足即时执行条件。

结果：

- `needs_cloud_review=true`
- `deferred_cloud_review=false`
- 结果指令仍为边缘临时结果。
- 调度器创建云复核控制任务。

为保持单一可靠执行路径，所有需要云端复核的任务都先写入同一个 SQLite 任务仓库。即时路径以可立即领取的任务表达，延期路径以条件未恢复前无法通过资格检查的任务表达。边缘不应在收到路由响应后再绕过该仓库发起第二条独立上传链路，否则会造成重复云端调用。

### 6.3 `EDGE_PROVISIONAL_AND_DEFER_CLOUD`

条件：业务上需要云端复核，但即时云端执行条件不满足。

结果：

- `needs_cloud_review=true`
- `deferred_cloud_review=true`
- 结果指令为边缘临时结果。
- 创建 `PENDING` 任务并等待资格检查恢复。

## 7. 延期状态机与竞争消除

### 7.1 状态机

```text
PENDING
  -> DISPATCHING
  -> WAITING_RESULT
  -> SUCCEEDED

DISPATCHING 或 WAITING_RESULT
  -> PENDING               可重试失败
  -> PERMANENT_FAILED      不可重试失败
  -> SUCCEEDED             云端复核成功

任何非终态
  -> EXPIRED               达到保留期
```

重试间隔继续使用 5、10、20、40、60 秒，之后每次 60 秒，直到成功、永久失败或 24 小时过期。

### 7.2 派发时序

`dispatch_once()` 的正确时序：

1. 原子领取一个到期的 `PENDING` 任务，使其进入 `DISPATCHING`。
2. 重新检查云节点和边云链路资格；不满足时直接安排下一次重试。
3. 调用边缘 `/edge/cloud-review-tasks`。
4. HTTP 请求异常时，若任务仍是可派发非终态，则安排调度器侧重试。
5. HTTP 正常返回后重新读取数据库中的权威状态。
6. 仅当状态仍为 `DISPATCHING` 时推进到 `WAITING_RESULT`。
7. 如果同步回报已经把状态改为 `PENDING`、`SUCCEEDED` 或 `PERMANENT_FAILED`，直接返回当前状态，不再执行 `mark_dispatched()`。

### 7.3 同步回报竞争

边缘的 `/edge/cloud-review-tasks` 当前会在一个 HTTP 调用内完成云端调用和 `/scheduler/cloud-upload-results` 回报。因此允许以下合法时序：

```text
调度器：PENDING -> DISPATCHING
调度器：POST /edge/cloud-review-tasks
边缘：  POST /cloud/infer 失败
边缘：  POST /scheduler/cloud-upload-results(RETRYABLE_FAILED)
调度器：DISPATCHING -> PENDING
边缘：  HTTP 返回调度器
调度器：读取当前 PENDING，不再 mark_dispatched
```

这不是异常状态，而是同步回报模式下的正常完成顺序。

## 8. 生命周期与线程管理

### 8.1 调度器启动

FastAPI lifespan 启动顺序：

1. 初始化运行时组件。
2. 启动节点监控器。
3. 恢复延期任务。
4. 启动延期派发线程。
5. 开始接收 HTTP 请求。

### 8.2 调度器关闭

关闭顺序：

1. 阻止延期派发器领取新任务。
2. 等待当前派发线程退出。
3. 停止节点监控器。

测试不得依赖 `atexit` 完成正常生命周期清理。`atexit` 只作为直接进程退出的后备保护。

### 8.3 边缘启动

边缘运行时启动时建立：

- MQTT 单包接入。
- 模型处理管线。
- `CloudReviewStore`。
- 包级路由 HTTP 客户端。
- 云端复核接收接口。
- 过期原始包清理 worker。

清理 worker 也应由 FastAPI lifespan 管理，避免模块导入时启动线程。

## 9. 接口和配置统一

### 9.1 边缘节点状态接口

统一使用：

```text
POST /scheduler/edge-nodes/status
```

边缘运行时 `SchedulerConfig.status_path` 默认值改为该路径。调度器不再新增 `/scheduler/edge-status` 别名，避免长期保留两个合同。

### 9.2 节点标识

本检出统一采用：

```text
edge_01
cloud_01
```

包级路由默认云节点标识必须与云状态上报和边云链路目标一致，不能继续混用 `cloud_1` 与 `cloud_01`。

### 9.3 配置项

在 `configs/local.yaml` 恢复并保留：

```yaml
packet_routing:
  confidence_threshold: 0.80

cloud_node:
  max_queue_length: 5
  status_ttl_seconds: 5

cloud_network:
  min_uplink_mbps: 2.0
  max_rtt_p95_ms: 100.0
  max_loss_rate: 0.10

deferred_cloud_review:
  retention_hours: 24
  dispatcher_interval_seconds: 1.0
  cleanup_interval_seconds: 60.0
```

这些值是恢复现有已设计阈值，不在本次工作中重新调参。

## 10. 错误处理

### 10.1 边缘提交包级结果失败

- 原始包已经落盘，不得删除。
- 当前单包处理记录标记路由上报失败并保留可审计错误码。
- 本次实现不创建第二套边缘本地调度队列；调度器接口短暂失败由有限重试处理，仍失败时保留原始包和错误记录，供后续恢复机制或人工重放。

### 10.2 调度器到边缘派发失败

- 超时映射为 `EDGE_DISPATCH_TIMEOUT`。
- 网络请求失败映射为 `EDGE_UNREACHABLE`。
- 其他派发失败映射为 `EDGE_DISPATCH_FAILED`。
- 任务回到 `PENDING` 并按现有退避重试。

### 10.3 边缘到云端失败

- 超时、连接错误和云端 5xx 回报 `RETRYABLE_FAILED`。
- 非法请求和不可解析的成功响应回报 `PERMANENT_FAILED`。
- 成功必须包含非空 `review_id`，否则按不可恢复的非法云响应处理。

### 10.4 调度器结果回报失败

边缘在云端已经成功但回报调度器失败时，必须保留 `CLOUD_SUCCEEDED` 检查点和原始包。下一次相同 `decision_id` 重试时不得重复调用云端，只重试调度器回报；回报成功后再释放原始包。

## 11. 测试策略

所有生产改动必须先有能准确复现缺失行为的失败测试。

### 11.1 调度器 API 与生命周期测试

测试以下行为：

- 应用包含 `/scheduler/packet-route`。
- 应用包含 `/scheduler/cloud-nodes/status`。
- 应用包含 `/scheduler/cloud-upload-results`。
- FastAPI lifespan 启动运行时和派发器。
- lifespan 退出停止运行时和派发器。
- 标准库 HTTP 入口使用同一启动和停止逻辑。
- `/scheduler/decide` 的现有合同保持通过。

### 11.2 状态竞争回归测试

使用真实临时 SQLite 仓库和受控边缘客户端验证：

- 边缘同步回报 `RETRYABLE_FAILED` 后，`dispatch_once()` 不抛异常且最终状态为 `PENDING`。
- 边缘同步回报 `SUCCESS` 后，最终状态为 `SUCCEEDED`。
- 边缘同步回报 `PERMANENT_FAILED` 后，最终状态为 `PERMANENT_FAILED`。
- 边缘只确认接收但没有同步回报时，最终状态为 `WAITING_RESULT`。
- HTTP 请求异常时，最终状态为带退避时间的 `PENDING`。

测试断言真实仓库状态，不只断言模拟客户端被调用。

### 11.3 边缘单包接线测试

使用真实 `CloudReviewStore` 和受控调度器 HTTP 边界验证：

- 成功单包先落盘，再提交正式包级请求。
- 失败单包提交正确的失败合同。
- 调度器不可达时原始包仍存在。
- 请求中的身份、序号、时间戳、置信度、复杂度和模型版本来自同一个完成事件。
- 现有轴承聚合仍收到该完成事件。

### 11.4 合同与幂等测试

- 包身份与已分配任务不一致返回 409。
- 相同 `decision_id` 和相同内容重复请求不创建第二个任务。
- 相同 `decision_id` 的冲突内容返回 409。
- `cloud_01` 标识在路由、状态和链路中一致。
- 边缘心跳调用 `/scheduler/edge-nodes/status`。

### 11.5 进程内闭环测试

使用临时 SQLite、临时边缘存储和进程内 HTTP 测试客户端执行：

1. 创建已分配任务。
2. 保存一个低置信度边缘包结果。
3. 注入不合格条件，使其进入延期 `PENDING`。
4. 注入合格云状态和边云链路。
5. 运行一次派发。
6. 边缘调用受控云端并回报成功。
7. 断言调度器任务为 `SUCCEEDED`。
8. 断言边缘原始包已释放。

该测试验证代码内闭环，不等同于外部 MQTT broker、真实网络模拟器和真实云模型的生产 E2E。

## 12. 验收标准

实现只有在以下证据齐全时才可声明完成：

1. 当前 `scheduler.api:app` 实际暴露全部三个包级接口。
2. 应用生命周期测试证明派发器会随调度器启动和停止。
3. 边缘真实单包完成事件会先落盘再进入 `/scheduler/packet-route`。
4. 低置信度或失败包能够创建延期任务。
5. 条件恢复后派发器能够调用 `/edge/cloud-review-tasks`。
6. 同步 `RETRYABLE_FAILED`、`SUCCESS` 和 `PERMANENT_FAILED` 均不会触发 `INVALID_DEFERRED_STATE`。
7. 云端成功但调度器回报暂时失败时不会重复调用云端或提前删除原始包。
8. 边缘节点心跳路径与调度器接口一致。
9. 现有初始边缘分配焦点测试继续通过。
10. 调度器、边缘运行时和云复核焦点测试全部通过。
11. `compileall` 对修改模块成功。
12. 进程内闭环测试确认最终 SQLite 状态和原始包释放行为。

如果完整项目测试因 `torch`、MQTT broker 或其他外部依赖无法运行，交付报告必须分别列出：

- 已通过的单元和集成测试。
- 已完成的进程内闭环。
- 未完成的外部服务 E2E 及其具体阻塞条件。

## 13. 预计文件范围

预计修改：

- `scheduler/api.py`
- `scheduler/node_registry.py`
- `scheduler/deferred_dispatcher.py`
- `scheduler/deferred_cloud_repository.py`，仅在权威状态读取需要仓库接口时修改
- `scheduler/routing_config.py`
- `edge_service/app.py`
- `edge_service/src/edge_runtime/config.py`
- `edge_service/src/edge_runtime/factory.py`
- `edge_service/src/edge_runtime/coordinator.py`
- `edge_service/src/packet_routing_bridge.py`
- `cloud_service/app.py`
- `configs/local.yaml`

预计新增焦点测试目录或文件：

- 调度器 API 生命周期测试。
- 延期派发状态竞争测试。
- 边缘单包路由接线测试。
- 单包云端复核进程内闭环测试。

具体文件名和逐步测试命令将在用户批准本文后，通过单独的实施计划确定。

## 14. 实施顺序

用户批准本文后，实施计划按以下依赖顺序展开：

1. 用失败测试固定调度器 API 和生命周期缺口。
2. 建立调度器运行时容器并恢复包级接口。
3. 用失败测试复现同步回报竞争。
4. 修改派发器，使数据库当前状态成为唯一权威状态。
5. 用失败测试固定边缘单包完成事件未接线的问题。
6. 接入原始包落盘和正式包级请求。
7. 统一边缘心跳、云状态上报和节点标识。
8. 编写并通过进程内完整闭环测试。
9. 运行焦点回归、编译检查和可运行范围内的完整测试。
