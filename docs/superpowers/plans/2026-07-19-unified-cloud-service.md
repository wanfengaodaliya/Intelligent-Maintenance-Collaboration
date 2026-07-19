# 统一云端服务实施计划

> **供智能代理执行：** 必须使用 `superpowers:executing-plans`，按复选框逐项实施。本计划采用测试驱动开发，先写失败测试，再完成最小实现。

**目标：** 将现有云端 mock 服务升级为一套可通过环境变量切换 `mock` 和 `vllm` 的统一服务，并让调度流程只通过 `CLOUD_SERVICE_URL` 切换本地或 AutoDL 地址。

**架构：** `cloud_service.service` 负责请求校验和后端选择；`mock_backend` 保留本地无 GPU 演示；`vllm_backend` 调用 AutoDL 本机的 OpenAI 兼容接口。两个后端输出相同的 `CloudResult`，`model.py` 保留为兼容入口。

**技术栈：** Python 3.11、FastAPI、requests、unittest、Bash、vLLM OpenAI 兼容接口。

## 全局约束

- 只升级现有 `cloud_edge_project/cloud_service`，不创建第二套独立云服务。
- 未设置 `CLOUD_BACKEND` 时必须默认使用 `mock`。
- `CLOUD_BACKEND=vllm` 时必须调用 `VLLM_URL`，不能静默回退 mock。
- 两种后端必须继续接受 `docs/api.md` 定义的 `packet + edge_result` 请求。
- 两种后端必须返回相同的 `CloudResult` 字段。
- 模型权重、密钥、`local_word` 和运行日志禁止纳入 Git。
- 所有测试必须在没有 GPU、vLLM 和网络的环境中运行。

---

### 任务一：增加云端运行配置和提示词输入构造

**文件：**
- 创建：`cloud_edge_project/cloud_service/config.py`
- 创建：`cloud_edge_project/cloud_service/prompt.py`
- 创建：`cloud_edge_project/tests/__init__.py`
- 创建：`cloud_edge_project/tests/test_cloud_prompt.py`

**接口：**
- 产出：`CloudSettings`、`load_cloud_settings()`、`build_cloud_messages(request)`。
- 供后续任务使用：后端选择、vLLM地址、模型名、鉴权和超时；模型消息构造。

- [ ] **步骤1：编写配置和提示词失败测试**

测试必须验证：默认后端为 `mock`；环境变量可以覆盖五项设置；800点振动序列被转换为 count、min、max、mean、rms 和 peak_abs；用户消息是合法中文 JSON，且不包含原始振动数组。

```python
class CloudPromptTests(unittest.TestCase):
    def test_default_settings_use_mock(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = load_cloud_settings()
        self.assertEqual(settings.backend, "mock")
        self.assertEqual(settings.vllm_model_name, "qwen-cloud")

    def test_build_messages_compacts_vibration(self):
        request = make_cloud_request([1.0, -1.0] * 400)
        messages = build_cloud_messages(request)
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["vibration_summary"]["count"], 800)
        self.assertEqual(payload["vibration_summary"]["rms"], 1.0)
        self.assertNotIn("vibration", payload["sensor_data"])
```

- [ ] **步骤2：运行测试并确认失败**

运行：`cd cloud_edge_project && python -m unittest tests.test_cloud_prompt -v`

预期：因 `cloud_service.config` 或 `cloud_service.prompt` 不存在而失败。

- [ ] **步骤3：实现配置和提示词模块**

`config.py` 使用不可变 dataclass：

```python
@dataclass(frozen=True)
class CloudSettings:
    backend: str
    vllm_url: str
    vllm_model_name: str
    vllm_api_key: str
    vllm_timeout_seconds: float


def load_cloud_settings() -> CloudSettings:
    return CloudSettings(
        backend=os.getenv("CLOUD_BACKEND", "mock").strip().lower(),
        vllm_url=os.getenv(
            "VLLM_URL",
            "http://127.0.0.1:6006/v1/chat/completions",
        ).strip(),
        vllm_model_name=os.getenv("VLLM_MODEL_NAME", "qwen-cloud").strip(),
        vllm_api_key=os.getenv("VLLM_API_KEY", "").strip(),
        vllm_timeout_seconds=float(os.getenv("VLLM_TIMEOUT_SECONDS", "120")),
    )
```

`prompt.py` 定义中文 `CLOUD_SYSTEM_PROMPT`，并实现：

```python
def summarize_vibration(values: list[float]) -> dict[str, float | int]:
    count = len(values)
    mean = sum(values) / count
    return {
        "count": count,
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "mean": round(mean, 6),
        "rms": round(math.sqrt(sum(value * value for value in values) / count), 6),
        "peak_abs": round(max(abs(value) for value in values), 6),
    }


def build_cloud_messages(request: dict[str, Any]) -> list[dict[str, str]]:
    packet = request["packet"]
    data = packet["data"]
    user_payload = {
        "packet": {
            "packet_id": packet["packet_id"],
            "device_id": packet["device_id"],
            "sensor_id": packet["sensor_id"],
        },
        "sensor_data": {
            "current": data["current"],
            "temperature": data["temperature"],
            "speed": data["speed"],
            "load": data["load"],
        },
        "vibration_summary": summarize_vibration(data["vibration"]),
        "edge_result": request["edge_result"],
    }
    return [
        {"role": "system", "content": CLOUD_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
```

- [ ] **步骤4：运行测试并确认通过**

运行：`cd cloud_edge_project && python -m unittest tests.test_cloud_prompt -v`

预期：全部通过。

- [ ] **步骤5：提交任务一**

```bash
git add cloud_edge_project/cloud_service/config.py cloud_edge_project/cloud_service/prompt.py cloud_edge_project/tests
git commit -m "feat: add cloud model configuration and prompt"
```

---

### 任务二：拆分 mock 后端并增加统一选择层

**文件：**
- 创建：`cloud_edge_project/cloud_service/mock_backend.py`
- 创建：`cloud_edge_project/cloud_service/errors.py`
- 创建：`cloud_edge_project/cloud_service/service.py`
- 修改：`cloud_edge_project/cloud_service/model.py`
- 创建：`cloud_edge_project/tests/test_cloud_service.py`

**接口：**
- 产出：`infer_mock(validated_request)`、`infer_cloud(request, settings=None)`、`CloudServiceError`。
- 保持：`from cloud_service.model import infer_cloud` 继续可用。

- [ ] **步骤1：编写 mock 和后端选择失败测试**

```python
class CloudServiceTests(unittest.TestCase):
    def test_default_backend_uses_mock(self):
        request = make_valid_cloud_request()
        with patch.dict(os.environ, {}, clear=True):
            result = infer_cloud(request)
        self.assertEqual(result["packet_id"], request["packet"]["packet_id"])
        self.assertEqual(result["model_name"], "cloud_bearing_mock")

    def test_unknown_backend_does_not_fall_back(self):
        settings = CloudSettings("unknown", "", "", "", 120)
        with self.assertRaisesRegex(CloudServiceError, "unsupported cloud backend"):
            infer_cloud(make_valid_cloud_request(), settings=settings)
```

- [ ] **步骤2：运行测试并确认失败**

运行：`cd cloud_edge_project && python -m unittest tests.test_cloud_service -v`

预期：因新模块或 `settings` 参数不存在而失败。

- [ ] **步骤3：实现 mock 后端和统一服务层**

把现有 `model.py` 的规则实现移动到 `mock_backend.py`：

```python
def infer_mock(validated_request: dict[str, Any]) -> dict[str, Any]:
    start = perf_counter()
    packet = validated_request["packet"]
    edge_result = validated_request["edge_result"]
    edge_confidence = require_confidence(
        edge_result["confidence"],
        "edge_result.confidence",
        packet["packet_id"],
    )
    label = edge_result["label"]

    if label == "abnormal":
        confidence = max(0.9, min(edge_confidence + 0.21, 0.97))
        risk_level = "high"
        action = "send_alert"
        description = "bearing anomaly risk is high; schedule inspection"
    else:
        confidence = max(0.9, min(edge_confidence + 0.08, 0.97))
        risk_level = "low"
        action = "record_only"
        description = "bearing state is normal; keep monitoring"

    elapsed_ms = max((perf_counter() - start) * 1000, 12.0)
    return {
        "packet_id": packet["packet_id"],
        "device_id": packet["device_id"],
        "cloud_node_id": CLOUD_NODE_ID,
        "model_name": MODEL_NAME,
        "label": label,
        "confidence": round(confidence, 2),
        "risk_level": risk_level,
        "cloud_latency_ms": round(elapsed_ms + 74.0, 2),
        "decision": {"action": action, "description": description},
    }
```

`errors.py` 定义服务层和后端共同使用的异常，避免模块循环导入：

```python
class CloudServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
```

`service.py` 只负责校验和选择：

```python
def infer_cloud(
    request: dict[str, Any],
    settings: CloudSettings | None = None,
) -> dict[str, Any]:
    validated = validate_cloud_request(request)
    selected = settings or load_cloud_settings()
    if selected.backend == "mock":
        return infer_mock(validated)
    if selected.backend == "vllm":
        return infer_vllm(validated, selected)
    raise CloudServiceError(
        "MODEL_INFER_FAILED",
        f"unsupported cloud backend: {selected.backend}",
        500,
    )
```

`model.py` 改为兼容转发：

```python
from cloud_service.mock_backend import CLOUD_NODE_ID
from cloud_service.service import infer_cloud

__all__ = ["CLOUD_NODE_ID", "infer_cloud"]
```

- [ ] **步骤4：运行 mock 选择测试和现有直接演示**

运行：

```bash
cd cloud_edge_project
python -m unittest tests.test_cloud_service -v
python quick_demo.py --kind abnormal
```

预期：测试通过，演示仍能输出 mock 云端结果并完成日志流程。

- [ ] **步骤5：提交任务二**

```bash
git add cloud_edge_project/cloud_service cloud_edge_project/tests/test_cloud_service.py
git commit -m "refactor: add switchable cloud inference service"
```

---

### 任务三：实现 vLLM 后端和严格响应解析

**文件：**
- 创建：`cloud_edge_project/cloud_service/vllm_backend.py`
- 修改：`cloud_edge_project/tests/test_cloud_service.py`

**接口：**
- 产出：`infer_vllm(validated_request, settings)`。
- 依赖：任务一的 `build_cloud_messages` 和 `CloudSettings`；任务二 `errors.py` 中的 `CloudServiceError`。

- [ ] **步骤1：编写 vLLM 请求、解析和异常失败测试**

测试使用 `unittest.mock.patch("cloud_service.vllm_backend.requests.post")`，覆盖：合法 JSON；Authorization 请求头；请求超时；连接失败；HTTP 500；空回答；Markdown 代码围栏；非法字段值。

```python
def test_vllm_backend_returns_cloud_result(self):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({
            "label": "abnormal",
            "confidence": 0.94,
            "risk_level": "high",
            "action": "send_alert",
            "description": "云端确认异常",
        }, ensure_ascii=False)}}]
    }
    with patch("cloud_service.vllm_backend.requests.post", return_value=response):
        result = infer_cloud(make_valid_cloud_request(), settings=vllm_settings())
    self.assertEqual(result["model_name"], "qwen-cloud")
    self.assertEqual(result["decision"]["action"], "send_alert")
```

- [ ] **步骤2：运行测试并确认失败**

运行：`cd cloud_edge_project && python -m unittest tests.test_cloud_service -v`

预期：因 `vllm_backend.py` 不存在或尚未实现而失败。

- [ ] **步骤3：实现 vLLM 调用和严格校验**

请求体必须包含 `model`、`messages`、`temperature=0.1` 和 `max_tokens=512`。仅当密钥非空时添加值为 `Bearer <VLLM_API_KEY>` 的 `Authorization` 请求头。

解析逻辑必须使用 `json.loads(content)`，拒绝以三个反引号开头的内容，并验证：

```python
ALLOWED_LABELS = {"normal", "abnormal"}
ALLOWED_RISK_LEVELS = {"low", "medium", "high"}
ALLOWED_ACTIONS = {"none", "record_only", "send_alert", "stop_machine_check"}
```

成功时使用 `perf_counter()` 计算真实调用耗时，并组装完整 `CloudResult`。`requests.Timeout` 和 `requests.ConnectionError` 转换为状态码503的 `CLOUD_UNAVAILABLE`；响应或JSON错误转换为状态码502的 `MODEL_INFER_FAILED`。

- [ ] **步骤4：运行测试并确认通过**

运行：`cd cloud_edge_project && python -m unittest tests.test_cloud_service -v`

预期：所有 mock 和 vLLM 测试通过，且测试期间没有真实网络请求。

- [ ] **步骤5：提交任务三**

```bash
git add cloud_edge_project/cloud_service/vllm_backend.py cloud_edge_project/tests/test_cloud_service.py
git commit -m "feat: add vllm cloud inference backend"
```

---

### 任务四：完善 FastAPI 错误映射、健康检查和云服务地址切换

**文件：**
- 修改：`cloud_edge_project/cloud_service/app.py`
- 修改：`cloud_edge_project/common/config.py`
- 创建：`cloud_edge_project/tests/test_cloud_app.py`
- 创建：`cloud_edge_project/tests/test_config.py`

**接口：**
- 产出：健康检查显示当前后端；模型不可用返回统一状态码；`service_url("cloud")` 支持 `CLOUD_SERVICE_URL`。

- [ ] **步骤1：编写失败测试**

测试直接调用路由函数，验证：mock健康检查；vLLM不可用健康检查；`CloudServiceError` 到 `JSONResponse` 的状态码和统一错误体；`CLOUD_SERVICE_URL` 去除尾部斜杠后覆盖 YAML 地址。

```python
def test_cloud_service_url_env_override(self):
    with patch.dict(os.environ, {"CLOUD_SERVICE_URL": "https://example.test/"}):
        self.assertEqual(service_url("cloud"), "https://example.test")
```

- [ ] **步骤2：运行测试并确认失败**

运行：

```bash
cd cloud_edge_project
python -m unittest tests.test_cloud_app tests.test_config -v
```

预期：环境变量覆盖或新错误映射断言失败。

- [ ] **步骤3：实现路由和地址覆盖**

`common.config.service_url` 在读取 YAML 前处理：

```python
if service == "cloud":
    override = os.getenv("CLOUD_SERVICE_URL", "").strip()
    if override:
        return override.rstrip("/")
```

`app.py` 捕获 `CloudServiceError`，使用其 `status_code` 和项目现有 `error_response` 返回 JSON。健康检查返回 `service`、`node_id`、`status`、`port` 和实际 `model_backend`；vLLM模式通过派生的 `/v1/models` 地址进行短超时检查。

- [ ] **步骤4：运行相关测试和完整测试集**

运行：

```bash
cd cloud_edge_project
python -m unittest discover -s tests -v
```

预期：所有测试通过，无网络请求。

- [ ] **步骤5：提交任务四**

```bash
git add cloud_edge_project/cloud_service/app.py cloud_edge_project/common/config.py cloud_edge_project/tests
git commit -m "feat: configure cloud service endpoints and health"
```

---

### 任务五：增加 AutoDL 启动脚本和中文部署说明

**文件：**
- 创建：`cloud_edge_project/scripts/start_vllm.sh`
- 创建：`cloud_edge_project/scripts/start_cloud_service.sh`
- 创建：`cloud_edge_project/docs/autodl-cloud-service.md`
- 修改：`cloud_edge_project/requirements.txt`

**接口：**
- 产出：可从仓库直接运行的两个启动脚本和完整部署步骤。

- [ ] **步骤1：编写脚本静态测试**

在 `tests/test_deployment_scripts.py` 中读取脚本文本，验证模型路径、环境名、端口、`CLOUD_BACKEND=vllm` 和相对项目目录切换均存在；验证脚本不包含真实密钥。

- [ ] **步骤2：运行测试并确认失败**

运行：`cd cloud_edge_project && python -m unittest tests.test_deployment_scripts -v`

预期：因脚本尚不存在而失败。

- [ ] **步骤3：创建脚本和说明文档**

`start_vllm.sh`：

```bash
#!/bin/bash
set -e

source /root/miniconda3/bin/activate cloud_llm

MODEL_PATH="${MODEL_PATH:-/root/autodl-tmp/models/Qwen3-14B-AWQ}"

exec vllm serve "${MODEL_PATH}" \
    --host 127.0.0.1 \
    --port 6006 \
    --served-model-name qwen-cloud \
    --max-model-len 4096 \
    --gpu-memory-utilization 0.9
```

`start_cloud_service.sh`：

```bash
#!/bin/bash
set -e

source /root/miniconda3/bin/activate cloud_llm
export CLOUD_BACKEND="vllm"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

exec python -m uvicorn cloud_service.app:app \
    --host 0.0.0.0 \
    --port 6008
```

中文说明文档必须写清：GitHub拉取路径、模型权重位置、安装依赖、两个终端的启动顺序、服务器内部curl测试、AutoDL公网映射、环境变量和更新命令。`requirements.txt` 保持 `fastapi`、`uvicorn`、`requests`，不添加模型权重或vLLM锁定项。

- [ ] **步骤4：运行脚本静态测试和完整回归**

运行：

```bash
cd cloud_edge_project
python -m unittest discover -s tests -v
python quick_demo.py --kind abnormal
```

预期：所有测试通过；本地演示仍使用mock完成。

- [ ] **步骤5：提交任务五**

```bash
git add cloud_edge_project/scripts cloud_edge_project/docs/autodl-cloud-service.md cloud_edge_project/requirements.txt cloud_edge_project/tests/test_deployment_scripts.py
git commit -m "docs: add autodl cloud service deployment"
```

---

### 任务六：最终验证和发布准备

**文件：**
- 检查：本计划涉及的全部文件
- 检查：`.gitignore` 和 `.git/info/exclude`

- [ ] **步骤1：运行完整自动化测试**

运行：`cd cloud_edge_project && python -m unittest discover -s tests -v`

预期：0 failures，0 errors。

- [ ] **步骤2：运行本地端到端mock演示**

运行：`cd cloud_edge_project && python quick_demo.py --kind abnormal`

预期：调度路径为 cloud 时返回 `cloud_bearing_mock`，任务日志成功保存。

- [ ] **步骤3：执行语法和变更检查**

运行：

```bash
python -m compileall -q cloud_edge_project
git diff --check origin/main..HEAD
git status --short --branch
```

预期：编译和差异检查退出码为0；只出现计划内提交，本地资料与模型权重不在状态中。

- [ ] **步骤4：核对提交历史和待推送范围**

运行：`git log --oneline origin/main..HEAD`

预期：包含中文版设计、实施计划和各任务的本地提交，不包含私人文件、密钥或模型权重。

- [ ] **步骤5：按用户要求推送**

只有全部验证通过后，使用发布流程将当前 `main` 推送到 `origin/main`；禁止强制推送。
