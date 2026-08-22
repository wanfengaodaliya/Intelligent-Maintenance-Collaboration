# P1-v2 训练与正式对照验证后续实施方案

> 适用工作树：`D:\codex_workspace\.worktrees\p1-v2-context-correctness-sla`
>
> 当前状态：代码链、配置、测试和证据聚合逻辑已完成；尚未生成可用的 P1-v2 checkpoint，也尚未在本工作树中生成正式 `output/p1_v2` 结果。

## 1. 目标

在不直接替换固定规则 R0 的前提下，完成 P1-v2 的训练、冻结、正式配对仿真和真实调度 shadow 验证，回答以下问题：

1. P1-v2 是否在相同任务、网络事件和测试 seed 下改善 R0 的最终按时完成率和时延。
2. P1-v2 的本地定稿是否引入 `wrong_local`、Macro-F1 或故障召回率退化。
3. 缺少上下文、checkpoint、云模型或网络条件不满足时，是否与 R0 保持一致回退。
4. 结果是否具有可追溯的 seed、场景、checkpoint lineage 和 SHA-256 证据。

本方案的成功条件不是“P1 一定胜出”，而是得到可复现的通过或不通过结论；任何安全性或正确性门禁失败时都保持 R0。

## 2. 全局约束

- 历史对比实验目录只读，不覆盖、不重跑覆盖、不改写旧结果。
- 新结果只能写入 `cloud_edge_project/output/p1_v2/<run_id>/`，`run_id` 已存在时直接失败。
- R0 的规则、阈值和动作空间保持冻结，P1-v2 只能在安全合法动作集合内选择。
- 训练、验证、测试 seed 严格分离，不使用测试结果调参。
- P1-v2 使用 `p1-features-v2` 的 17 维特征；旧 15 维 checkpoint 不得加载。
- 冻结推理不调用在线 `observe()`，本阶段不启用线上学习。
- `FINAL` 才能计入 `on_time_final_rate`；`PROVISIONAL` 单独统计，不能伪装成最终完成。
- 训练和验证使用项目声明的 Python 环境；不要把依赖安装到不匹配的系统 Python 中。

## 3. 实施阶段

### 阶段一：基线和环境确认

**涉及文件：**

- `cloud_edge_project/configs/p1_v2_experiment.json`
- `cloud_edge_project/experiments/scheduler_comparison/cli.py`
- `cloud_edge_project/experiments/scheduler_comparison/training.py`
- `cloud_edge_project/scheduler/tests/test_p1_v2_config.py`

**步骤：**

1. 记录当前提交、Python 版本、NumPy 版本和项目依赖版本。
2. 确认 `train_seeds`、`validation_seeds`、`test_seeds` 两两不重叠。
3. 确认八个场景为 `S1`–`S8`，`bootstrap_seed=20260818`。
4. 确认 `output/p1_v2` 下没有同名 run；如已有结果，不删除，改用新的 run-id。
5. 使用可写且可清理的 pytest basetemp 运行配置、上下文、特征、聚合和回退测试。

**验收：**配置解析通过；17 维特征名和版本固定；旧 checkpoint 被拒绝；测试环境中的 `PermissionError` 单独记录，不能伪装成业务测试失败。

### 阶段二：训练、验证和冻结 checkpoint

**涉及文件：**

- `cloud_edge_project/experiments/scheduler_comparison/training.py`
- `cloud_edge_project/experiments/scheduler_comparison/cli.py`
- `cloud_edge_project/experiments/scheduler_comparison/policies/p1_features_v2.py`
- `cloud_edge_project/configs/p1_v2_experiment.json`

**推荐输出根目录：**

```powershell
$runRoot = 'output\p1_v2\formal-20260822-v1'
```

**执行顺序：**

```powershell
python -m experiments.scheduler_comparison.cli train `
  --output-root $runRoot `
  --config configs\p1_v2_experiment.json

python -m experiments.scheduler_comparison.cli validate `
  --output-root $runRoot `
  --config configs\p1_v2_experiment.json

python -m experiments.scheduler_comparison.cli verify `
  --output-root $runRoot `
  --config configs\p1_v2_experiment.json
```

**冻结产物必须包括：**

- `models/p1/checkpoint.json`
- `models/p1/FROZEN`
- `models/p1/p1_v2_freeze_metadata.json`
- `configs/frozen_experiment.json`
- `experiment_manifest.json`

**冻结验收：**

- checkpoint 的 `feature_version` 为 `p1-features-v2`；
- 特征维度为 17，特征名顺序与代码一致；
- provenance 中包含训练 seed、场景、样本数量、动作数量和 `DecisionLevel`；
- `FROZEN` 中的 SHA-256 与 checkpoint 文件一致；
- lineage 来自 checkpoint 自身，不由当前配置临时伪造；
- 训练完成后，测试运行前禁止修改冻结目录。

### 阶段三：正式 R0/P1-v2 配对仿真

**执行命令：**

```powershell
$testRun = 'formal-20260822-v1-test'

python -m experiments.scheduler_comparison.cli run `
  --output-root $runRoot `
  --config configs\p1_v2_experiment.json `
  --run-id $testRun `
  --policy R0,P1_V2 `
  --scenario S1,S2,S3,S4,S5,S6,S7,S8 `
  --seed 7,11,17,23,27,31,37,43,47,53 `
  --fail-if-exists

python -m experiments.scheduler_comparison.cli aggregate `
  --output-root $runRoot `
  --config configs\p1_v2_experiment.json `
  --run-id $testRun
```

**配对要求：**

- R0 和 P1-v2 使用完全相同的任务、真值、网络事件、云事件和测试 seed；
- 两个策略维护独立业务队列，不能共享动作造成相互影响；
- 每个场景都必须有多个合法动作样本，不能用“只有一个合法动作”的样本冒充模型收益；
- 配对 bootstrap 以注册的测试 seed 为独立单位，不能把每条 task row 当作独立样本；
- 输出至少包括 `metrics_by_run.csv`、`scenario_metrics.json`、`correctness_metrics.json`、`paired_comparisons.json`、`safety_violations.csv`、`integrity_report.json` 和 `winner_report.md`。

### 阶段四：结果判定

聚合结果必须同时检查以下门禁：

| 指标 | 门禁 |
|---|---:|
| 网络故障场景 `on_time_final_rate` | `>= 0.90` |
| 至少两类网络场景 `latency_p95_s` | `<= 0.2` |
| `wrong_local_rate` 相对 R0 增幅 | `<= 0.002` |
| Macro-F1 相对 R0 下降 | `<= 0.005` |
| 故障召回率相对 R0 下降 | `<= 0.005` |
| permanent failure / expired | 不高于 R0 |
| safety violations | `0` |
| P1 内部决策耗时 P95 | `<= 5 ms` |
| 缺上下文、缺模型、OOD 回退一致率 | `100%` |

结果判定必须分开报告：

1. 代码和单元测试是否通过；
2. 冻结仿真是否通过；
3. R0/P1-v2 配对 CI 是否通过；
4. 正确性覆盖率是否足够；
5. 是否已经完成真实 HTTP A/B；
6. 哪些结论仍不能外推到真实物理网络和生产诊断准确率。

如果性能提升但正确性、安全性或证据完整性失败，结论为“不允许切换，继续 R0”。

### 阶段五：真实调度 shadow

**涉及文件：**

- `cloud_edge_project/common/schemas.py`
- `cloud_edge_project/scheduler/p1_context.py`
- `cloud_edge_project/scheduler/p1_policy_adapter.py`
- `cloud_edge_project/scheduler/rule_scheduler.py`
- 真实调度请求的生产侧构造模块

**实施顺序：**

1. 让生产请求携带真实 `routing_context`，包括 deadline、云模型状态、网络 RTT/goodput、队列和重试状态。
2. 先使用 `SCHEDULER_ROUTING_POLICY=p1_v2_shadow`，实际执行 R0，只记录 P1-v2 候选动作、上下文质量和回退原因。
3. 校验 shadow 不改变业务队列、不调用额外的业务云执行、不影响端到端时延。
4. 对缺失、过期、非法和 OOD 上下文逐类验证 R0 回退。
5. 只有真实上下文覆盖率、checkpoint 加载率和回退原因稳定后，才进入小流量 canary。

### 阶段六：canary、回滚和最终切换

建议模式顺序：

```text
r0 -> p1_v2_shadow -> p1_v2_canary -> p1_v2
```

canary 固定设备白名单和流量比例，持续记录：

- `policy_id`
- `context_quality`
- `reason_codes`
- `action_distribution`
- `wrong_local_rate`
- `on_time_final_rate`
- `permanent_failure_rate`
- `expired_rate`
- `decision_duration_p95_ms`

任一安全门、正确性门或 checkpoint 完整性失败，立即回到 R0；性能门失败则保持 shadow，不通过改指标口径放行。

## 4. 最终交付物

完成后应保留以下证据索引，而不是只提交一张汇总表：

- 代码提交 SHA；
- checkpoint SHA-256 和 lineage；
- 冻结配置及 seed 列表；
- formal manifest；
- `winner_report.md`；
- 配对 CI 和逐场景指标；
- 测试命令、Python 环境和测试输出；
- HTTP A/B 的入口、端口、请求数和结果目录；
- 未完成的真实网络或生产结论边界。

只有当上述证据链完整，且所有发布门禁通过，才可以讨论把默认模式从 R0 改为 P1-v2。
