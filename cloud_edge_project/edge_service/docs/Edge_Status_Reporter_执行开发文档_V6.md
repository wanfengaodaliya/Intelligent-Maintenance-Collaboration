# Edge Status Reporter 最小侵入执行开发文档（V6）

## 0. 文档定位

本文档基于以下两项重新生成：

- 最新仓库：当前 Git 仓库
- 原始需求：`Edge_Status_Reporter_Codex_开发方案_V3.md`（外部输入文档，不随仓库提交）

基线信息：

- Git 分支：`edge_reporter`
- Git 基线提交：`2e6a394`（`feat: 合入云边路由与延期调度模块`）
- 实施范围：仅 `cloud_edge_project/edge_service/`
- 实际一键启动入口：`cloud_edge_project/start_all.py` → `edge_service.app:app`
- 文档版本：V6
- 文档日期：2026-08-12

本文档是后续代码生成的直接执行依据。实施时不得重新设计架构，不得扩大修改范围；如果本地代码与本文档不一致，先停止并重新核对，不得凭猜测修改其他模块。

## 1. 最终目标

在当前实际运行的 FastAPI Edge 服务中，以旁路、可关闭、故障隔离方式新增 `EdgeStatusReporter`：

1. Edge 通过 `start_all.py` 启动时 Reporter 自动启动；
2. Edge 停止时 Reporter 自动停止；
3. Reporter 默认每 1 秒生成一次状态；
4. 每轮只采集一次并生成一份不可变状态快照；
5. 同一份 JSON 分别发送给 Scheduler 与 Cloud；
6. Scheduler 和 Cloud target 默认都开启；
7. 采集、硬件检测、HTTP 超时和接收端错误不得影响 Edge 请求处理；
8. 不改变现有任务接收、包校验、模型推理、云审查和路由逻辑；
9. 不修改 Scheduler、Cloud、Network、Sender、Common、Core 或启动器；
10. 把接收端合同缺口记录为联调前置条件，而不是通过 Reporter 伪造网络状态规避。

## 2. 不可违反的边界

### 2.1 允许新增或修改

仅允许：

```text
cloud_edge_project/edge_service/app.py
cloud_edge_project/edge_service/requirements.txt
cloud_edge_project/edge_service/src/edge_status_reporter/**
cloud_edge_project/edge_service/status_reporter_tests/**
cloud_edge_project/edge_service/docs/**
```

### 2.2 明确禁止修改

```text
cloud_edge_project/cloud_service/**
cloud_edge_project/scheduler/**
cloud_edge_project/internet_service/**
cloud_edge_project/sender_module/**
cloud_edge_project/common/**
cloud_edge_project/core/**
cloud_edge_project/configs/**
cloud_edge_project/start_all.py
cloud_edge_project/quick_demo.py
```

### 2.3 现有 Edge 逻辑保护

不得改变：

- `/edge/infer` 的请求、响应、校验和异常映射；
- `/edge/tasks` 的任务注册和冲突语义；
- `/edge/packets` 的接收、推理和路由流程；
- `/edge/cloud-review-tasks` 的云审查处理；
- `infer_edge()` 的模型计算和返回合同；
- `EdgeTaskIngress` 的内部状态和幂等逻辑；
- `CloudReviewService`、`PacketRoutingBridge` 和清理线程；
- 现有 HTTP 地址和业务接口；
- `edge_runtime`、`edge_model`、`edge_perception` 等未被实际入口装配的已有逻辑。

现有文件只能增加 Reporter 装配接缝，不能重排或重构原业务代码。

## 3. 最新代码事实映射

### 3.1 实际启动链路

```text
python start_all.py
  → uvicorn edge_service.app:app
  → FastAPI Edge HTTP 服务（默认 127.0.0.1:8001）
```

`edge_service/src/edge_runtime/factory.py` 虽然存在 MQTT、模型队列和旧心跳装配，但当前仓库中没有调用 `build_edge_runtime()` 的生产启动入口。因此本次不得把 Reporter 只接到该未启动链路，否则无法满足“一键启动同步开启”。

### 3.2 当前 Edge 业务入口

| 路由 | 当前实现 | Reporter 行为 |
|---|---|---|
| `GET /health` | Edge 健康检查 | 不计为任务活动 |
| `POST /edge/infer` | 同步调用 `infer_edge()` | 旁路更新时间 |
| `POST /edge/tasks` | `EdgeTaskIngress.register_task()` | 旁路更新时间 |
| `POST /edge/packets` | 接收、推理、路由 | 旁路更新时间 |
| `POST /edge/cloud-review-tasks` | 云审查任务 | 默认不计入状态 Reporter 的本地模型任务活动 |

### 3.3 状态来源

| 字段 | V6 来源 | 说明 |
|---|---|---|
| `edge_node_id` | `edge_service.model.EDGE_NODE_ID` | 不新增第二份节点身份配置 |
| `reported_at_ns` | `time.time_ns()` | 每轮生成一次 |
| `logical_cpu_count` | Resource Collector | System 或 Process 模式 |
| `cpu_utilization_percent` | Resource Collector | 统一归一化为 `0~100` |
| `memory_available_mb` | Resource Collector | 非负 MB |
| `gpu_available` | Accelerator Detector | 启动时检测并缓存，可覆盖 |
| `npu_available` | Accelerator Detector | 启动时检测并缓存，可覆盖 |
| `queue_length` | 当前 FastAPI Edge 状态源 | 固定为 `0`，见第 6.2 节 |
| `model_version` | `EDGE_STATUS_MODEL_VERSION` 或现有 `MODEL_NAME` | 不根据文件名猜测 |
| `load_status` | Edge 应用状态源 | Reporter 启动后为 `LOADED` |
| `last_task_activity_ns` | 旁路活动中间件 | 无活动为 `0` |

### 3.4 现有接收端差异

Scheduler 当前真实路由为：

```text
POST /scheduler/edge-nodes/status
```

但其校验器要求 `network_to_scheduler`。这与 V3“网络状态只能由 Network Module 产生”的职责边界冲突。本次不修改 Scheduler，也不伪造网络字段，因此 Scheduler 在合同修正前可能返回 `400`。

Cloud 当前没有：

```text
POST /cloud/edge-status
```

本次严格不修改 Cloud，因此该目标在 Cloud 接收端完成前可能返回 `404`。两类失败都必须被 Reporter 隔离，不能影响 Edge。

## 4. 选定架构

```text
start_all.py
    │
    └── uvicorn edge_service.app:app
              │
              ├── Existing FastAPI Endpoints（原逻辑不变）
              │        │
              │        └── EdgeActivityMiddleware（旁路观察）
              │
              └── FastAPI lifespan
                       │
                       ├── startup → EdgeStatusReporter.start()
                       └── shutdown → EdgeStatusReporter.stop()

EdgeStatusReporter Thread
    │
    ├── EdgeApplicationState.snapshot()，一次
    ├── ResourceCollector.collect()，一次
    ├── AcceleratorDetector.cached_snapshot()，一次
    ├── EdgeStatusReport，构建一次
    └── 同一 payload
          ├── SchedulerTarget.send()
          └── CloudTarget.send()
```

设计约束：

- Reporter 使用独立 daemon 线程，不使用 FastAPI 请求线程；
- 周期等待使用 `threading.Event.wait()`，便于立即停止；
- 中间件使用纯 ASGI 包装，不使用 `BaseHTTPMiddleware`；
- 中间件不得读取或改写请求体、响应体和响应头；
- Reporter 不创建 HTTP 服务，不监听新端口；
- Reporter 不与旧 `edge_runtime.HeartbeatLoop` 同时装配，避免双重上报。

## 5. 文件级实施清单

### 5.1 新增生产文件

```text
edge_service/src/edge_status_reporter/__init__.py
edge_service/src/edge_status_reporter/config.py
edge_service/src/edge_status_reporter/contracts.py
edge_service/src/edge_status_reporter/state.py
edge_service/src/edge_status_reporter/middleware.py
edge_service/src/edge_status_reporter/collectors.py
edge_service/src/edge_status_reporter/transport.py
edge_service/src/edge_status_reporter/reporter.py
edge_service/src/edge_status_reporter/bootstrap.py
```

### 5.2 小幅修改现有文件

```text
edge_service/app.py
edge_service/requirements.txt
```

`app.py` 只允许：

1. 导入 Reporter bootstrap；
2. 构建一个 integration；
3. 把 integration 的 lifespan 传给 FastAPI；
4. 安装活动中间件。

禁止修改已有路由函数体。

`requirements.txt` 只增加：

```text
psutil>=5.9,<8
```

### 5.3 新增测试

```text
edge_service/status_reporter_tests/__init__.py
edge_service/status_reporter_tests/conftest.py
edge_service/status_reporter_tests/test_config.py
edge_service/status_reporter_tests/test_contracts.py
edge_service/status_reporter_tests/test_state.py
edge_service/status_reporter_tests/test_middleware.py
edge_service/status_reporter_tests/test_collectors.py
edge_service/status_reporter_tests/test_transport.py
edge_service/status_reporter_tests/test_reporter.py
edge_service/status_reporter_tests/test_bootstrap.py
```

## 6. 状态合同

### 6.1 统一 JSON

Scheduler 和 Cloud 必须收到同一结构：

```json
{
  "edge_node_id": "edge_01",
  "reported_at_ns": 1786200000000000000,
  "resources": {
    "logical_cpu_count": 4,
    "cpu_utilization_percent": 56.2,
    "memory_available_mb": 4210.0,
    "gpu_available": false,
    "npu_available": false,
    "queue_length": 0
  },
  "models": [
    {
      "model_version": "edge_bearing_mock",
      "load_status": "LOADED"
    }
  ],
  "last_task_activity_ns": 1786199999500000000
}
```

禁止增加：

```text
network_to_scheduler
network_to_cloud
rtt_ms_avg
rtt_ms_p95
loss_rate
available_uplink_mbps_estimate
Link_Reliability_Score
```

### 6.2 `queue_length` 语义

当前实际 FastAPI Edge 使用同步函数直接处理 `/edge/infer` 和 `/edge/packets`，没有项目内部可读取的等待模型队列。ASGI Server 的内部调度队列也不是 Edge 业务队列。

因此 V6 必须：

```text
queue_length = 0
```

不得：

- 把正在执行的请求数量当成等待队列；
- 新建人工加减计数器冒充模型队列；
- 读取未启动 `edge_runtime` 中的 `pipeline.queue_length`；
- 修改现有请求处理为排队模型。

如果未来生产入口切换到 `build_edge_runtime()`，必须另开任务重新设计状态源，不能在本次代码中提前耦合。

### 6.3 `model_version` 与 `load_status`

模型版本优先级：

```text
EDGE_STATUS_MODEL_VERSION 非空
  → 使用环境变量

否则
  → 使用现有 edge_service.model.MODEL_NAME
```

不得通过路径或 checkpoint 文件名推断版本。

FastAPI 应用和 `edge_service.model` 已成功导入、Reporter 成功启动后，状态源上报：

```text
load_status = LOADED
```

Reporter 停止后不再产生报告，不需要额外发送 `UNLOADED`。

### 6.4 `last_task_activity_ns`

初始值为 `0`。纯 ASGI 中间件只观察以下路径：

```text
/edge/infer
/edge/tasks
/edge/packets
```

每个匹配请求：

1. 进入业务应用前调用一次 `touch()`；
2. 应用正常返回或抛出异常后，在 `finally` 中调用一次 `touch()`。

Reporter 自己读取状态时不得刷新该字段。`/health` 请求不得刷新该字段。

### 6.5 数据类

`contracts.py` 实现冻结数据类：

```python
ModelStatus
BusinessStatusSnapshot
ResourceSnapshot
AcceleratorSnapshot
EdgeStatusReport
```

`EdgeStatusReport.as_dict()` 是唯一序列化入口。校验要求：

- 字符串字段非空；
- `reported_at_ns` 为 `1..9223372036854775807`；
- `last_task_activity_ns` 为非负整数；
- CPU 数大于 0；
- CPU 百分比在 `0..100`；
- 内存和队列非负；
- GPU/NPU 必须是布尔值；
- `load_status` 仅允许 `LOADING/LOADED/UNLOADED/ERROR`。

## 7. 配置合同

所有 Reporter 配置仅使用 `EDGE_STATUS_*` 环境变量，不修改公共 YAML。

### 7.1 默认值

| 环境变量 | 默认值 | 含义 |
|---|---:|---|
| `EDGE_STATUS_REPORTER_ENABLED` | `true` | Reporter 总开关 |
| `EDGE_STATUS_INTERVAL_SECONDS` | `1.0` | 上报周期 |
| `EDGE_STATUS_MODEL_VERSION` | `MODEL_NAME` | 显式模型版本覆盖 |
| `EDGE_STATUS_SCHEDULER_ENABLED` | `true` | Scheduler target 开关 |
| `EDGE_STATUS_SCHEDULER_URL` | `http://127.0.0.1:8003/scheduler/edge-nodes/status` | Scheduler 完整 URL |
| `EDGE_STATUS_SCHEDULER_TIMEOUT_SECONDS` | `0.5` | 单次请求超时 |
| `EDGE_STATUS_SCHEDULER_RETRY_COUNT` | `1` | 首次失败后的重试次数 |
| `EDGE_STATUS_CLOUD_ENABLED` | `true` | Cloud target 开关 |
| `EDGE_STATUS_CLOUD_URL` | `http://127.0.0.1:8004/cloud/edge-status` | Cloud 完整 URL |
| `EDGE_STATUS_CLOUD_TIMEOUT_SECONDS` | `0.5` | 单次请求超时 |
| `EDGE_STATUS_CLOUD_RETRY_COUNT` | `1` | 首次失败后的重试次数 |
| `EDGE_STATUS_RESOURCE_MODE` | `system` | `system` 或 `process` |
| `EDGE_STATUS_PROCESS_LOGICAL_CPU_COUNT` | 无 | Process 模式必填 |
| `EDGE_STATUS_PROCESS_MEMORY_LIMIT_MB` | 无 | Process 模式必填 |
| `EDGE_STATUS_GPU_AVAILABLE_OVERRIDE` | 无 | 可选 true/false |
| `EDGE_STATUS_NPU_AVAILABLE_OVERRIDE` | 无 | 可选 true/false |

### 7.2 校验

- Reporter 关闭时，不导入 `psutil`，不创建线程，不安装活动中间件；
- Reporter 开启时至少启用一个 target；
- URL 必须是包含 hostname 的 HTTP(S) 完整 URL；
- timeout 必须大于 0；
- retry count 必须为非负整数；
- interval 必须大于 0；
- Process 模式必须提供正整数 CPU 配额和正数内存配额；
- 布尔变量只接受 `true/false/1/0/yes/no/on/off`，其他值视为配置错误；
- 配置错误只禁用 Reporter 并记录错误，不阻止 Edge 启动。

## 8. 资源与加速器采集

### 8.1 System 模式

使用 `psutil`：

```python
psutil.cpu_count(logical=True)
psutil.cpu_percent(interval=None)
psutil.virtual_memory().available
```

启动时预热一次 CPU 采样。内存转换为 MB。CPU 结果 clamp 到 `0..100`。

### 8.2 Process 模式

```text
logical_cpu_count = EDGE_STATUS_PROCESS_LOGICAL_CPU_COUNT
cpu_utilization_percent = process.cpu_percent() / logical_cpu_count
memory_available_mb = max(memory_limit_mb - process RSS MB, 0)
```

CPU 最终 clamp 到 `0..100`。Process 模式是配额模拟，不代表物理隔离。

### 8.3 Accelerator Detector

优先级：

```text
override
  → 使用 override
否则
  → 尝试轻量自动检测
检测失败
  → warning + false
```

GPU/NPU 只在 Reporter 启动时检测一次并缓存，禁止每秒加载大型 SDK、执行外部进程或重复探测。

## 9. HTTP 发送与故障隔离

`transport.py` 提供 `HttpStatusTarget`：

- 使用项目已有 `requests`；
- 支持注入 `http_post` 以便测试；
- 每个 target 独立 URL、timeout 和 retry；
- 仅网络异常、timeout 和 `5xx` 重试；
- `4xx` 不重试；
- 返回 `True/False`，不向 Reporter 线程传播发送异常；
- 不保存失败 payload；
- 不指数退避，不让旧状态占用下一周期。

Reporter 每轮：

```text
collect once
build once
serialize once
send Scheduler
send Cloud
discard current snapshot
wait until next interval
```

Scheduler 失败后仍必须尝试 Cloud；Cloud 失败不影响下一周期 Scheduler。

## 10. 生命周期与线程安全

### 10.1 FastAPI 接入

`bootstrap.py` 暴露：

```python
build_edge_status_integration(
    *,
    edge_node_id: str,
    default_model_version: str,
) -> EdgeStatusIntegration
```

`EdgeStatusIntegration` 暴露：

```python
lifespan(app)
install(app)
```

`app.py` 只增加最少装配代码，不修改路由函数体。

### 10.2 启停要求

- `start()` 幂等；
- `stop()` 幂等；
- 线程名固定为 `edge-status-reporter`；
- 线程为 daemon；
- `stop()` 使用 Event 唤醒等待；
- join 超时不超过 `interval + 1 秒`；
- 启停失败记录日志，不影响 FastAPI lifespan。

### 10.3 状态锁

`EdgeApplicationState` 只保护：

```text
last_task_activity_ns
load_status
```

锁不得覆盖 HTTP 请求处理、模型推理或网络发送。`snapshot()` 在锁内只复制小型标量。

## 11. 日志要求

使用 Python 标准 `logging`，不修改公共 logger。

INFO：

- Reporter 启动和停止；
- 周期和资源模式；
- Scheduler/Cloud 是否启用及目标地址。

WARNING/ERROR：

- 配置无效导致 Reporter 被禁用；
- 资源采集或加速器检测失败；
- target、尝试次数、HTTP 状态或异常类型；
- Reporter 循环异常。

正常每秒成功不得把完整 JSON 写入 INFO；最多写 DEBUG。

## 12. 默认端口与联调地址矩阵

### 12.1 当前项目默认端口

| 组件 | 默认地址/端口 | 当前来源 | V6 用途 |
|---|---|---|---|
| Edge FastAPI | `127.0.0.1:8001` | `configs/local.yaml` | Reporter 随此服务启动 |
| Scheduler HTTP | `127.0.0.1:8003` | `configs/local.yaml` | Reporter Scheduler target |
| Cloud HTTP | `127.0.0.1:8004` | `configs/local.yaml` | Reporter Cloud target |
| Log Service | `127.0.0.1:8006` | `configs/local.yaml` | Reporter 不调用 |
| MQTT Broker | `127.0.0.1:1883` | Edge Runtime/Sender/Network 配置 | 方案 A 不直接使用 |
| Edge Runtime Control | `0.0.0.0:8011` | `edge_runtime/config.py` | 当前未启动，不接入 Reporter |
| Network Simulator API | `0.0.0.0:8090` | `plugins.yaml` | Reporter 不调用 |
| Toxiproxy API | `toxiproxy:8474` | `links.yaml` | 仅网络模拟控制面 |
| sender_01 MQTT proxy base | `18831` | `entities.yaml` | 动态代理端口，不是 Reporter 端口 |
| sender_02 MQTT proxy base | `18931` | `entities.yaml` | 动态代理端口，不是 Reporter 端口 |
| Edge Model HTTP 示例 | `127.0.0.1:8001` | `edge_model/config.py`、`model_service/app.py` | 与 Edge FastAPI 默认端口冲突，方案 A 不启动它 |

### 12.2 Reporter 默认完整地址

```text
Scheduler: http://127.0.0.1:8003/scheduler/edge-nodes/status
Cloud:     http://127.0.0.1:8004/cloud/edge-status
```

Reporter 自身不监听端口。

### 12.3 联调警告

1. `internet_service/network_simulator/config/reporter.yaml` 中的 `http://scheduler:8000/...` 是 Network Module Reporter 的旧容器占位地址，不是本项目本地 Scheduler 默认端口；本地联调不得据此把 Edge Reporter 指向 `8000`。
2. `edge_runtime/config.py` 默认状态路径是 `/scheduler/edge-status`，但 Scheduler 当前真实路由是 `/scheduler/edge-nodes/status`；V6 使用后者。
3. Edge Model HTTP 示例和 Edge FastAPI 都默认 `8001`，两者不能在同一地址同时监听；方案 A 只需要 Edge FastAPI。
4. Network Module 的 Edge→Scheduler、Edge→Cloud HTTP 代理仍是注释模板。启用代理后，应只覆盖 `EDGE_STATUS_SCHEDULER_URL` 和 `EDGE_STATUS_CLOUD_URL`，不得修改 Reporter 代码。

## 13. 测试计划

### 13.1 单元测试

1. 合法状态生成完整 JSON；
2. 非法 CPU、内存、时间戳、加载状态被拒绝；
3. 状态源初始队列为 0、活动时间为 0；
4. 中间件只更新三个指定业务路径；
5. `/health` 不更新时间；
6. 中间件不修改响应和异常；
7. System Collector 字段和 MB 转换正确；
8. Process Collector CPU 归一化和内存配额正确；
9. override 优先于自动检测；
10. Scheduler 和 Cloud 收到同一 `reported_at_ns` 和同一 payload；
11. Scheduler 失败时 Cloud 仍发送；
12. Cloud 失败时 Scheduler 仍发送；
13. 两端失败时 Reporter 下一轮继续；
14. `4xx` 不重试，网络错误和 `5xx` 有限重试；
15. Reporter disabled 不导入 `psutil`、不创建线程、不装中间件；
16. start/stop 幂等；
17. FastAPI lifespan 自动启停 Reporter。

### 13.2 回归测试

按以下顺序执行：

```powershell
python -m pytest edge_service/status_reporter_tests -q
python -m pytest edge_service/verification -q
python -m pytest cloud_service/tests cloud_service/verification -q
python -m compileall edge_service
```

如果项目根目录测试收集可执行，再运行：

```powershell
python -m pytest -q
```

不得为了通过测试修改其他模块或删除既有测试。无关失败必须单独记录。

## 14. 实施步骤

### 阶段 1：合同与配置

1. 新增冻结数据类和严格校验；
2. 新增环境变量解析；
3. 新增配置错误测试。

### 阶段 2：状态源与旁路中间件

1. 新增 `EdgeApplicationState`；
2. 固定真实 `queue_length=0`；
3. 新增纯 ASGI 活动中间件；
4. 验证请求和响应完全不变。

### 阶段 3：资源与加速器

1. 实现 System Collector；
2. 实现 Process Collector；
3. 实现启动时加速器检测和 override；
4. 加入 `psutil` 依赖。

### 阶段 4：双目标发送

1. 实现 `HttpStatusTarget`；
2. 实现 retry 判定；
3. 验证双目标故障隔离。

### 阶段 5：Reporter 与生命周期

1. 实现 `report_once()`；
2. 实现后台循环、幂等 start/stop；
3. 实现 `EdgeStatusIntegration`；
4. 小幅修改 `app.py` 完成自动启停。

### 阶段 6：验证与交付

1. 运行新增测试；
2. 运行 Edge 回归测试；
3. 运行可执行的项目测试；
4. 检查 Git diff，确保无 Edge 外修改；
5. 输出新增文件、修改文件、配置、端口、测试结果和外部阻塞项。

## 15. 验收标准

- [ ] 只有 `edge_service` 范围发生变化。
- [ ] `start_all.py` 无修改且启动 Edge 时 Reporter 自动启动。
- [ ] Reporter 默认开启。
- [ ] Scheduler target 默认开启并使用端口 `8003`。
- [ ] Cloud target 默认开启并使用端口 `8004`。
- [ ] Reporter 不监听新端口。
- [ ] 现有 Edge 路由函数体没有修改。
- [ ] 原业务请求、响应和异常行为不变。
- [ ] 每轮只生成一份状态快照。
- [ ] Scheduler 与 Cloud 获得相同 payload。
- [ ] `queue_length=0` 的语义在代码和文档中明确。
- [ ] Reporter 不生成网络指标。
- [ ] 任一目标失败不影响另一目标。
- [ ] 两个目标都失败不影响 Edge。
- [ ] 不产生历史状态 backlog。
- [ ] 支持 System 与 Process 模式。
- [ ] GPU/NPU 检测失败不影响 Edge 启动。
- [ ] Reporter disabled 恢复原 Edge 行为。
- [ ] 默认端口矩阵完整且注明已发现的冲突。
- [ ] 新增测试与既有 Edge 测试通过，或明确记录无关失败。

## 16. 外部模块联调前置条件

以下事项不属于本次允许修改范围：

1. Scheduler 接收合同需要允许 Edge Reporter 不携带 `network_to_scheduler`，并从 Network Module 的 Link State Table 独立读取网络状态；
2. Cloud 需要实现 `POST /cloud/edge-status`；
3. 如果启用 Network Module HTTP 代理，需要为 Edge→Scheduler 和 Edge→Cloud 分配实际代理端口；
4. 联调方需要通过 `EDGE_STATUS_SCHEDULER_URL`、`EDGE_STATUS_CLOUD_URL` 指向代理地址；
5. 如果未来生产入口切换到 `edge_runtime`，需重新决定是否复用其 `pipeline.queue_length` 和旧心跳，避免双 Reporter。

这些缺口不得通过修改本次 Reporter 的职责边界解决。

## 17. 后续代码生成指令

后续编码代理必须按以下顺序执行：

```text
1. 重新确认 Git 工作区干净或只含用户已知改动
2. 严格按第 5 节创建文件
3. 先写测试，再写对应最小生产代码
4. 最后才修改 app.py 和 requirements.txt
5. 不修改任何 Edge 外文件
6. 执行第 13 节测试
7. 用 git diff --name-only 审核修改边界
8. 输出端口、环境变量、测试结果和外部联调条件
```

若实现中发现必须修改 Scheduler、Cloud、Network、Common 或启动器才能继续，应停止编码并向用户说明，不得越界修改。

## 18. 最终决策摘要

V6 采用“FastAPI lifespan 自动启停 + 纯 ASGI 旁路活动观察 + 独立 Reporter 后台线程 + 双目标同快照发送”的方案。生产功能集中在新包中，现有代码只增加装配接缝和 `psutil` 依赖；现有路由、推理、任务和云审查逻辑保持不变。Reporter 默认向 `127.0.0.1:8003` 的 Scheduler 和 `127.0.0.1:8004` 的 Cloud 上报，但接收端当前合同缺口只作为联调前置条件记录，不越界修改。Reporter 不产生网络状态、不监听端口、不保存失败历史，并保证任何辅助功能故障都不影响 Edge 主业务。
