# 统一云端服务设计方案

## 目标

维护一套统一的 `cloud_service` 代码：本地开发时无需 GPU，使用模拟后端；部署到 AutoDL 后使用真实的 Qwen 模型。调度器始终使用同一种请求格式，仅根据运行环境切换云端服务地址。

## 实施范围

本次修改升级现有的 `cloud_edge_project/cloud_service`，不另外创建第二套独立的服务器应用，不修改调度器的路由策略，不修改边缘模型，不上传模型权重，也不包含模型微调。

## 总体架构

FastAPI 应用继续提供 `POST /cloud/infer` 和 `GET /health`。服务层读取环境变量 `CLOUD_BACKEND`，并且只选择一个推理后端：

- `mock`：默认值，在没有 vLLM 和 GPU 的本地环境中生成确定性的模拟结果。
- `vllm`：调用 vLLM 提供的 OpenAI 兼容聊天接口，将模型返回的 JSON 转换为项目现有的 `CloudResult` 格式。

本地负责开发代码并提交到 GitHub，AutoDL 从 GitHub 拉取同一套代码。后端选择由运行配置决定，而不是维护不同源代码。

```text
本地调度器 -> 本地 /cloud/infer -> mock 后端

团队调度器 -> AutoDL /cloud/infer -> vLLM 后端
                                      -> 127.0.0.1:6006
                                      -> Qwen3-14B-AWQ
```

## 文件及职责

```text
cloud_edge_project/
├── cloud_service/
│   ├── app.py              FastAPI 路由和 HTTP 错误转换
│   ├── model.py            保持现有调用方式稳定的推理入口
│   ├── service.py          后端选择和 CloudResult 组装
│   ├── mock_backend.py     本地确定性模拟推理
│   ├── vllm_backend.py     vLLM HTTP 客户端及响应解析
│   └── prompt.py           云端复核系统提示词和输入构造
├── scripts/
│   ├── start_vllm.sh       在 AutoDL 启动 vLLM 和 Qwen
│   └── start_cloud_service.sh  在 AutoDL 选择 vLLM 并启动 FastAPI
├── configs/
│   └── local.yaml          现有的本地服务默认配置
└── tests/
    └── test_cloud_service.py
```

保留 `model.py` 作为兼容入口，确保现有 `app.py` 和项目其他模块的导入方式不会失效。不同后端的具体实现移动到职责单一的独立模块中。

## 对外接口

请求继续使用 `docs/api.md` 中已有的 `CloudRequest` 格式：

```json
{
  "packet": {
    "packet_id": "batch_000001",
    "device_id": "K001",
    "sensor_id": "sensor_K001",
    "sequence_number": 1,
    "start_timestamp_ns": 1781920800000000000,
    "end_timestamp_ns": 1781920800050000000,
    "duration_ms": 50,
    "data": {
      "data_type": "bearing_timeseries",
      "vibration_sample_rate_hz": 16000,
      "vibration_sample_count": 800,
      "vibration": [],
      "current": 1.34,
      "temperature": 45.8,
      "speed": 899.7,
      "load": 0.7
    }
  },
  "edge_result": {
    "label": "abnormal",
    "confidence": 0.72,
    "risk_level": "medium"
  }
}
```

上例中的 `vibration` 空数组仅用于简化展示数据结构，不是合法的正式请求。正式请求仍必须按照现有接口约定提供 800 个数值采样点，并由 `common.schemas.validate_cloud_request` 完整校验。

两个后端都返回项目现有的 `CloudResult` 格式：

```json
{
  "packet_id": "batch_000001",
  "device_id": "K001",
  "cloud_node_id": "cloud_1",
  "model_name": "qwen-cloud",
  "label": "abnormal",
  "confidence": 0.93,
  "risk_level": "high",
  "cloud_latency_ms": 852.4,
  "decision": {
    "action": "send_alert",
    "description": "云端复核确认轴承处于异常状态。"
  }
}
```

模型生成的字段值必须限制在现有接口允许的范围内：

- `label`：`normal` 或 `abnormal`
- `risk_level`：`low`、`medium` 或 `high`
- `decision.action`：`none`、`record_only`、`send_alert` 或 `stop_machine_check`
- `confidence`：0 到 1 之间的数值

## 提示词和模型输入

系统提示词将模型定义为边缘—云协同智能维护系统中的云端复核模型，要求只返回一个 JSON 对象，其中必须包含 `label`、`confidence`、`risk_level`、`action` 和 `description`。提示词禁止模型编造测量数据，并要求在信息不足时采用保守判断。

用户消息通过 `json.dumps(..., ensure_ascii=False)` 序列化。输入包含数据包标识、标量传感器数据、边缘模型结果，以及从 800 个振动采样点计算出的紧凑统计特征。不会把原始的 800 个振动数值直接塞入语言模型提示词，因为这会大量占用上下文，而且纯文本语言模型无法可靠地直接完成时序信号处理。

振动统计摘要包括采样数量、最小值、最大值、平均值、均方根值和绝对峰值。在生成统计摘要之前，原始请求仍会执行完整的接口校验。

## 配置方式

运行配置使用环境变量，并提供适合本地开发的安全默认值：

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `CLOUD_BACKEND` | `mock` | 选择 `mock` 或 `vllm` 后端 |
| `VLLM_URL` | `http://127.0.0.1:6006/v1/chat/completions` | vLLM 内部接口地址 |
| `VLLM_MODEL_NAME` | `qwen-cloud` | 与 `--served-model-name` 一致的模型名 |
| `VLLM_API_KEY` | 空 | 可选鉴权密钥，禁止提交到仓库 |
| `VLLM_TIMEOUT_SECONDS` | `120` | vLLM 请求超时时间，单位为秒 |

如果 `CLOUD_BACKEND` 使用不支持的值，服务必须明确报错，不能悄悄回退到 mock。

调度器使用另一个环境变量 `CLOUD_SERVICE_URL` 选择调用哪个云端服务实例：

- 本地开发：`http://127.0.0.1:8004`
- 真实联调：AutoDL 为 6008 端口提供的公网映射地址

调度器不检测 GPU，也不负责选择推理后端。

## 启动和部署

`start_vllm.sh` 激活 `cloud_llm` Conda 环境，通过 vLLM 启动 Qwen3-14B-AWQ，并监听 6006 端口。

`start_cloud_service.sh` 激活同一环境，设置 `CLOUD_BACKEND=vllm`，进入从 GitHub 拉取的项目目录，然后在 6008 端口启动 FastAPI 服务。其他 vLLM 配置使用代码中的默认值，除非运维人员明确覆盖。

AutoDL 启动顺序：

1. 执行 `start_vllm.sh`，等待 vLLM 健康接口正常响应。
2. 执行 `start_cloud_service.sh`。
3. 通过 6008 端口测试 `GET /health` 和 `POST /cloud/infer`。

模型权重继续放在 `/root/autodl-tmp/models/Qwen3-14B-AWQ`，禁止提交到 GitHub。AutoDL 从 GitHub 拉取应用源代码，不再单独手工维护另一份云服务代码。

## 错误处理

现有请求字段错误继续返回 HTTP 400，并使用项目统一错误格式。新增错误的处理方式如下：

- 后端配置值不受支持：HTTP 500，错误码 `MODEL_INFER_FAILED`
- vLLM 连接失败或超时：HTTP 503，错误码 `CLOUD_UNAVAILABLE`
- vLLM 返回非成功 HTTP 状态：HTTP 503，错误码 `CLOUD_UNAVAILABLE`
- 模型返回空内容、非法 JSON、Markdown 代码围栏或不符合字段约定的 JSON：HTTP 502，错误码 `MODEL_INFER_FAILED`

如果能够从请求中解析出 `packet_id`，错误响应必须保留它。返回给客户端的错误消息不得包含密钥或完整模型响应。

`GET /health` 返回当前选择的后端。mock 模式直接报告可用；vLLM 模式使用较短超时时间检查配置的 vLLM 模型接口，当模型服务无法连接时报告不可用。

## 测试方案

所有自动化测试都能在没有 GPU 的本地环境运行，覆盖：

1. 未配置环境变量时默认选择 `mock`。
2. mock 推理保留数据包和设备编号，并产生合法的 `CloudResult`。
3. 选择 `vllm` 时，请求包含正确的模型名、系统提示词、紧凑传感器数据和超时时间。
4. 合法的模型 JSON 能转换为现有 `CloudResult`。
5. 拒绝 Markdown 代码围栏包裹的 JSON，避免模糊解析。
6. 非法标签、风险等级、置信度、处理动作、空响应、超时和连接错误能转换为规定的错误。
7. 不支持的后端配置必须失败，且不能调用 mock。
8. FastAPI 健康检查和推理路由返回正确的 HTTP 状态码。

项目现有演示在默认 mock 模式下仍能运行。测试不依赖 AutoDL、vLLM、模型权重或外部网络。

## 验收标准

- 仓库中只维护一套 `cloud_service`，能够在本地和 AutoDL 运行。
- 本地未配置后端时默认使用 mock，不需要 GPU。
- AutoDL 启动脚本设置 `CLOUD_BACKEND=vllm`，调用同一服务器上的 vLLM。
- 两种模式接收相同的 `CloudRequest`，返回相同的 `CloudResult`。
- 调度器在本地和真实联调之间只切换 `CLOUD_SERVICE_URL`。
- 自动化测试在没有 GPU 和网络的环境中全部通过。
- 模型权重、访问密钥、`local_word` 和运行日志均不纳入 Git 管理。
