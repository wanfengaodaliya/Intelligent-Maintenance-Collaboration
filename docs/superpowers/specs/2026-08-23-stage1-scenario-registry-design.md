# 阶段1：通用契约与场景注册机制设计

## 范围

本阶段只增加平台通用契约、可选能力协议、实例化场景注册表、轴承场景清单和唯一装配入口。现有云端模块级 `ScenarioHandler` 注册函数继续保留，所有服务仍走原运行路径。

## 方案

- 通用契约使用不可变数据类，只包含 `scenario_id`、`unit_id`、`state` 等平台字段。
- 每项场景能力使用独立 `Protocol`，不建立万能插件基类。
- `ScenarioPlugin` 暴露不可变清单、能力映射和配置校验。
- `ScenarioRegistry` 显式注册插件，拒绝重复 ID，并在场景或能力不存在时给出带上下文的错误。
- 轴承插件声明九项既有能力。能力绑定使用未解析引用描述原实现，本阶段不导入或初始化模型；请求可执行 Provider 时会明确报告能力尚未完成适配。
- `bootstrap/scenarios.py` 是新插件的唯一生产装配入口；现有服务入口暂不切换。

## 数据流

启动装配代码创建注册表并注册 `BearingScenarioPlugin`。调用方可以按 `scenario_id` 获取插件，再按能力标识获取提供器。能力引用只有在后续阶段显式解析时才加载原实现。

## 错误处理

- 空场景 ID、空版本或空能力名在对象边界直接拒绝。
- 重复注册抛出 `DuplicateScenarioError`。
- 未注册场景抛出 `ScenarioNotFoundError`。
- 缺失能力抛出 `MissingScenarioCapabilityError`，错误信息包含场景和能力。
- 能力已声明但尚未绑定可执行 Provider 时抛出 `UnresolvedScenarioCapabilityError`。
- 插件清单与注册能力不一致时抛出 `InvalidScenarioPluginError`。

## 验证

- 契约字段验证和不可变性测试。
- 轴承注册、能力查询、重复 ID、缺失能力和无效插件测试。
- 架构测试保证 `core` 不导入轴承插件，并确认生产装配集中在 `bootstrap/scenarios.py`。
- 新增测试通过后运行原全量严格测试，结果必须不低于阶段0基线且不能出现未知失败。

## 非目标

不切换边缘、云端、调度或存储入口；不移动实现；不修改模型、算法、阈值、API、数据库、MQTT、启动参数或旧默认轴承场景兼容逻辑。
