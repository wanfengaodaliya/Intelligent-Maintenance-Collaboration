# 弱网缓速发送实施计划

> **供执行该计划的 AI 使用：** 按任务顺序实施。每个任务先写临时失败测试、确认 RED，再写最小实现、确认 GREEN。用户不希望提交新增测试文件，因此测试通过后删除本计划创建的 `*_temp.py`；不要删除或改写仓库原有测试。

**目标：** 保持 Network Simulator 原有 GOOD/MEDIUM/BAD 区间和状态矩阵不变；当 Sender→Edge 链路带宽在 4Mbps 以上但不足实时发送需求时，Scheduler 仍分配 Edge，并让 Sender 按链路容量降低发送速度；低于4Mbps时等待网络恢复，最长沿用现有60秒调度重试窗口。

**架构：** Scheduler 仍负责选择 Edge，同时在分配响应中返回 `delivery_mode`、`delivery_interval_ms` 和本次链路带宽。Sender 只采用 Scheduler 给出的间隔控制 MQTT 发布节奏，传感器数据窗口及生成时间戳仍保持50ms。Edge 接收的数据结构、任务登记方式和诊断流程不因缓传而改变。

**技术栈：** Python 3.11、FastAPI/现有 Scheduler API、requests、MQTT QoS 1、pytest。

## 全局约束

- 不修改 `cloud_edge_project/internet_service/network_simulator/config/network_states.yaml`。
- 不修改 `cloud_edge_project/internet_service/network_simulator/config/transition_matrix.yaml`。
- 不新增网络状态；GOOD/MEDIUM/BAD/DISCONNECTED 枚举保持原样。
- 保留当前未提交的 float32 二进制协议修改：
  - `cloud_edge_project/sender_module/sender/packet.py`
  - `cloud_edge_project/edge_service/src/edge_runtime/mqtt.py`
- 不修改诊断模型、窗口组装器或 Edge 任务接入规则。
- 不更改传感器时间轴：每包仍代表50ms采样窗口，`end_generate_timestamp_ns` 仍按50ms递增。
- 只有网络发送的墙钟间隔可以变慢。
- 低于4Mbps时不发送任何数据包，继续使用同一个 `task_id` 请求调度；不得重新生成设备ID或任务ID。
- 不自动提交或推送。完成并验证后先向用户汇报，由用户决定是否提交。
- 不保留新建的自动化测试文件；允许保留因正式接口变化而必须同步修改的仓库原有测试。

---

## 一、最终行为定义

### 1. Scheduler 的三种决策

| 链路条件 | Scheduler 行为 | Sender 行为 |
|---|---|---|
| 没有链路快照 | 保持旧兼容行为，按实时模式分配 | 50ms/包 |
| 可用带宽足以覆盖任务需求 | 优先选择该类 Edge，返回 `realtime` | 50ms/包 |
| 可用带宽 ≥ 4Mbps，但不足实时需求 | 允许成为候选，返回 `buffered` | 按 Scheduler 返回的间隔缓速发送 |
| 可用带宽 < 4Mbps | 以 `INSUFFICIENT_BANDWIDTH` 拒绝该候选 | 不发送，沿用现有调度重试 |

若一个 Edge 可实时发送、另一个只能缓传，必须优先实时 Edge；只有所有可用候选都无法实时发送时，才选择分数最高的缓传 Edge。

### 2. 缓传间隔公式

Scheduler 使用链路带宽和模拟丢包率计算可用吞吐：

```python
base_interval_ms = math.ceil(
    request["expected_duration_ms"] / request["expected_packet_count"]
)
effective_mbps = (
    link.available_throughput_mbps
    * (1.0 - link.simulated_packet_loss_rate)
)
wire_interval_ms = math.ceil(
    request["packet_size_bytes"] * 8.0
    / max(effective_mbps, 0.001)
    / 1000.0
)
delivery_interval_ms = max(base_interval_ms, wire_interval_ms)
delivery_mode = (
    "realtime"
    if delivery_interval_ms <= base_interval_ms
    else "buffered"
)
```

单位说明：`packet_size_bytes × 8 / Mbps / 1000` 的结果是毫秒。

当前二进制包约41.9KB：

- 带宽6.7Mbps左右：约50ms/包。
- 带宽5Mbps：约68ms/包。
- 带宽4Mbps且模拟丢包率约1%：约85ms/包。
- 80包总发送时间约4～6.8秒。

Scheduler 当前最短保留任务预约30秒，已覆盖上述最长约6.8秒，因此本次不修改预约TTL。

### 3. 低于4Mbps的等待语义

不要新增独立等待队列。复用当前 Sender 调度重试机制：

1. Scheduler 没有合格候选时返回 HTTP 503、`NO_AVAILABLE_EDGE_NODE`，候选拒绝原因保留 `INSUFFICIENT_BANDWIDTH`。
2. Sender 的 `SchedulerClient.assign()` 对503继续使用当前2/4/8/16秒退避，并受60秒总窗口限制。
3. 每次重试使用完全相同的 `task_id` 和请求内容。
4. 网络恢复到4Mbps以上后，Scheduler 为同一任务分配 Edge。
5. 60秒内仍未恢复才返回 `scheduling_failed`；不得静默丢弃。

当前网络模拟器每1秒更新一次状态。从 BAD 回到 MEDIUM/GOOD 的理论平均时间约4秒，约77%在5秒内恢复、94%在10秒内恢复，因此无需新增更复杂的持久化等待器。

### 4. Scheduler 响应字段

成功响应在现有字段基础上新增：

```json
{
  "device_id": "machine_01",
  "sender_id": "sender_01",
  "task_id": "sd_01_tk_0001",
  "bearing_id": "bearing_01",
  "target_topic": "edge/edge_01/input",
  "delivery_mode": "buffered",
  "delivery_interval_ms": 85,
  "available_throughput_mbps": 4.0
}
```

字段约束：

- `delivery_mode` 只能是 `realtime` 或 `buffered`。
- `delivery_interval_ms` 必须是正整数，不能是布尔值。
- `available_throughput_mbps` 为非负数；无快照时为 `null`。
- 为兼容旧 Scheduler，Sender 收到缺少新增字段的响应时回退为 `realtime + 50ms`。

---

## 二、文件修改地图

### 必须修改

- `cloud_edge_project/scheduler/assignment_scheduler.py`
  - 计算缓传计划。
  - 允许4Mbps以上的低带宽候选。
  - 实时候选优先于缓传候选。
  - 在成功响应中返回缓传字段。
- `cloud_edge_project/sender_module/sender/scheduler_client.py`
  - 解析、校验新增响应字段。
  - 缺少新增字段时兼容旧 Scheduler。
- `cloud_edge_project/sender_module/sender/controller.py`
  - 使用 `delivery_interval_ms` 控制墙钟发送节奏。
  - 在任务摘要中记录缓传状态。

### 明确禁止修改

- `cloud_edge_project/internet_service/network_simulator/config/network_states.yaml`
- `cloud_edge_project/internet_service/network_simulator/config/transition_matrix.yaml`
- `cloud_edge_project/edge_service/src/edge_task_ingress/manager.py`
- Edge 诊断模型和窗口组装代码。

### 临时测试文件（验证后删除）

- `cloud_edge_project/scheduler/tests/test_buffered_delivery_temp.py`
- `cloud_edge_project/sender_module/tests/test_buffered_delivery_temp.py`

---

## 三、任务1：Scheduler 计算缓传计划

**修改文件：** `cloud_edge_project/scheduler/assignment_scheduler.py`

**产出接口：**

```python
MIN_BUFFERED_THROUGHPUT_MBPS = 4.0
REALTIME_DELIVERY_MODE = "realtime"
BUFFERED_DELIVERY_MODE = "buffered"


def _delivery_plan(
    request: Mapping[str, Any],
    link: LinkSnapshot | None,
) -> tuple[str, int, float | None]:
    """返回 delivery_mode、delivery_interval_ms、available_mbps。"""
```

- [ ] **步骤1：创建 Scheduler 临时失败测试**

在 `cloud_edge_project/scheduler/tests/test_buffered_delivery_temp.py` 中直接测试 `_delivery_plan()`：

```python
from types import SimpleNamespace

from scheduler.assignment_scheduler import _delivery_plan


REQUEST = {
    "packet_size_bytes": 41_900,
    "expected_packet_count": 80,
    "expected_duration_ms": 4_000,
}


def _link(mbps: float, loss: float = 0.0):
    return SimpleNamespace(
        available_throughput_mbps=mbps,
        simulated_packet_loss_rate=loss,
    )


def test_delivery_plan_keeps_realtime_when_bandwidth_is_enough():
    mode, interval, available = _delivery_plan(REQUEST, _link(10.0))
    assert (mode, interval, available) == ("realtime", 50, 10.0)


def test_delivery_plan_slows_down_at_four_mbps():
    mode, interval, available = _delivery_plan(REQUEST, _link(4.0, 0.01))
    assert mode == "buffered"
    assert interval == 85
    assert available == 4.0


def test_delivery_plan_keeps_old_behavior_without_snapshot():
    assert _delivery_plan(REQUEST, None) == ("realtime", 50, None)
```

- [ ] **步骤2：运行测试并确认 RED**

```powershell
cd D:\desktop\Intelligent-Maintenance-Collaboration
D:\develop\Miniconda3\envs\moment\python.exe -m pytest cloud_edge_project\scheduler\tests\test_buffered_delivery_temp.py -q
```

预期：因 `_delivery_plan` 尚不存在而失败。

- [ ] **步骤3：实现 `_delivery_plan()`**

使用“一、最终行为定义”中的公式。必须使用 `math.ceil()`；不允许使用固定85ms，也不允许根据 GOOD/MEDIUM/BAD 字符串判断。

- [ ] **步骤4：扩展数据类**

将 `AssignmentDecision` 扩展为：

```python
@dataclass(frozen=True)
class AssignmentDecision:
    device_id: str
    sender_id: str
    task_id: str
    bearing_id: str
    target_topic: str
    delivery_mode: str
    delivery_interval_ms: int
    available_throughput_mbps: float | None
```

`to_dict()` 必须包含三个新增字段。

将 `RankedNode` 增加：

```python
delivery_mode: str
```

- [ ] **步骤5：修改候选门控与排序**

在 `_rank_candidates()` 中：

```python
if (
    link is not None
    and link.available_throughput_mbps < MIN_BUFFERED_THROUGHPUT_MBPS
):
    # 保留原 INSUFFICIENT_BANDWIDTH 拒绝记录
    continue

delivery_mode, _, _ = _delivery_plan(request, link)
```

删除“低于实时所需带宽就直接拒绝”的旧条件。排序键必须先比较模式：

```python
key=lambda item: (
    item.delivery_mode != REALTIME_DELIVERY_MODE,
    -item.total_score,
    item.state.config.edge_node_id,
)
```

这样 `realtime` 永远优先于 `buffered`。

- [ ] **步骤6：所有成功返回路径都携带缓传字段**

至少覆盖：

- 新任务首次分配成功。
- 同一个已分配 `task_id` 幂等重试。

已分配任务重试时，从 `repository` 中的 `edge_node_id` 重新读取当前链路快照并调用 `_delivery_plan()`，不要修改数据库表结构。

- [ ] **步骤7：确认 GREEN**

重新运行步骤2命令，预期3项通过。

---

## 四、任务2：Sender 解析缓传协议

**修改文件：** `cloud_edge_project/sender_module/sender/scheduler_client.py`

**产出接口：**

```python
@dataclass(frozen=True)
class ScheduleAssignment:
    device_id: str
    sender_id: str
    task_id: str
    bearing_id: str
    target_topic: str
    schedule_retry_count: int
    delivery_mode: str
    delivery_interval_ms: int
    available_throughput_mbps: float | None
```

- [ ] **步骤1：创建 Sender 临时失败测试**

在 `cloud_edge_project/sender_module/tests/test_buffered_delivery_temp.py` 中加入：

```python
from sender.scheduler_client import validate_assignment


BASE = {
    "device_id": "machine_01",
    "sender_id": "sender_01",
    "task_id": "sd_01_tk_0001",
    "bearing_id": "bearing_01",
    "target_topic": "edge/edge_01/input",
}


def _validate(payload):
    return validate_assignment(
        payload,
        expected_device_id="machine_01",
        expected_sender_id="sender_01",
        expected_task_id="sd_01_tk_0001",
        expected_bearing_id="bearing_01",
    )


def test_assignment_accepts_buffered_delivery_fields():
    result = _validate({
        **BASE,
        "delivery_mode": "buffered",
        "delivery_interval_ms": 85,
        "available_throughput_mbps": 4.0,
    })
    assert result.delivery_mode == "buffered"
    assert result.delivery_interval_ms == 85
    assert result.available_throughput_mbps == 4.0


def test_assignment_falls_back_for_old_scheduler():
    result = _validate(BASE)
    assert result.delivery_mode == "realtime"
    assert result.delivery_interval_ms == 50
    assert result.available_throughput_mbps is None
```

再加入非法值用例，分别拒绝：未知模式、0或负数间隔、布尔间隔、负带宽。

- [ ] **步骤2：运行并确认 RED**

```powershell
cd D:\desktop\Intelligent-Maintenance-Collaboration\cloud_edge_project\sender_module
D:\develop\Miniconda3\envs\moment\python.exe -m pytest tests\test_buffered_delivery_temp.py -q
```

- [ ] **步骤3：最小实现字段解析**

旧响应默认值必须是：

```python
delivery_mode = payload.get("delivery_mode", "realtime")
delivery_interval_ms = payload.get("delivery_interval_ms", 50)
available_throughput_mbps = payload.get("available_throughput_mbps")
```

禁止因为旧 Scheduler 缺少字段而报错。

- [ ] **步骤4：确认 GREEN**

重新运行步骤2命令。

---

## 五、任务3：Sender 按调度间隔缓速发送

**修改文件：** `cloud_edge_project/sender_module/sender/controller.py`

- [ ] **步骤1：在 Sender 临时测试中增加节奏测试**

测试必须证明：

- 调度返回 `delivery_interval_ms=85` 时，第1、2、3包目标时刻分别为0、85、170ms。
- `end_generate_timestamp_ns` 仍按0、50、100ms递增，不能跟随发送间隔变成85ms。
- `realtime=False` 时仍不等待，保持加速回放行为。

为避免真实等待，monkeypatch `sender.controller.time.monotonic` 和 `time.sleep`，记录 sleep 参数；不要连接真实 MQTT。

- [ ] **步骤2：运行并确认 RED**

```powershell
cd D:\desktop\Intelligent-Maintenance-Collaboration\cloud_edge_project\sender_module
D:\develop\Miniconda3\envs\moment\python.exe -m pytest tests\test_buffered_delivery_temp.py -q
```

- [ ] **步骤3：替换墙钟发送间隔**

在 `run_sender_task()` 中只替换：

```python
interval_seconds = assignment.delivery_interval_ms / 1000.0
```

以下生成时间戳公式必须保留 `config.packet_interval_ms`：

```python
started_ns
+ (sequence_number - 1) * config.packet_interval_ms * 1_000_000
```

- [ ] **步骤4：任务摘要增加可观测字段**

所有已获得 Scheduler 分配的成功、部分成功和 MQTT 失败摘要增加：

```python
"delivery_mode": assignment.delivery_mode,
"delivery_interval_ms": assignment.delivery_interval_ms,
"available_throughput_mbps": assignment.available_throughput_mbps,
"estimated_delivery_duration_ms": (
    assignment.delivery_interval_ms * config.expected_packet_count
),
```

调度阶段直接失败时这四项使用 `None`。保留现有 `replay_mode`，不要用 `delivery_mode` 替换它：

- `replay_mode` 表示用户选择实时回放还是加速回放。
- `delivery_mode` 表示网络层实时发送还是缓传。

- [ ] **步骤5：确认 GREEN**

重新运行步骤2命令。

---

## 六、任务4：回归验证与清理

- [ ] **步骤1：验证 Network Simulator 没有变化**

```powershell
git diff -- cloud_edge_project/internet_service/network_simulator/config/network_states.yaml
git diff -- cloud_edge_project/internet_service/network_simulator/config/transition_matrix.yaml
```

预期：两条命令均无输出。

- [ ] **步骤2：运行 Sender 全量测试**

```powershell
cd D:\desktop\Intelligent-Maintenance-Collaboration\cloud_edge_project\sender_module
D:\develop\Miniconda3\envs\moment\python.exe -m pytest tests -q
```

预期：除临时新增用例外，仓库原24项全部通过。

- [ ] **步骤3：运行 Scheduler 测试**

```powershell
cd D:\desktop\Intelligent-Maintenance-Collaboration
D:\develop\Miniconda3\envs\moment\python.exe -m pytest cloud_edge_project\scheduler\tests -q
```

预期：全部通过。

- [ ] **步骤4：运行静态检查**

```powershell
cd D:\desktop\Intelligent-Maintenance-Collaboration
D:\develop\Miniconda3\envs\moment\python.exe -m py_compile `
  cloud_edge_project\scheduler\assignment_scheduler.py `
  cloud_edge_project\sender_module\sender\scheduler_client.py `
  cloud_edge_project\sender_module\sender\controller.py
git diff --check
```

- [ ] **步骤5：做不接真实服务的节奏实验**

至少验证三种输入：

| 带宽 | 预期结果 |
|---:|---|
| 10Mbps | realtime，50ms/包 |
| 5Mbps | buffered，约68ms/包 |
| 4Mbps、1%丢包 | buffered，约85ms/包 |
| 3.9Mbps | Scheduler 拒绝候选，Sender 保持同一任务重试 |

- [ ] **步骤6：删除临时测试文件**

仅删除本计划创建的：

```text
cloud_edge_project/scheduler/tests/test_buffered_delivery_temp.py
cloud_edge_project/sender_module/tests/test_buffered_delivery_temp.py
```

不得删除仓库原测试。

- [ ] **步骤7：检查最终改动范围**

正常情况下最终功能差异应集中在：

```text
cloud_edge_project/scheduler/assignment_scheduler.py
cloud_edge_project/sender_module/sender/scheduler_client.py
cloud_edge_project/sender_module/sender/controller.py
cloud_edge_project/sender_module/sender/packet.py
cloud_edge_project/edge_service/src/edge_runtime/mqtt.py
```

其中最后两个文件是已经完成的二进制协议改造，不应在缓传任务中大幅重写。

---

## 七、最终验收标准

- Network Simulator 配置和矩阵与修改前完全相同。
- float32 二进制包仍保持约4.7倍缩小效果。
- 可实时链路永远优先于缓传链路。
- 4～实时需求带宽之间不再被 Scheduler 直接拒绝。
- 4Mbps以下不发送数据，继续使用同一个任务等待恢复，最长60秒。
- 缓传只改变 MQTT 发布墙钟间隔，不改变50ms传感器窗口和时间戳。
- 旧 Scheduler 响应缺少缓传字段时，Sender 仍能按50ms运行。
- Edge 无需增加新的缓传分支，正常接收按顺序到达的80包。
- 任务摘要能够区分 `realtime` 与 `buffered`，并显示实际发送间隔。
- 无新增设计文件以外的临时测试、缓存或实验产物。
- 完成后不自动提交、不推送，先向用户报告测试证据和最终文件列表。
