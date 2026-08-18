# 全局分析与分层优化任务实施规格（最小可行版）

## 1. 目标与范围

本规格只改造以下两项能力：

1. 全局分析：识别包级诊断、轴承级汇总、设备级仲裁中的持续问题；
2. 优化任务：创建、验证、批准、发布和回验模型或规则更新。

三层都必须可以创建优化任务：

| 目标层 `target_layer` | 支持的 `update_type` |
| --- | --- |
| `packet_diagnosis` | `model_update`、`rule_update` |
| `bearing_aggregation` | `model_update`、`rule_update` |
| `device_arbitration` | `rule_update` |

本规格不实现上游采集、实时诊断、训练算法本身或边缘运行时；这些模块只需按本规格提供已存在的结构化历史结果。禁止为此引入消息队列、通用工作流引擎或新的微服务。

## 2. 设计原则

- **一套任务、三层适配。** 状态机、审批、审计和发布流程共用；各层只实现自己的分析、候选校验和回放验证。
- **安全优先。** 数据不足只观察；验证不通过不能批准；灰度异常立即保持或恢复旧版本。
- **口径可追溯。** 每个分析、任务和回验均保存版本、时间窗、阈值与证据快照。
- **先规则后模型。** 轴承汇总和设备仲裁优先使用可回放规则；模型候选仅在具备稳定标签和特征契约后启用。

## 3. 最小架构

```text
GlobalAnalysisService
  ├─ PacketAnalysisAdapter
  ├─ BearingAggregationAnalysisAdapter
  └─ DeviceArbitrationAnalysisAdapter
             ↓
   OptimizationDecisionService
             ↓
       OptimizationTaskService
             ↓
CandidateAdapter（按 target_layer）
             ↓
验证 → 审批 → 灰度发布 → 回验
```

`GlobalAnalysisService` 负责一次分析的编排和持久化。三个分析适配器返回相同结构，不直接创建或发布更新。`OptimizationTaskService` 是唯一允许变更任务状态的服务。候选适配器只处理目标层特有的文件与回放逻辑。

## 4. 统一分析结果

每个目标层输出一个 `LayerAnalysisResult`：

```json
{
  "target_layer": "packet_diagnosis",
  "problem_detected": true,
  "problem_type": "risk_underestimation",
  "problem_context": {"load_bucket": "high"},
  "sample_coverage": {
    "total_count": 80,
    "context_count": 28,
    "random_review_count": 12,
    "selection_bias_note": "复核样本含调度上云样本与随机影子样本"
  },
  "evidence": {
    "edge_cloud_agreement_rate": 0.72,
    "correction_rate": 0.28,
    "underestimation_rate": 0.21,
    "overestimation_rate": 0.07
  },
  "persistence": {
    "window_count": 2,
    "consecutive_problem_windows": 2,
    "is_persistent": true
  },
  "baseline_version": "edge_packet_model_v1",
  "suggested_action": "model_update"
}
```

全局分析最终结果保留现有 `device_health`、`bearing_risk`、冲突指标，并新增：

```text
packet_diagnosis_analysis
bearing_aggregation_analysis
device_arbitration_analysis
```

每一项均为 `LayerAnalysisResult` 或 `problem_detected=false` 的同构对象。

### 4.1 问题类型

| 层级 | 支持的问题类型 |
| --- | --- |
| 包级诊断 | `risk_underestimation`、`risk_overestimation`、`condition_weakness` |
| 轴承汇总 | `aggregation_underestimation`、`aggregation_overestimation`、`excessive_uncertainty` |
| 设备仲裁 | `high_conflict_rate`、`unresolved_conflict`、`repeated_conflict_pattern` |

包级低估定义为边缘状态严重度低于云端参考状态；高估定义相反。轴承级使用边缘汇总与云端轴承参考结果比较。设备级以仲裁记录的冲突和未解决状态为依据。`resolved` 仅代表系统已作出决定，不得称为仲裁正确率。

### 4.2 统计门槛

所有门槛存入配置，并连同实际取值写入 `evidence`：

- 一个分析窗口默认 7 天；
- 至少 2 个连续窗口均满足问题阈值，才标记为持续问题；
- 每层总有效样本默认不少于 20；具体问题工况样本默认不少于 10；
- 包级候选创建阈值默认：修正率不低于 15%，或低估率不低于 10%；
- 任何缺少版本、标签、工况或最小样本的数据均标记 `insufficient_data`，只输出 `observe`。

统计结果必须保存 `review_selection_reason`。对于包级模型表现，随机影子复核应按工况分层保留少量样本；分析结果必须分别给出“全部复核样本”和“随机复核样本”口径，避免把低置信度上云样本误当作全量准确率。

## 5. 优化任务

将现有 `ModelUpdateTask` 扩展并统一命名为 `OptimizationTask`。为减少迁移范围，可以保留现有表名 `model_update_task`，但任务 API 和领域对象统一使用新名称。

```json
{
  "task_id": "opt_...",
  "analysis_id": "ga_...",
  "target_layer": "bearing_aggregation",
  "update_type": "rule_update",
  "problem_type": "aggregation_underestimation",
  "problem_context": {"window": "20_packets"},
  "baseline_version": "bearing_rule_v1",
  "candidate_version": "bearing_rule_v2",
  "candidate_artifact": {"path": "...", "sha256": "...", "schema_version": "..."},
  "evidence_snapshot": {},
  "status": "created"
}
```

`create` 必须从指定 `analysis_id` 读取并复制证据快照，调用方不能自填问题类型或基线指标。创建规则如下：

1. `suggested_action=observe` 或 `is_persistent=false` 时，返回 `observe`，不创建任务；
2. `target_layer` 与 `update_type` 组合不在第 1 节表格内时拒绝；
3. `baseline_version`、候选文件哈希和候选契约必须存在；
4. 每个任务只对应一个层、一个候选版本和一份分析证据。

## 6. 候选适配器与验证

候选适配器接口保持简单：

```text
validate_artifact(task) -> artifact metadata or error
run_replay(task, frozen_samples) -> ValidationResult
```

三类适配器要求如下：

| 层级 | 候选包最小内容 | 回放主指标 |
| --- | --- | --- |
| 包级诊断 | 模型或规则、特征/预处理版本、训练数据版本（模型时） | 低估率、异常召回、一致率 |
| 轴承汇总 | 规则或模型、输入包窗口与字段契约 | 汇总修正率、低估率、不确定率 |
| 设备仲裁 | 规则集、严重度排序、冲突触发配置 | 未解决冲突率、重复冲突率、规则确定性 |

验证样本为冻结的时间留出集：候选训练或规则调参使用的数据不得进入该集合。云端复核结果可作为参考标签，但报告必须标识其标签来源；没有人工或检修真值时，结果名称为“参考一致率”，不得称“准确率”。

候选只有同时满足下列条件才能进入 `waiting_approval`：

1. 留出集样本数达到配置的最小值；
2. 本层主指标优于基线，或达到明确的安全目标；
3. 包级低估率和关键工况指标不超过允许退化阈值；
4. 候选包、特征或规则契约验证成功。

任一条件不满足，状态为 `validation_failed`。`approve` 接口只接受 `waiting_approval`，不得提供人工绕过验证失败的入口。

## 7. 发布与回滚

任务状态机：

```text
created
→ waiting_validation_data
→ validating
→ validation_failed | waiting_approval
→ approved
→ rollout_pending
→ canary_active
→ active
→ verifying
→ succeeded | rolled_back
```

最小发布流程：

1. 云端提供候选包及 SHA-256；
2. 首批目标节点下载并校验哈希；
3. 节点加载候选包并执行健康检查，回传 `ack`、当前版本和错误码；
4. 默认仅激活 10% 目标节点（最少 1 个）；通过观察窗口后再扩大到全部节点；
5. 下载、校验、加载或健康检查失败时，节点继续使用基线版本；
6. 灰度期间关键安全指标恶化或节点报告加载失败时，已激活节点恢复 `baseline_version`，任务变为 `rolled_back`。

规则更新与模型更新使用相同状态机。规则发布必须原子替换完整规则包，不能在线编辑单个阈值。

## 8. 回验

任务激活后，以与基线相同的层级、工况、指标和窗口长度执行分析。回验结果至少包含：

```text
baseline_version
candidate_version
baseline_metrics
active_metrics
metric_delta
coverage
decision: succeeded | rolled_back | insufficient_data
```

样本不足时保留 `verifying` 或返回 `insufficient_data`，不得将缺数据视为成功。仅当回验满足发布门禁中的关键安全指标时，任务进入 `succeeded`。

## 9. 最小数据变更

现有 `global_analysis_result` 的 `result_json` 可直接承载完整分层结果；为支持检索，应增加以下索引字段：

```text
packet_problem_detected
bearing_problem_detected
arbitration_problem_detected
```

现有 `model_update_task` 增加：

```text
target_layer
problem_type
problem_context_json
baseline_version
candidate_artifact_json
evidence_snapshot_json
rollout_result_json
post_validation_json
rollback_result_json
```

保留旧的 `old_version`、`new_version` 读取兼容，并在写入时映射为 `baseline_version`、`candidate_version`。任务状态和 JSON 字段均必须由服务端更新，不能由客户端直接修改。

## 10. 实现边界与验收标准

实现只涉及以下目录：

```text
cloud_service/global_analysis/
cloud_service/model_update/
scenarios/bearing/cloud/global_analysis/
cloud_service/storage/schema.py
cloud_service/storage/database.py
```

验收标准：

1. 三层均能从同一分析结果创建合法的 `OptimizationTask`；
2. 数据不足、非持续问题或不支持的层/类型组合不能创建任务；
3. 包级分析能输出低估、高估、工况和持续性字段；
4. 三种候选适配器均能对非法候选包拒绝并产生明确错误码；
5. 验证未达标的任务无法批准；
6. 发布记录包含每个节点的下载、哈希、加载和激活 ACK；
7. 灰度失败能将已激活节点恢复到基线版本；
8. 回验仅比较相同层级、版本和工况口径，并在数据不足时不宣告成功。

## 11. 取舍

本版本不实现自动训练、统计显著性检验、自动阈值寻优、跨设备迁移学习或全量 A/B 平台。它们可以在本规格跑通、并积累可靠标签和版本数据后再增加。当前目标是确保每一次优化都可定位、可验证、可发布、可回滚和可回验。
