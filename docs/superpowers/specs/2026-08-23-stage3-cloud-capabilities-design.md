# 阶段3：云端诊断、全局分析与模型更新解耦设计

## 范围与选择

采用薄Provider方案：轴承插件新增云端诊断、全局分析和模型更新Provider，内部继续调用已经验证的实现；云端应用只通过场景注册表取得能力。相比物理移动实现或只隐藏导入，此方案能改变依赖方向且最小化行为风险。

## 组件

- `BearingCloudDiagnosisProvider`：按数据库路径创建原 `BearingCloudHandler`，保留诊断和现有仲裁兼容调用。
- `BearingGlobalAnalysisProvider`：提供轴承风险、云复核聚合和维护建议分析器，并声明 `scenario_id`。
- `BearingModelUpdateProvider`：原样装配训练数据源、标签确认链、H5/MOMENT Trainer和 `ModelUpdateService`，代理本地MOMENT候选激活与回滚。
- `BearingScenarioPlugin`：将 `cloud_diagnosis`、`global_analysis`、`model_update` 注册为可执行能力。
- `cloud_service/app.py`：通过注册表选择Provider；保留旧 `get_scenario_handler` 模块级注入点，并将未知场景继续转换为原API错误。

## 数据流

云端请求从兼容字段取得场景标识，注册表返回相应Provider。诊断Provider创建原Handler；全局分析Provider向通用 `GlobalAnalysisService` 注入原分析函数；模型更新Provider创建原服务实例。服务、算法和数据库调用次序保持不变。

## 错误与兼容

- 未知场景继续返回 `UNSUPPORTED_SCENARIO`。
- 旧请求缺少场景标识时继续使用已有兼容默认值。
- Provider不捕获或改写原诊断、全局分析、模型更新异常。
- 原 `/cloud/*` 路径、请求响应、状态码及测试monkeypatch入口保留。

## 验证

- 注册表能够取得三个可执行云端能力。
- Provider输出的分析器集合和原装配一致。
- 旧诊断Handler、周期分析和模型更新服务构造行为一致。
- 架构测试禁止 `cloud_service/app.py` 直接导入 `scenarios.bearing`。
- 运行固定MOMENT探针、云端API、全局分析、模型更新及全量严格测试。

## 非目标

不修改MOMENT算法、checkpoint、归一化、标签、分析公式、模型更新状态机、调度/仲裁业务规则、数据库、API、周期频率或降级语义；不移动现有实现文件。
