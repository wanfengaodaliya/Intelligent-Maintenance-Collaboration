# 阶段2：边缘推理解耦设计

## 范围与选择

采用适配器方案：轴承插件新增 `BearingEdgeInferenceProvider` 和 `BearingEdgeModelProvider`，内部继续调用既有 `LocalH5ModelClient`、模型仓初始化和旧同步推理函数。边缘应用通过阶段1注册表取得能力，不重写模型或 V1.2 状态机。

未采用直接重写通用推理流水线的方案，因为会同时改变队列、降级与模型调用；也未采用仅移动导入的方案，因为入口仍会直接认识 H5 类型和窗口约束。

## 组件

- 通用 `EdgeInferenceRuntimeRequest`：传递模型目录、版本 pin、窗口大小和生命周期开关。
- 通用 `EdgeInferenceMetadata`：作为后端、默认版本、特征版本和部署状态的唯一元数据来源。
- 通用 `EdgeInferenceRuntime`：返回流水线后端、符合结构协议的模型客户端和证据构造器。
- `BearingEdgeModelProvider`：声明 H5 模型版本、模型类型和固定 50ms 窗口，按原逻辑创建并检查客户端。
- `BearingEdgeInferenceProvider`：构建轴承边缘运行时，并代理旧 `/edge/infer` 同步合同。
- `BearingScenarioPlugin`：将 `edge_inference` 与 `model_provider` 绑定为可执行 Provider，其他阶段能力保持未解析。
- `edge_service/app.py`：只按通用能力和 Provider 返回的元数据装配现有 `EdgeModelPipeline`。

## 行为保持

Provider 原样复用模型仓选择、readiness、证据构建和旧同步推理函数。50ms校验错误文本保持不变。`official` HTTP 对照路线保持现状。`v12_flow.py` 不修改。

## 错误处理

- 场景或能力缺失沿用注册表明确错误。
- 非50ms轴承窗口仍在启动时抛出原 `ValueError`。
- H5加载或版本不匹配仍返回原 readiness 细节并阻止启动。
- 模型推理异常仍由现有客户端映射为 `MODEL_INFERENCE_FAILED`，继续进入原降级路径。

## 验证

- 注册表返回可执行轴承边缘和模型 Provider。
- Provider构建参数、50ms校验及旧同步接口与原实现一致。
- 架构测试禁止 `edge_service/app.py` 直接导入 H5客户端、H5版本或轴承插件。
- 运行 H5固定探针、local_h5路线、V1.2流程、弱网与失败回归，再运行全量严格测试。

## 非目标

不修改3200点输入、64kHz到16kHz重采样、800点张量、19维物理特征、13维工况特征、0.25缩放、标签顺序、权重、Softmax、风险动作映射、调度与仲裁规则、API、数据库、MQTT或启动端口。
