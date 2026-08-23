# 阶段 10：轴承全局分析物理归位设计

## 背景

阶段 3 已通过 `BearingGlobalAnalysisProvider` 将轴承风险、云端复核聚合和维护建议注入云端全局分析流程。当前主要运行入口已经通过场景注册表取得 Provider，但物理责任边界仍不完整：

- `cloud_service/global_analysis/v12_data_source.py` 仍直接读取轴承专属表和字段；
- `cloud_service/global_analysis/contracts.py` 仍定义轴承复核阈值与轴承工况阈值；
- `cloud_service/global_analysis/problem_detector.py` 仍包含轴承云复核候选规则；
- `cloud_service/global_analysis/service.py` 仍按轴承分析器名称和轴承结果字段编排扩展步骤；
- `periodic.py` 仍以 `bearing` 作为内部默认场景并依赖默认轴承数据源。

本批次继续执行原方案阶段 10 的物理目录整理，只处理全局分析职责。离线训练、模型更新、存储以及模型资产保留到后续独立批次。

## 目标与成功标准

目标是让平台全局分析只负责通用编排，让轴承插件拥有全部轴承数据读取、阈值、分析规则和结果扩展逻辑，同时保持外部行为不变。

成功标准：

1. 云端应用和周期任务通过场景 Provider 注入数据源与分析扩展，不自行装配轴承实现。
2. 轴承 V1.2 数据源、轴承配置和轴承候选规则的生产实现仅存在于 `scenarios/bearing/**`。
3. `cloud_service/global_analysis` 保留通用加载、设备分析、数据包分析、仲裁统计、物理证据分析、结果持久化和周期编排。
4. 原 API 路径、请求字段、响应字段、错误码、分析结果字段及默认轴承兼容行为不变。
5. 阈值数值、分析公式、排序、去重、候选生成和持久化顺序不变。
6. 旧导入路径继续可用，并与新路径导出同一实现对象；旧路径文件不得保留第二份算法实现。
7. 全局分析定向测试、架构测试、云端 API 测试和全仓测试全部通过。

## 方案选择

采用“场景实现完整归位＋平台扩展点通用化”。

不采用仅移动 `v12_data_source.py` 的最小方案，因为它会继续让平台服务认识轴承阈值、轴承分析器名称和轴承候选规则。也不采用整包移动 `cloud_service/global_analysis` 的方案，因为设备健康、数据包复核、仲裁统计、结果仓储和周期调度属于平台通用机制，不应归入轴承插件。

## 责任边界

### 平台保留

`cloud_service/global_analysis` 保留：

- 通用分析服务与执行顺序；
- 通用数据源协议；
- 设备健康分析；
- 数据包模型分析；
- 设备仲裁统计；
- 物理证据分析；
- 通用问题候选规则；
- 历史分析读取与结果持久化；
- 周期扫描、逐设备失败隔离和日志。

平台新增仅描述通用分析器所需字段的运行时配置协议。通用分析器依赖该协议，不再依赖包含轴承阈值的旧配置类。平台代码只处理通用结果段和 Provider 返回的场景扩展结果，不再硬编码轴承分析器名称。

### 轴承插件接管

`scenarios/bearing/cloud/global_analysis` 接管：

- V1.2 轴承历史结果读取、修订去重和云复核配对；
- 轴承风险分析；
- 轴承云复核聚合；
- 轴承维护建议；
- 轴承工况和云复核阈值；
- 轴承专属问题候选规则；
- 轴承结果段名称及兼容字段组装；
- 面向通用服务的轴承全局分析扩展对象。

### 兼容层接管

`compatibility/bearing_v12` 保留：

- 旧 `cloud_service.global_analysis.v12_data_source` 导入路径的转发；
- 旧配置导出的转发；
- 旧直接构造方式所需的轴承默认装配；
- 旧对象身份和异常透传。

兼容层只能装配和转发，不复制分析公式、SQL 或候选规则。

旧 `cloud_service.global_analysis.contracts.GlobalAnalysisConfig` 保持可导入，但该模块降为兼容入口，导出的轴承配置类实际定义在场景目录。通用运行路径改用独立的运行时配置协议和通用默认任务上限，不再经过这个旧配置入口。

## 最小扩展协议

在现有 `GlobalAnalysisProvider` 上增加一个小型运行时装配入口，而不是创建万能插件基类。Provider 为指定数据库路径返回以下依赖：

- 数据源；
- 场景配置；
- 场景分析步骤；
- 场景候选检测步骤。

通用服务继续接收显式依赖。场景分析步骤返回“结果字段名到结果值”的映射，因此平台可以合并 `bearing_risk_analysis`、`cloud_bearing_review_analysis` 和 `maintenance_recommendations`，但无需理解这些字段的轴承含义。场景候选检测步骤只返回新增候选列表，平台将其与通用候选列表按原顺序拼接。

现有 `build_analyzers()` 暂时保留，作为阶段 3 Provider 接口和旧测试的兼容能力；新运行入口不再按 `analyze_bearing_risk` 等名称编排。

## 数据流

### API 调用

1. `cloud_service/app.py` 从兼容请求中取得 `scenario_type`。
2. 场景注册表返回 `GlobalAnalysisProvider`。
3. Provider 按数据库路径创建轴承数据源和场景扩展。
4. 通用 `GlobalAnalysisService` 加载历史数据并计算通用分析结果。
5. 通用服务调用场景分析步骤并合并其结果段。
6. 通用候选规则先运行，轴承候选规则随后运行，保持原候选顺序。
7. 结果按原结构保存并返回。

### 周期调用

1. 周期任务仍由平台发现设备 subject。
2. `app.py` 将 Provider 运行时工厂传给周期执行器。
3. 周期执行器为每个 subject 使用相同的场景运行时配置。
4. 单个 subject 失败时继续记录异常并处理其他 subject。

### 旧直接构造

旧代码若仍直接构造 `GlobalAnalysisService(database_path)`，由明确的 bearing V1.2 兼容装配提供原默认数据源和配置。该路径仅为兼容保留；云端正式入口和周期入口必须使用注册表注入。

## 预计文件

预计新增或调整：

- `core/scenario_plugin.py`：声明最小全局分析运行时装配能力；
- `cloud_service/global_analysis/runtime_contracts.py`：定义通用配置协议和通用默认任务上限；
- `cloud_service/global_analysis/service.py`：改为合并通用结果与通用场景扩展结果；
- `cloud_service/global_analysis/periodic.py`：接收 Provider 运行时工厂；
- `cloud_service/global_analysis/contracts.py`：降为旧配置导入路径的兼容转发；
- `cloud_service/global_analysis/problem_detector.py`：仅保留通用候选规则；
- `cloud_service/global_analysis/v12_data_source.py`：改成无业务逻辑的旧路径兼容转发；
- `cloud_service/app.py`：从注册表取得完整全局分析运行时依赖；
- `scenarios/bearing/cloud/global_analysis/config.py`：拥有轴承阈值；
- `scenarios/bearing/cloud/global_analysis/v12_data_source.py`：拥有原 V1.2 SQL 和修订规则；
- `scenarios/bearing/cloud/global_analysis/problem_detector.py`：拥有轴承候选规则；
- `scenarios/bearing/cloud/global_analysis/provider.py`：装配轴承运行时并保留旧 analyzer 导出；
- `compatibility/bearing_v12/global_analysis_exports.py`：集中维护旧导出和默认装配；
- 全局分析场景测试、架构测试和兼容性测试。

最终文件集合以测试先行阶段发现的真实调用关系为准；任何新增文件都必须直接服务上述边界，不能顺带整理其他模块。

## 兼容与错误处理

- 未知场景仍由现有入口返回 `UNSUPPORTED_SCENARIO`。
- 缺少场景能力时沿用注册表现有错误，不静默回退到轴承。
- 旧请求未提供场景时，只由现有兼容入口补充 `bearing`。
- Provider、数据源和分析器异常原样透传给现有 API 错误处理。
- 周期执行继续逐 subject 隔离异常。
- SQL、表缺失处理、JSON 容错和修订排序原样保留。
- 不新增重试、缓存或降级逻辑。

## 测试策略

### 基线冻结

在生产代码移动前冻结：

- V1.2 修订去重结果；
- 轴承复核配对结果；
- 设备健康、数据包、仲裁和物理证据结果；
- 轴承风险、云复核和维护建议；
- 问题候选内容与顺序；
- API 响应和周期任务成功列表；
- 关键分析结果的规范化金丝雀。

随机生成的 `analysis_id`、`problem_id` 和时间戳在对比时只做格式检查，其他稳定字段必须完全一致。

### 契约与架构测试

- 新旧 V1.2 数据源和配置导出对象身份一致；
- 旧模块仅包含明确转发，不定义第二份受保护实现；
- `cloud_service/app.py` 和通用全局分析模块不得导入 `scenarios.bearing`；
- 轴承 SQL、阈值和候选规则只能由轴承场景目录拥有；
- Provider 创建的数据源、配置和步骤正确；
- 正式 API 与周期入口实际使用 Provider 注入路径；
- 旧直接构造路径继续产生相同结果。

### 回归测试

- 全局分析 analyzer、service、V1.2、periodic 和端到端测试；
- 场景 Provider 与架构规则测试；
- 云端 API、存储和模型更新邻接回归；
- 全仓严格测试。

## 明确不修改

本批次不修改：

- 任何分析公式或阈值数值；
- 结果字段名称、顺序要求或业务含义；
- V1.2 表名、字段名、SQL 语义和修订选择规则；
- API 路径、请求响应、状态码和错误码；
- 周期频率、失败隔离和日志语义；
- H5、MOMENT、模型权重或 Git LFS；
- 离线训练、模型更新、模型生命周期和存储物理布局；
- 调度、一致性和仲裁规则；
- 用户已有删除项和未跟踪文档。

## 实施顺序与停止条件

1. 冻结全局分析基线与工作区保护清单。
2. 先写布局、对象身份和注入路径的失败测试。
3. 移动轴承配置、V1.2 数据源和候选规则，旧路径改为兼容转发。
4. 扩展 Provider 并切换 API、周期和通用服务装配。
5. 运行定向、邻接和全仓回归。
6. 独立复审后关闭 Critical 和 Important 问题。

若必须改变分析公式、阈值、SQL 语义、结果结构、API 语义或数据库结构才能继续，立即停止并报告。若旧直接构造兼容与通用服务边界无法同时满足，也停止并由用户选择是否保留该兼容债务。
