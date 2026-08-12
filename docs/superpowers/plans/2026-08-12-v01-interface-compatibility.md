# V0.1 Interface Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留现有轴承工作流的同时，实现根目录接口文档规定的 V0.1 `task_id` 全链路。

**Architecture:** HTTP 入口根据请求结构分流到 V0.1 兼容实现或现有轴承实现。公共校验集中在 `common/schemas.py`，日志层统一读取两类 JSONL 记录，仲裁服务保持独立。

**Tech Stack:** Python 3.12、FastAPI、pytest、JSONL、YAML

## Global Constraints

- 不改变现有轴承 `packet_id`、发送器任务分配和云端分析接口行为。
- V0.1 字段名、端口和路由值严格遵循根目录《云边协同项目接口说明文档》。
- 服务地址、端口和模型路径不得写死；端口从 `configs/local.yaml` 读取。
- 生产代码前必须先运行对应失败测试；每个任务完成后运行相关测试。
- 只实现 V0.1 最小规则，不实现 PER-DDPG 训练、前端、雾执行器或真实模型部署。

---

### Task 1: 公共契约与边缘/云端推理兼容

**Files:**
- Modify: `cloud_edge_project/common/schemas.py`
- Modify: `cloud_edge_project/edge_service/model.py`
- Modify: `cloud_edge_project/edge_service/app.py`
- Modify: `cloud_edge_project/cloud_service/app.py`
- Create: `cloud_edge_project/tests/test_v01_inference.py`

**Interfaces:**
- Consumes: V0.1 `TaskRequest` 和文档 6.3 的云端请求。
- Produces: `validate_task_request(payload)`, `validate_task_log(payload)`, `infer_edge_v01(task)`, `infer_cloud_v01(payload)`；公开 HTTP 路由保持 `/edge/infer`、`/cloud/infer`、`/health`。

- [ ] **Step 1: 写失败测试**

```python
def test_edge_v01_returns_documented_contract():
    result = infer_edge_v01(TASK)
    assert result["task_id"] == "task_0001"
    assert set(result) == {"task_id", "node_id", "model_name", "label", "confidence", "risk_level", "edge_latency_ms", "need_cloud"}

def test_cloud_v01_returns_documented_contract():
    result = infer_cloud_v01(CLOUD_REQUEST)
    assert result["task_id"] == "task_0001"
    assert set(result["decision"]) == {"action", "description"}

def test_task_request_rejects_out_of_range_priority():
    payload = {**TASK, "priority": 1.1}
    with pytest.raises(ContractError):
        validate_task_request(payload)
```

- [ ] **Step 2: 验证红灯**

Run: `.venv/Scripts/python.exe -m pytest cloud_edge_project/tests/test_v01_inference.py -q`
Expected: FAIL，缺少 V0.1 校验和推理符号。

- [ ] **Step 3: 写最小实现**

在 `common/schemas.py` 校验所有必填字段、0~1 比例、正时延和合法 route；边缘推理使用温度、振动、电流、负载产生确定性标签与置信度；云端推理在边缘结果基础上返回更完整的 `decision`。HTTP 入口仅在 V0.1 请求结构出现时调用新实现，否则保留原调用。

- [ ] **Step 4: 验证绿灯与回归**

Run: `.venv/Scripts/python.exe -m pytest cloud_edge_project/tests/test_v01_inference.py cloud_edge_project/cloud_service/tests cloud_edge_project/edge_service/verification -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add cloud_edge_project/common/schemas.py cloud_edge_project/edge_service/model.py cloud_edge_project/edge_service/app.py cloud_edge_project/cloud_service/app.py cloud_edge_project/tests/test_v01_inference.py
git commit -m "feat: 支持 V0.1 边缘与云端推理契约"
```

### Task 2: 调度接口兼容

**Files:**
- Modify: `cloud_edge_project/scheduler/api.py`
- Modify: `cloud_edge_project/scheduler/rule_scheduler.py`
- Create: `cloud_edge_project/tests/test_v01_scheduler.py`

**Interfaces:**
- Consumes: 文档 6.2 的 `task`、`edge_result`、`network_state`、`node_state`。
- Produces: 仅含文档规定六字段的 V0.1 `ScheduleDecision`；发送器分配响应保持不变。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.parametrize(("cloud_available", "confidence", "route"), [
    (False, 0.5, "fallback_edge"),
    (True, 0.79, "cloud"),
    (True, 0.80, "edge"),
])
def test_documented_scheduler_rules(cloud_available, confidence, route):
    request = schedule_request(cloud_available, confidence)
    assert decide(request)["route"] == route
    assert set(decide(request)) == {"task_id", "route", "target_node", "reason", "estimated_total_latency_ms", "upload_required"}
```

- [ ] **Step 2: 验证红灯**

Run: `.venv/Scripts/python.exe -m pytest cloud_edge_project/tests/test_v01_scheduler.py -q`
Expected: FAIL，入口仍将 V0.1 请求交给发送器分配调度器或返回额外内部字段。

- [ ] **Step 3: 写最小实现**

在 `scheduler/api.py` 用嵌套 `task` 识别 V0.1 请求并调用 `decide_schedule`；V0.1 输出删除 `scheduler` 和 `policy_score` 内部字段。规则顺序严格为云不可用、低置信度、其余留边缘。

- [ ] **Step 4: 验证绿灯与回归**

Run: `.venv/Scripts/python.exe -m pytest cloud_edge_project/tests/test_v01_scheduler.py cloud_edge_project/sender_module/tests -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add cloud_edge_project/scheduler/api.py cloud_edge_project/scheduler/rule_scheduler.py cloud_edge_project/tests/test_v01_scheduler.py
git commit -m "feat: 兼容 V0.1 调度决策接口"
```

### Task 3: V0.1 日志、指标与任务查询

**Files:**
- Modify: `cloud_edge_project/common/logger.py`
- Modify: `cloud_edge_project/log_service/app.py`
- Create: `cloud_edge_project/tests/test_v01_logging.py`

**Interfaces:**
- Consumes: V0.1 `TaskLog` 或现有 `TaskTrace` JSONL 记录。
- Produces: `/logs/task_trace` 保存确认；文档八项 `/dashboard/metrics` 指标；V0.1 `/dashboard/tasks` 行字段。

- [ ] **Step 1: 写失败测试**

```python
def test_v01_log_is_saved_and_included_in_metrics(tmp_path):
    config = config_for(tmp_path)
    saved = append_task_trace(TASK_LOG, config)
    metrics = compute_metrics(read_task_traces(config))
    assert saved == {"task_id": "task_0001", "saved": True, "log_path": "logs/task_trace.jsonl"}
    assert metrics["avg_latency_ms"] == 154.0
    assert metrics["cloud_call_ratio"] == 1.0
    assert metrics["conflict_rate"] == 0.0

def test_metrics_use_nearest_rank_p95():
    assert compute_metrics([log_with_latency(10), log_with_latency(30)])["p95_latency_ms"] == 30.0
```

- [ ] **Step 2: 验证红灯**

Run: `.venv/Scripts/python.exe -m pytest cloud_edge_project/tests/test_v01_logging.py -q`
Expected: FAIL，旧校验要求 `packet_id` 且指标名不匹配。

- [ ] **Step 3: 写最小实现**

`append_task_trace` 按标识字段选择校验器；`compute_metrics` 返回新旧兼容的指标超集并用 nearest-rank 计算 P95；冲突率按全部任务计算，冲突解决率按有冲突任务计算；日志路由对 V0.1 与旧记录分别投影公开列表字段。

- [ ] **Step 4: 验证绿灯与回归**

Run: `.venv/Scripts/python.exe -m pytest cloud_edge_project/tests/test_v01_logging.py cloud_edge_project/tests/test_v01_inference.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add cloud_edge_project/common/logger.py cloud_edge_project/log_service/app.py cloud_edge_project/tests/test_v01_logging.py
git commit -m "feat: 实现 V0.1 日志与仪表盘指标"
```

### Task 4: 冲突仲裁、配置与端到端集成

**Files:**
- Create: `cloud_edge_project/consistency_service/__init__.py`
- Create: `cloud_edge_project/consistency_service/resolver.py`
- Create: `cloud_edge_project/consistency_service/app.py`
- Create: `cloud_edge_project/tests/test_v01_consistency.py`
- Create: `cloud_edge_project/tests/test_v01_end_to_end.py`
- Modify: `cloud_edge_project/configs/local.yaml`
- Modify: `cloud_edge_project/start_all.py`
- Modify: `cloud_edge_project/examples/task_industrial.json`
- Modify: `cloud_edge_project/examples/edge_result.json`
- Modify: `cloud_edge_project/examples/schedule_decision.json`

**Interfaces:**
- Consumes: 文档 6.4 的 `decision_id`、`scenario`、`decisions`、`global_constraints`。
- Produces: `/consistency/resolve`、8005 `/health`，并通过 `start_all.py --service consistency_service` 启动。

- [ ] **Step 1: 写失败测试**

```python
def test_opposite_actions_choose_highest_priority_decision():
    result = resolve_decisions(CONFLICT_REQUEST)
    assert result == {
        "decision_id": "decision_001", "has_conflict": True,
        "conflict_type": "opposite_action", "final_action": "charge",
        "selected_source_node": "edge_1",
        "reason": "charge decision has higher priority and confidence",
        "resolved": True,
    }

def test_main_flow_uses_one_task_id_through_all_results(tmp_path):
    edge = infer_edge_v01(TASK)
    schedule = decide(schedule_request_for(TASK, edge))
    cloud = infer_cloud_v01(cloud_request_for(TASK, edge)) if schedule["route"] == "cloud" else None
    assert {TASK["task_id"], edge["task_id"], schedule["task_id"], cloud["task_id"]} == {"task_0001"}
```

- [ ] **Step 2: 验证红灯**

Run: `.venv/Scripts/python.exe -m pytest cloud_edge_project/tests/test_v01_consistency.py cloud_edge_project/tests/test_v01_end_to_end.py -q`
Expected: FAIL，仲裁模块尚不存在且启动配置缺少服务。

- [ ] **Step 3: 写最小实现**

校验同一目标设备的相反动作和最大功率约束；按 `(priority, confidence)` 降序选择决策；增加 FastAPI 路由、健康检查、YAML 配置和启动项。三个示例 JSON 写入无注释的有效文档样例。

- [ ] **Step 4: 验证绿灯与全量回归**

Run: `.venv/Scripts/python.exe -m pytest cloud_edge_project/tests -q`
Expected: PASS。

Run: `.venv/Scripts/python.exe -m pytest cloud_edge_project -q`
Expected: 现有 68 项与新增测试全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add cloud_edge_project/consistency_service cloud_edge_project/tests/test_v01_consistency.py cloud_edge_project/tests/test_v01_end_to_end.py cloud_edge_project/configs/local.yaml cloud_edge_project/start_all.py cloud_edge_project/examples
git commit -m "feat: 补齐 V0.1 仲裁与端到端集成"
```
