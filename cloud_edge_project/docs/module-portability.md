# 网络与边缘感知模块可迁移化说明

> 当前启动和运行以仓库根目录的 `start_project.ps1` 为准。本文早期 V0.1 章节中提到的
> Consistency、Log 和 `start_all.py` 已从当前代码移除，不再作为迁移或部署依赖。

## 改造边界

- 边缘运行时只依赖 `PerceptionHandler` 通用协议。
- 轴承感知由 `scenarios/bearing/edge` 在入口层装配。
- 原 `edge_perception.EdgePerception` 和处理逻辑保持不变，兼容已有调用。
- 网络模拟器可从任意工作目录启动，并可安装为独立 Python 命令。
- 网络节点、链路、上游地址和运行目录均通过外部配置或环境变量提供。

## 边缘感知迁移

目标机器复制整个 `cloud_edge_project`，安装原项目依赖后，在启动边缘服务前设置：

```powershell
$env:EDGE_NODE_ID = "edge_01"
$env:EDGE_SCENARIO_TYPE = "bearing"
$env:EDGE_PERCEPTION_FIR_PATH = "D:\edge-config\fir_64k_to_16k_369.txt"
$env:EDGE_PERCEPTION_FIR_SHA256 = "<文件SHA-256>"
$env:EDGE_NETWORK_STATUS_URL = "http://network-host:8090/api/v1/network/links/edge_01__to__scheduler__http"
```

可选参数：

| 环境变量 | 默认值 |
|---|---:|
| `EDGE_PERCEPTION_PROFILE` | `development_test` |
| `EDGE_PERCEPTION_CONFIG_SOURCE` | 与 profile 相同 |
| `EDGE_PERCEPTION_CONFIG_VERSION` | `runtime-v1` |
| `EDGE_PERCEPTION_RUNNING_SPEED_THRESHOLD_RPM` | `100.0` |
| `EDGE_PERCEPTION_CONSTANT_THRESHOLD` | `1e-9` |
| `EDGE_PERCEPTION_FEATURE_ZERO_RMS_THRESHOLD` | `1e-10` |
| `EDGE_PERCEPTION_FEATURE_ZERO_POWER_THRESHOLD` | RMS阈值平方 |
| `EDGE_PERCEPTION_CURRENT_ZERO_RMS_THRESHOLD` | `1e-10` |

不设置这些变量时，运行结果与改造前保持一致。

### 场景选择

通过 `EDGE_SCENARIO_TYPE` 环境变量选择场景（默认 `bearing`），入口层在 `run_edge_service.py` 中
根据场景类型注册对应的 `PerceptionHandler`。

### 兼容性

- 旧导入路径 `from edge_perception` 继续有效，通过兼容层转发。
- 所有 `EDGE_*` 环境变量名与改造前保持一致。

状态上报器默认根据 `EDGE_NODE_ID` 生成网络链路 ID。也可以设置
`EDGE_NETWORK_LINK_ID` 只替换链路 ID，或使用 `EDGE_NETWORK_STATUS_URL`
覆盖完整地址。调度器和云端上报地址继续分别由
`EDGE_STATUS_SCHEDULER_URL`、`EDGE_STATUS_CLOUD_URL` 配置。

从任意工作目录启动边缘服务：

```powershell
python D:\modules\cloud_edge_project\edge_service\run_edge_service.py
```

只检查迁移后的模块能否完成装配：

```powershell
python D:\modules\cloud_edge_project\edge_service\run_edge_service.py --check-import
```

## 网络模块迁移

### 源码方式

```powershell
python D:\modules\network_simulator\run_network_simulator.py --config-dir D:\network-config --log-dir D:\network-logs
```

仅验证迁移后的配置：

```powershell
python D:\modules\network_simulator\run_network_simulator.py --config-dir D:\network-config --check-config
```

### 独立安装方式

```powershell
pip install cloud_edge_project\internet_service\network_simulator
network-simulator --config-dir D:\network-config --log-dir D:\network-logs
```

迁移时复制 `network_simulator/config` 为独立配置目录，再修改：

- `entities.yaml`：发送器、边缘节点和 MQTT 上游；
- `links.yaml`：HTTP 链路、代理监听端口和上游；
- `reporter.yaml`：调度器网络状态接收地址；
- 其余 YAML：网络状态、评分、插件和实验参数。

Docker Compose 使用外部配置目录：

```powershell
$env:NETWORK_CONFIG_HOST_DIR = "D:\network-config"
docker compose up --build
```

上面的默认 Compose 仅用于当前固定的本机演示端口。Linux 虚拟机需要按
`entities.yaml`、`links.yaml` 动态增加代理端口时，使用 host network 版本：

```bash
NETWORK_CONFIG_HOST_DIR=/opt/cloud-edge/network-config \
NETWORK_MOSQUITTO_CONFIG_PATH=/opt/cloud-edge/network-config/mosquitto.conf \
docker compose -f docker-compose.vm.yml up --build
```

虚拟机配置中的 MQTT `broker_upstream` 应填写虚拟机实际可达地址，例如
`127.0.0.1:1883`，`mqtt_proxy_host` 填写业务进程可访问的虚拟机 IP 或主机名；
HTTP `upstream` 填写调度器、云端和边缘节点的实际 IP。

> **安全要求：** `docker-compose.vm.yml` 使用 host network，只允许部署在隔离、
> 可信的实验网络。启动前必须通过虚拟机防火墙限制 `8474`、`8090`、`1883`
> 以及全部代理端口的来源地址；`8474` 仅允许网络控制器所在主机访问，
> `8090` 仅允许边缘节点访问，MQTT 和代理端口仅允许已登记业务节点访问。
> 当前 Toxiproxy API、Network API 和匿名 MQTT 均无鉴权，禁止暴露到公网。
> VM 专用 `plugins.yaml`、`mosquitto.conf` 应按实际网卡地址收紧监听范围。

也可用 `NETWORK_LINK_UPSTREAMS_JSON` 在不修改 YAML 的情况下覆盖显式 HTTP 链路上游。

## 默认端口

| 组件 | 默认端口 |
|---|---:|
| Sender 模块 | 无默认 HTTP 监听端口 |
| Edge 服务 | `8001` |
| Scheduler 服务 | `8003` |
| Cloud 服务 | `8004` |
| Edge 控制接口 | `8011` |
| MQTT Broker | `1883` |
| Network API | `8090` |
| Toxiproxy API | `8474` |
| Network Fake Scheduler（仅本地演示） | `8000` |

Reporter 本地默认代理端口：

| 节点 | Scheduler 上报代理 | Cloud 上报代理 |
|---|---:|---:|
| `edge_01` | `18011` | `18021` |
| `edge_02` | `18051` | `18053` |

代理链路端口由 `entities.yaml` 和 `links.yaml` 决定，不应在业务代码中写死。

## 通用配置与路径解析

所有模块遵循统一的路径解析规则：

- 默认项目根目录从 `Path(__file__)` 推导，不依赖启动时的当前工作目录（CWD）。
- 数据库、日志、缓存、模型、临时文件和运行目录均支持通过环境变量外部覆盖。
- 相对路径相对于项目根目录解析，并在内部转换为绝对路径。
- 未设置环境变量时，所有默认值与改造前完全一致。

### 环境变量覆盖示例

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `CLOUD_REVIEW_DB_PATH` | `data/cloud_review.db` | 云端评审数据库路径 |
| `CLOUD_MOMENT_CHECKPOINT_PATH` | `model_assets/moment/releases/moment-scl05-final/best_model.pt` | MOMENT 模型检查点 |
| `CLOUD_MOMENT_CONDITION_NORM_PATH` | `model_assets/moment/releases/moment-scl05-final/condition_norm.json` | 条件归一化参数 |
| `CLOUD_MOMENT_PRETRAINED_PATH` | `model_assets/moment/pretrained/MOMENT-1-small` | 预训练模型路径 |
| `CLOUD_MOMENT_DEPLOYMENT_DIR` | `model_assets/moment/releases/moment-scl05-final` | 部署目录 |
| `CLOUD_MOMENT_DEVICE` | `auto` | 推理设备 |
| `CLOUD_BACKEND` | `mock` | Cloud 推理后端 |
| `VLLM_URL` | `http://127.0.0.1:6006/v1/chat/completions` | vLLM 服务地址 |
| `VLLM_MODEL_NAME` | `qwen-cloud` | vLLM 模型名称 |
| `VLLM_API_KEY` | `""` | vLLM API 密钥 |

## Cloud 服务迁移

Cloud 服务通过 API 层注入轴承场景分析器，通用服务本身不直接依赖场景。

### 迁移方式

```powershell
set CLOUD_REVIEW_DB_PATH=D:\cloud-data\cloud_review.db
set CLOUD_BACKEND=mock
python -m cloud_service.app
```

### 全局分析解耦

`GlobalAnalysisService` 通过 `scenario_analyzers` 参数接受注入的场景分析器：

```python
from cloud_service.global_analysis.service import GlobalAnalysisService

service = GlobalAnalysisService(
    database_path,
    scenario_analyzers={
        "analyze_bearing_risk": my_bearing_risk_fn,
        "analyze_cloud_bearing_review": my_bearing_review_fn,
        "maintenance_recommendations": my_maintenance_fn,
    },
)
result = service.analyze("bearing", "device_01", 20)
```

不提供分析器时，通用分析结果中不包含轴承专有字段。

### 兼容性

- 旧导入路径 `from cloud_service.bearing_review`、`from cloud_service.enhanced_analysis` 等继续有效。
- 所有 Cloud API 路径、HTTP 方法、状态码和响应结构不变。
- 数据库表结构和已有数据不受影响。

## Sender 模块迁移

### 迁移方式

Sender 模块的默认配置路径基于 `sender_module/sender/__main__.py` 所在位置推导，
不依赖启动目录。

```powershell
python -m sender_module.sender --config D:\sender-config\local.json
```

或通过环境变量：

```powershell
set SENDER_CONFIG_PATH=D:\sender-config\local.json
set SENDER_MQTT_BROKER=tcp://mqtt-host:1883
set SENDER_SCHEDULER_URL=http://scheduler-host:8003
set SENDER_LOG_DIR=D:\sender-logs
set SENDER_STATE_DIR=D:\sender-state
set SENDER_DATA_DIR=D:\sender-data
python -m sender_module.sender
```

### 兼容性

- 不设置环境变量时，使用默认配置 `sender_module/config/local.json`，行为不变。
- 默认 MQTT Broker 地址为 `127.0.0.1:1883`，Scheduler URL 为 `http://127.0.0.1:8003`。
- 现有数据包字段、序列号和发送时序不变。

## Summary 场景服务边界

最新主分支加入的跨边缘汇总、动作评分、冲突请求和维护建议属于轴承场景能力，
实现统一位于 `scenarios/bearing/summary_service`。原有 `summary_service.*` 启动和
导入路径继续有效，但只作为兼容入口，不再拥有轴承算法和规则。云端汇总仲裁合同
与 Edge 快速发布器也通过 `compatibility/bearing_v12` 转发到场景实现。

因此新增其他场景时可以提供自己的汇总服务和合同，无需修改轴承实现；平台已有的
MQTT、重试、Outbox、健康检查和启动编排行为保持不变。

## Scheduler 服务迁移

### 迁移方式

```powershell
set SCHEDULER_DB_PATH=D:\scheduler-data\scheduler.db
set SCHEDULER_CLOUD_URL=http://cloud-host:8004
set SCHEDULER_MQTT_BROKER=tcp://mqtt-host:1883
python -m scheduler.app
```

### 兼容性

- 默认数据库路径为 `data/scheduler.db`（相对项目根目录）。
- 默认 Cloud 地址为 `http://127.0.0.1:8004`，MQTT 地址为 `127.0.0.1:1883`。
- 现有节点控制地址、路由结果、API 和决策结果不变。

## 已移除的 V0.1 服务

早期版本的 Consistency 和 Log 独立服务已移除。当前日志由公共日志组件写入本地日志文件，
一致性处理不再通过独立的 `consistency_service` 进程启动。

## 场景注册机制

`core/scenario_registry.py` 定义了通用场景注册协议，不直接导入任何具体场景：

```python
from core.scenario_registry import register_scenario_handler, get_scenario_handler, list_scenarios

# 入口层注册轴承场景
register_scenario_handler("bearing", BearingCloudHandler())

# 查询已注册场景
handler = get_scenario_handler("bearing")
```

### 新增场景步骤

1. 在 `scenarios/<new_scenario>/` 下实现场景的 Handler 和适配器。
2. 在入口层（如 `cloud_service/app.py`）注册新场景 Handler。
3. 设置 `SCENARIO_TYPE` 或 `EDGE_SCENARIO_TYPE` 环境变量为对应场景名称。
4. 场景 Handler 实现通用协议定义的方法即可，无需修改通用服务。

### 兼容层

以下旧导入路径通过兼容层继续工作：

| 旧路径 | 兼容方式 |
|---|---|
| `core.bearing_actions` | 兼容层，转发到 `scenarios.bearing._compat.bearing_actions` |
| `core.bearing_workflow_contracts` | 兼容层，转发到 `scenarios.bearing._compat.bearing_workflow_contracts` |
| `core.scenario_registry.BearingCloudHandler` | 通过注册机制动态解析 |

## 验证方法

```powershell
# 1. 编译检查
python -m compileall cloud_edge_project -x ".venv"

# 2. Edge 导入检查
python edge_service/run_edge_service.py --check-import

# 3. Network Simulator 配置检查
python internet_service/network_simulator/run_network_simulator.py --config-dir internet_service/network_simulator/config --check-config

# 4. 从项目目录外启动
cd ..
python cloud_edge_project/edge_service/run_edge_service.py --check-import

# 5. 环境变量覆盖路径
set CLOUD_REVIEW_DB_PATH=C:\temp\test.db
python cloud_edge_project/cloud_service/tests/test_portability.py

# 6. 迁移性测试
python -m pytest cloud_service/tests/test_portability.py -v
```

## 最小复制范围

迁移到新机器时，至少需要复制：

```
cloud_edge_project/
  ├── common/           # 通用配置、日志、工具
  ├── core/             # 通用协议和注册机制
  ├── scenarios/        # 场景实现（至少保留 bearing）
  ├── sender_module/    # 数据发送模块
  ├── edge_service/     # 边缘推理服务
  ├── scheduler/        # 调度服务
  ├── cloud_service/    # 云端服务
  ├── internet_service/ # 网络模拟器
  ├── contracts/        # 契约定义和 Fixture
  ├── data/             # SQLite 数据库（如已有数据）
  ├── configs/          # 本地 YAML 配置
  └── .venv/ 或 requirements.txt  # 依赖
```

### 外部依赖

- Python 3.10+
- MQTT Broker（如 Mosquitto）
- vLLM（可选，Cloud 推理）
- Toxiproxy（可选，网络模拟）
- 模型权重文件（通过环境变量配置路径）

### 回退到原默认配置

不设置任何新增环境变量，所有模块使用代码内默认值，行为与改造前完全一致。
