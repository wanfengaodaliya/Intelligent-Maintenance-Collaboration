# Edge Status Reporter 完整测试流程

## 1. 测试目标

本文档用于完整验证 Edge Status Reporter 及其与 Scheduler、Cloud 的联调功能，包括：

1. 项目依赖和 Python 环境可用。
2. 项目代码可以正常编译。
3. 全部自动化测试通过。
4. Edge、Scheduler、Cloud、Log 四个业务服务可以正常启动。
5. Network Simulator、Toxiproxy、MQTT Broker 和本地 Fake Scheduler 可以正常启动。
6. Reporter 随 Edge 服务自动启动，不需要单独运行。
7. Scheduler 能接收、校验并保存 Reporter 上报的状态字段。
8. Cloud 能接收、保存并查询 Reporter 上报的最新状态。
9. Reporter 周期上报正常，业务活动时间能够更新。
10. 18 条网络代理链路的端口、方向和实际上游与代码配置一致。
11. 业务流量能够按配置经过网络代理，网络状态、参数、评分和可用性可查询。
12. 单个接收端或代理链路异常不会影响 Edge 主业务。
13. 服务停止后没有残留端口。

## 2. 默认组件端口

| 组件 | 默认地址 | 说明 |
|---|---|---|
| Edge Service | `http://127.0.0.1:8001` | 边缘节点业务服务，Reporter 随其自动启动 |
| Scheduler Service | `http://127.0.0.1:8003` | 接收边缘节点状态并参与调度 |
| Cloud Service | `http://127.0.0.1:8004` | 接收并保存最新边缘节点状态 |
| Log Service | `http://127.0.0.1:8006` | 日志服务 |
| Network Fake Scheduler | `http://127.0.0.1:8000` | 仅接收网络模块自身的 `/api/v1/network/reports`，不是项目真实 Scheduler |
| MQTT Broker | `127.0.0.1:1883` | MQTT 默认端口，本次 Reporter HTTP 联调不依赖它 |
| Edge Runtime Control | `http://127.0.0.1:8011` | Edge Runtime 控制端口，当前 FastAPI Reporter 流程不使用 |
| Network Module API | `http://127.0.0.1:8090` | 网络模块接口 |
| Toxiproxy API | `http://127.0.0.1:8474` | 网络模拟控制接口 |

Network Simulator 默认创建 18 条独立代理链路。宿主机业务进程使用 `127.0.0.1:<代理端口>`，Compose 容器内进程使用 `toxiproxy:<代理端口>`。

Sender 到 Edge 的 MQTT 代理端口：

| 链路 | 宿主机代理地址 | 实际上游 |
|---|---|---|
| sender_01 → edge_01 | `127.0.0.1:18831` | `mqtt-broker:1883` |
| sender_01 → edge_02 | `127.0.0.1:18832` | `mqtt-broker:1883` |
| sender_02 → edge_01 | `127.0.0.1:18931` | `mqtt-broker:1883` |
| sender_02 → edge_02 | `127.0.0.1:18932` | `mqtt-broker:1883` |
| sender_03 → edge_01 | `127.0.0.1:19031` | `mqtt-broker:1883` |
| sender_03 → edge_02 | `127.0.0.1:19032` | `mqtt-broker:1883` |

HTTP 代理端口：

| 链路 | 宿主机代理地址 | 实际上游 |
|---|---|---|
| sender_01 → Scheduler | `127.0.0.1:18031` | `127.0.0.1:8003` |
| sender_02 → Scheduler | `127.0.0.1:18032` | `127.0.0.1:8003` |
| sender_03 → Scheduler | `127.0.0.1:18033` | `127.0.0.1:8003` |
| edge_01 → Scheduler | `127.0.0.1:18041` | `127.0.0.1:8003` |
| Scheduler → edge_01 | `127.0.0.1:18042` | `127.0.0.1:8001` |
| edge_01 → Cloud | `127.0.0.1:18043` | `127.0.0.1:8004` |
| Cloud → edge_01 | `127.0.0.1:18044` | `127.0.0.1:8001` |
| Cloud → Scheduler | `127.0.0.1:18045` | `127.0.0.1:8003` |
| edge_02 → Scheduler | `127.0.0.1:18051` | `127.0.0.1:8003` |
| Scheduler → edge_02 | `127.0.0.1:18052` | 本地模拟为 `127.0.0.1:8002`，VM 模式覆盖为 edge_02 地址 |
| edge_02 → Cloud | `127.0.0.1:18053` | `127.0.0.1:8004` |
| Cloud → edge_02 | `127.0.0.1:18054` | 本地模拟为 `127.0.0.1:8002`，VM 模式覆盖为 edge_02 地址 |

> Toxiproxy 容器内使用 `host.docker.internal:8001/8003/8004` 访问宿主机业务服务；表中使用 `127.0.0.1` 表示对应的宿主机实际上游。

Reporter 默认上报地址：

```text
Scheduler: http://127.0.0.1:8003/scheduler/edge-nodes/status
Cloud:     http://127.0.0.1:8004/cloud/edge-status
```

Cloud 最新状态查询地址：

```text
GET http://127.0.0.1:8004/cloud/edge-status/{edge_node_id}
```

## 3. 测试前准备

### 3.1 进入项目目录

打开 PowerShell：

```powershell
cd <仓库目录>\cloud_edge_project
```

### 3.2 指定可用的 Python

本机的 `python` 命令可能命中 Windows 应用执行别名，表现为运行后没有任何输出。建议直接使用已确认可用的解释器：

```powershell
$python = "<Python解释器路径>\python.exe"
```

验证环境：

```powershell
& $python --version
& $python -m pytest --version
& $python -m pip check
```

预期结果包括：

```text
Python 3.12.13
pytest 9.1.1
No broken requirements found.
```

实际版本允许有小幅差异，但命令必须成功执行。

### 3.3 检查默认端口

```powershell
$ports = @(
    8000,8001,8003,8004,8006,8090,8474,
    18031,18032,18033,18041,18042,18043,18044,18045,
    18051,18052,18053,18054,
    1883,18831,18832,18931,18932,19031,19032
)

foreach ($port in $ports) {
    $connection = Get-NetTCPConnection `
        -LocalPort $port `
        -State Listen `
        -ErrorAction SilentlyContinue

    if ($connection) {
        "$port 已占用，PID=$($connection.OwningProcess)"
    } else {
        "$port 空闲"
    }
}
```

如果只测试 Reporter，至少保证 `8001/8003/8004/8006` 空闲；如果进行完整网络联调，则上面列出的全部端口都应空闲。`8011` 是未参与当前流程的保留控制端口，不要求空闲。如果端口被旧项目进程或旧容器占用，应先停止对应服务。

只有在明确 PID 属于本项目旧进程时，才可以执行：

```powershell
Stop-Process -Id <PID>
```

## 4. 静态编译检查

将 Python 字节码缓存写入临时目录，避免项目目录权限问题：

```powershell
$env:PYTHONPYCACHEPREFIX = "$env:TEMP\edge-status-pycache-$PID"

& $python -m compileall -q .
```

命令没有错误输出并正常返回，即表示静态编译通过。

## 5. 自动化测试

### 5.1 全量测试

pytest 默认临时目录可能存在历史权限或清理问题，因此每次使用一个独立目录：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
$env:EDGE_CLOUD_REVIEW_CACHE_DIR = "$env:TEMP\edge-cloud-review-test-$PID"
$testTemp = "$env:TEMP\edge-status-pytest-$([guid]::NewGuid().ToString('N'))"

& $python -m pytest `
    -p no:cacheprovider `
    -W error `
    -q `
    --basetemp $testTemp
```

当前预期结果：

```text
81 passed
```

### 5.2 Reporter 和接收端专项测试

```powershell
$specialTemp = "$env:TEMP\edge-status-special-$([guid]::NewGuid().ToString('N'))"

& $python -m pytest `
    -p no:cacheprovider `
    -W error `
    -q `
    --basetemp $specialTemp `
    edge_service\status_reporter_tests `
    scheduler\tests\test_edge_status_compatibility.py `
    cloud_service\tests\test_edge_status_registry.py
```

当前预期结果：

```text
47 passed
```

专项测试覆盖：

- Reporter 配置解析。
- 系统和进程资源采集。
- GPU/NPU 能力检测。
- 状态快照合同校验。
- 双目标同快照发送。
- Scheduler 和 Cloud 故障隔离。
- Reporter 自动启停和重复启停。
- Edge 业务活动观察。
- Scheduler 接受不包含网络字段的 Reporter 状态。
- Cloud 最新状态保存、旧状态拒绝和非法状态拒绝。

### 5.3 网络链路配置专项测试

该测试只读取配置，不需要启动 Docker：

```powershell
$networkTestTemp = "$env:TEMP\network-links-test-$([guid]::NewGuid().ToString('N'))"

& $python -m pytest `
    -p no:cacheprovider `
    -W error `
    -q `
    --basetemp $networkTestTemp `
    internet_service\network_simulator\verification
```

当前预期结果：

```text
3 passed
```

专项测试验证：

- 配置共生成 `18` 条链路，其中 `6` 条 MQTT、`12` 条 HTTP。
- 每条链路的 `link_id`、代理名称、监听端口和公布端口唯一。
- HTTP 代理端口和 `8001/8003/8004` 实际上游匹配。

## 6. 配置真实联调环境

### 6.1 直连基线配置

为了避免当前 PowerShell 留有关闭 Reporter 或修改 URL 的旧环境变量，启动前应显式设置：

```powershell
$env:EDGE_STATUS_REPORTER_ENABLED = "true"

$env:EDGE_STATUS_SCHEDULER_ENABLED = "true"
$env:EDGE_STATUS_CLOUD_ENABLED = "true"

$env:EDGE_STATUS_SCHEDULER_URL = "http://127.0.0.1:8003/scheduler/edge-nodes/status"
$env:EDGE_STATUS_CLOUD_URL = "http://127.0.0.1:8004/cloud/edge-status"

$env:EDGE_STATUS_INTERVAL_SECONDS = "1.0"

$env:EDGE_STATUS_SCHEDULER_TIMEOUT_SECONDS = "0.5"
$env:EDGE_STATUS_CLOUD_TIMEOUT_SECONDS = "0.5"

$env:EDGE_STATUS_SCHEDULER_RETRY_COUNT = "1"
$env:EDGE_STATUS_CLOUD_RETRY_COUNT = "1"

$env:EDGE_CLOUD_REVIEW_CACHE_DIR = "$env:TEMP\edge-cloud-runtime-$PID"
$env:CLOUD_REVIEW_DB_PATH = "$env:TEMP\cloud-review-runtime-$PID.db"
$env:SCHEDULER_DB_PATH = "$env:TEMP\scheduler-runtime-$PID.db"
```

先完成直连基线测试，再进行网络代理联调。直连成功但代理联调失败时，可以把问题范围缩小到 Network Simulator、代理端口或代理接入配置。

### 6.2 网络模块配置文件

网络模块目录：

```text
<仓库目录>\cloud_edge_project\internet_service\network_simulator
```

正常联调需要核对以下文件：

| 文件 | 需要配置的内容 | 当前默认值或作用 |
|---|---|---|
| `.env` | 宿主机绑定地址、Network API 端口、时区、可选 Token | 默认从 `.env.example` 复制 |
| `docker-compose.yml` | 容器、宿主机发布端口、`host.docker.internal` | 启动 Toxiproxy、MQTT、Fake Scheduler、Controller |
| `config/entities.yaml` | Sender、Edge 和 MQTT 上游 | 3 个 Sender、2 个 Edge、上游 `mqtt-broker:1883` |
| `config/links.yaml` | 代理监听端口、实际上游、链路类型 | 6 条自动生成 MQTT 链路、12 条显式 HTTP 链路 |
| `config/network_states.yaml` | GOOD/MEDIUM/BAD/DISCONNECTED 参数范围 | 延迟、抖动、带宽、丢包、断连模式 |
| `config/transition_matrix.yaml` | Markov 状态转移概率 | 控制各链路状态随时间变化 |
| `config/experiment.yaml` | 实验 ID、模式、随机种子和固定状态 | 默认 `mode: markov`、初始状态 `GOOD` |
| `config/score.yaml` | 网络评分权重和归一化范围 | 默认根据实际应用参数评分 |
| `config/reporter.yaml` | 网络状态上报开关、目标和重试参数 | 默认上报到 Compose 内 Fake Scheduler |
| `config/plugins.yaml` | 插件启停和 API 监听端口 | API 默认容器内监听 `8090` |
| `config/mosquitto.conf` | MQTT Broker 配置 | 本地实验 Broker 配置 |

必须区分两类 Reporter：

- Edge Status Reporter 向真实 Scheduler `8003/scheduler/edge-nodes/status` 和 Cloud `8004/cloud/edge-status` 上报边缘节点状态。
- Network Reporter 向 Compose 内 Fake Scheduler `8000/api/v1/network/reports` 上报网络链路状态。
- 当前真实 Scheduler `8003` 没有 `/api/v1/network/reports`，因此不得把 `NETWORK_SCHEDULER_URL` 改为真实 Scheduler 地址，否则会返回 `404`。

当前有意不配置以下链路，因为项目代码不存在对应通信方向或接口：

- Scheduler → Sender：Sender 没有 HTTP 服务端口。
- Scheduler → Cloud：当前 Scheduler 没有对应直接调用。
- Network Reporter → 真实 Scheduler：两者 HTTP 合同不兼容。

### 6.3 配置网络模块环境

需要先安装并启动 Docker Desktop。打开新的 PowerShell 窗口执行：

```powershell
docker info

cd <仓库目录>\cloud_edge_project\internet_service\network_simulator

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}

Get-Content .env
```

`.env` 默认内容应为：

```dotenv
NETWORK_BIND_ADDRESS=127.0.0.1
NETWORK_API_HOST_PORT=8090
TZ=Asia/Shanghai
NETWORK_REPORT_TOKEN=
NETWORK_LINK_UPSTREAMS_JSON=
```

配置说明：

- `NETWORK_BIND_ADDRESS=127.0.0.1`：所有 Compose 端口只允许本机访问，推荐保留。
- `NETWORK_API_HOST_PORT=8090`：Network API 对外端口；修改后本文所有 `8090` 命令也要同步修改。
- `TZ=Asia/Shanghai`：容器日志时区。
- `NETWORK_REPORT_TOKEN`：仅当 `config/reporter.yaml` 的 `auth.mode` 改为 `bearer` 时填写；默认 `none`，保持空值。
- `NETWORK_LINK_UPSTREAMS_JSON`：虚拟机部署时按 `link_id` 覆盖显式 HTTP 链路上游；本地测试保持空值。
- 不要在不可信网络中把绑定地址改为 `0.0.0.0`，因为本地 MQTT 和调试 API 默认没有对公网设计的鉴权。

启动前先验证 Compose 配置：

```powershell
docker compose --env-file .env config --quiet
```

命令正常返回且没有配置错误，即表示 Compose 配置可解析。

第一次做确定性全链路验收时，建议把 `config/experiment.yaml` 中唯一的：

```yaml
mode: markov
```

临时改为：

```yaml
mode: fixed
```

并保持同一文件中的 `fixed_state.default: GOOD` 不变。这样 18 条链路不会在基础验收期间随机断连。完成基础验收后再恢复 `mode: markov`，重启 `network-controller`，用于验证状态变化和故障隔离：

```powershell
docker compose --env-file .env restart network-controller
docker compose --env-file .env ps
```

### 6.4 配置业务流量经过网络代理

如果业务仍直接访问 `8001/8003/8004/1883`，流量不会经过 Network Simulator。必须在启动业务服务前设置代理地址。

Edge Status Reporter 经过 edge_01 的 HTTP 代理：

```powershell
$env:EDGE_STATUS_REPORTER_ENABLED = "true"
$env:EDGE_STATUS_SCHEDULER_ENABLED = "true"
$env:EDGE_STATUS_CLOUD_ENABLED = "true"
$env:EDGE_STATUS_SCHEDULER_URL = "http://127.0.0.1:18041/scheduler/edge-nodes/status"
$env:EDGE_STATUS_CLOUD_URL = "http://127.0.0.1:18043/cloud/edge-status"
```

Scheduler 调用 edge_01 经过 `18042`：

```powershell
$env:SCHEDULER_EDGE_NODES_JSON = '{"edge_01":{"control_url":"http://127.0.0.1:18042","target_topic":"edge/edge_01/input"}}'
```

Cloud 获取 edge_01 原始上下文经过 `18044`：

```powershell
$env:EDGE_RAW_CONTEXT_BASE_URL = "http://127.0.0.1:18044"
```

`18045` 已为 Cloud → Scheduler 预留并配置到真实 Scheduler `8003`，但当前 Cloud 业务代码没有对应出站调用，因此现阶段不需要额外环境变量。

虚拟机多边缘部署时，每台 Edge 都可以继续监听 `8001`，因为虚拟机 IP 不同。每台 Edge 必须设置唯一节点身份，并把所有出站地址指向中央服务。例如 edge_02 虚拟机：

```powershell
$env:EDGE_NODE_ID = "edge_02"
$env:EDGE_STATUS_SCHEDULER_URL = "http://192.168.56.10:8003/scheduler/edge-nodes/status"
$env:EDGE_STATUS_CLOUD_URL = "http://192.168.56.11:8004/cloud/edge-status"
$env:SCHEDULER_SERVICE_BASE_URL = "http://192.168.56.10:8003"
$env:CLOUD_SERVICE_BASE_URL = "http://192.168.56.11:8004"
$env:EDGE_CLOUD_REVIEW_CACHE_DIR = "D:\edge-data\edge_02\cloud-review"

$env:EDGE_MQTT_HOST = "192.168.56.12"
$env:EDGE_MQTT_PORT = "1883"
$env:EDGE_MQTT_INPUT_TOPIC = "edge/edge_02/input"
$env:EDGE_MQTT_CLIENT_ID = "edge_02-runtime"
```

Scheduler 虚拟机注册所有节点：

```powershell
$env:SCHEDULER_EDGE_NODES_JSON = '{"edge_01":{"control_url":"http://192.168.56.21:8001","target_topic":"edge/edge_01/input"},"edge_02":{"control_url":"http://192.168.56.22:8001","target_topic":"edge/edge_02/input"}}'
```

Cloud 虚拟机配置按节点回调地址，同时保留原单节点变量作为旧请求回退：

```powershell
$env:EDGE_RAW_CONTEXT_BASE_URL = "http://192.168.56.21:8001"
$env:EDGE_RAW_CONTEXT_BASE_URLS_JSON = '{"edge_01":"http://192.168.56.21:8001","edge_02":"http://192.168.56.22:8001"}'
```

Network Simulator 集中部署时，在 `.env` 中配置可访问的虚拟机上游：

```dotenv
NETWORK_BIND_ADDRESS=0.0.0.0
NETWORK_LINK_UPSTREAMS_JSON={"sender_01__to__scheduler__http":"192.168.56.10:8003","sender_02__to__scheduler__http":"192.168.56.10:8003","sender_03__to__scheduler__http":"192.168.56.10:8003","edge_01__to__scheduler__http":"192.168.56.10:8003","scheduler__to__edge_01__http":"192.168.56.21:8001","edge_01__to__cloud__http":"192.168.56.11:8004","cloud__to__edge_01__http":"192.168.56.21:8001","cloud__to__scheduler__http":"192.168.56.10:8003","edge_02__to__scheduler__http":"192.168.56.10:8003","scheduler__to__edge_02__http":"192.168.56.22:8001","edge_02__to__cloud__http":"192.168.56.11:8004","cloud__to__edge_02__http":"192.168.56.22:8001"}
```

`NETWORK_LINK_UPSTREAMS_JSON` 只覆盖列出的 `link_id`，其余链路继续使用 `config/links.yaml` 默认值。绑定到 `0.0.0.0` 后必须用虚拟机防火墙限制来源，禁止暴露公网。

Sender 的调度和 MQTT 地址来自 `sender_module/config/local.json`，不是环境变量。若当前只运行真实节点 edge_01，可将三个 Sender 的对应字段改为：

```json
{
  "sender_01": {
    "scheduler_url": "http://127.0.0.1:18031/scheduler/decide",
    "mqtt_host": "127.0.0.1",
    "mqtt_port": 18831
  },
  "sender_02": {
    "scheduler_url": "http://127.0.0.1:18032/scheduler/decide",
    "mqtt_host": "127.0.0.1",
    "mqtt_port": 18931
  },
  "sender_03": {
    "scheduler_url": "http://127.0.0.1:18033/scheduler/decide",
    "mqtt_host": "127.0.0.1",
    "mqtt_port": 19031
  }
}
```

上面的 JSON 仅展示每个 Sender 需要替换的三个字段，不应直接覆盖整个 `local.json`。如目标改为 edge_02，MQTT 端口分别使用 `18832/18932/19032`。当前 Sender 配置一次只能指定一个 MQTT 代理端口。

### 6.5 启动网络模块

在网络模块 PowerShell 窗口执行：

```powershell
cd <仓库目录>\cloud_edge_project\internet_service\network_simulator
docker compose --env-file .env up -d --build --wait
docker compose --env-file .env ps
```

首次启动需要拉取镜像并构建，耗时可能较长。以后代码没有变化时可执行：

```powershell
docker compose --env-file .env up -d --wait
```

预期以下四个容器启动：

| Compose 服务 | 默认宿主机端口 | 作用 |
|---|---:|---|
| `toxiproxy` | `8474` 和 18 个代理端口 | 创建代理并施加网络参数 |
| `mqtt-broker` | `1883` | 接收代理转发的 MQTT 数据 |
| `scheduler` | `8000` | Network Reporter 专用 Fake Scheduler |
| `network-controller` | `8090` | 状态生成、参数应用、评分、上报和只读 API |

> `start_all.py` 只启动 Edge、Scheduler、Cloud、Log，不会启动网络模块；网络模块必须通过本节的 `docker compose` 命令单独启动。

## 7. 启动全部服务

在第一个 PowerShell 窗口中执行：

```powershell
& $python start_all.py
```

预期看到：

```text
starting edge_service at http://127.0.0.1:8001
starting scheduler_service at http://127.0.0.1:8003
starting cloud_service at http://127.0.0.1:8004
starting log_service at http://127.0.0.1:8006
```

保持这个窗口运行，不要关闭，也不要执行其他命令。

注意：

- pytest 只运行测试，不会持续监听端口。
- 真正启动服务必须运行 `start_all.py`。
- 修改代码后必须停止并重新启动服务，否则旧进程仍使用修改前的代码。
- Reporter 随 Edge lifespan 自动启动，不需要额外运行 Reporter 命令。

## 8. 验证服务健康状态

打开第二个 PowerShell：

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
Invoke-RestMethod http://127.0.0.1:8003/health
Invoke-RestMethod http://127.0.0.1:8004/health
Invoke-RestMethod http://127.0.0.1:8006/health
```

四个接口都应返回包含以下内容的结果：

```text
status : ok
```

确认端口正在监听：

```powershell
Get-NetTCPConnection -State Listen |
    Where-Object LocalPort -in 8001,8003,8004,8006 |
    Select-Object LocalAddress,LocalPort,OwningProcess
```

预期存在四个端口的监听记录。

### 8.1 验证网络模块健康状态

如果本轮进行网络联调，继续执行：

```powershell
$networkHealth = Invoke-RestMethod http://127.0.0.1:8090/health
$networkHealth

Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8474/version
```

Network API 预期至少满足：

```text
status                     : ok
toxiproxy_available        : True
scheduler_reporter_healthy : True
link_count                 : 18
```

`available_link_count` 会随 Markov 状态变化，不要求永远等于 `18`。如果某条链路进入 `DISCONNECTED`，代理请求失败属于网络模拟预期行为。

检查 Toxiproxy 实际创建的代理：

```powershell
$proxies = Invoke-RestMethod http://127.0.0.1:8474/proxies
$proxyNames = @($proxies.PSObject.Properties.Name)
$proxyNames.Count
$proxyNames | Sort-Object
```

预期数量为 `18`，名称包括 6 条 `sender_xx__to__edge_xx__mqtt` 和 12 条 HTTP 链路。

检查 Network API 返回的完整链路字段：

```powershell
$links = @(Invoke-RestMethod http://127.0.0.1:8090/api/v1/network/links)
$links.Count

$links |
    Select-Object link_id,link_type,protocol,advertised_port,upstream,current_state,link_reliability_score,available,last_apply_success |
    Format-Table -AutoSize
```

预期：

- `$links.Count` 为 `18`。
- 每条记录包含 desired/applied 网络参数、评分、可用性和最近应用结果。
- `current_state` 为 `GOOD/MEDIUM/BAD/DISCONNECTED` 之一。
- `advertised_port` 和本文第 2 节端口表一致。

检查 Network Reporter 是否持续上报到 Fake Scheduler：

```powershell
$networkCache = Invoke-RestMethod http://127.0.0.1:8000/api/v1/network/cache
$networkCache | ConvertTo-Json -Depth 8
```

应能看到 `network-controller-01` 的上报缓存和链路数据。该步骤验证的是 Network Reporter，不是 Edge Status Reporter。

### 8.2 验证 HTTP 代理转发

业务服务和网络模块都启动后执行：

```powershell
$httpProxyHealthUrls = @(
    "http://127.0.0.1:18031/health",
    "http://127.0.0.1:18032/health",
    "http://127.0.0.1:18033/health",
    "http://127.0.0.1:18041/health",
    "http://127.0.0.1:18042/health",
    "http://127.0.0.1:18043/health",
    "http://127.0.0.1:18044/health",
    "http://127.0.0.1:18045/health",
    "http://127.0.0.1:18051/health",
    "http://127.0.0.1:18052/health",
    "http://127.0.0.1:18053/health",
    "http://127.0.0.1:18054/health"
)

foreach ($url in $httpProxyHealthUrls) {
    try {
        $result = Invoke-RestMethod $url -TimeoutSec 5
        "PASS $url -> $($result.status)"
    } catch {
        "SIMULATED-FAIL $url -> $($_.Exception.Message)"
    }
}
```

端口对应关系：

- `18031/18032/18033/18041/18045/18051` 的上游是 Scheduler `8003`。
- `18042/18044` 的上游是 edge_01。
- `18052/18054` 的上游是 edge_02。
- `18043/18053` 的上游是 Cloud `8004`。

默认实验为 Markov 模式，链路可能主动进入 `DISCONNECTED`，因此偶发 `SIMULATED-FAIL` 不等于程序错误。若所有代理持续失败，应检查 Toxiproxy、业务上游和 `host.docker.internal`。

### 8.3 验证 Edge Status Reporter 经过代理

本项的前提是启动 `start_all.py` 前已经按第 6.4 节把 Reporter URL 设置为 `18041` 和 `18043`。等待 3 秒后执行：

```powershell
Start-Sleep -Seconds 3

$scheduler = Invoke-RestMethod http://127.0.0.1:8003/health
$scheduler.edge_nodes

$cloud = Invoke-RestMethod http://127.0.0.1:8004/cloud/edge-status/edge_01
$cloud.edge_node_id
$cloud.reported_at_ns
```

在链路可用时，Scheduler 应显示 `online = 1`，Cloud 应返回 `edge_01` 最新状态。这证明 Reporter 的实际请求经过 `18041/18043` 后分别到达真实 Scheduler 和 Cloud，而不只是验证代理端口可连接。

同时可查看网络链路状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8090/api/v1/network/links/edge_01__to__scheduler__http |
    ConvertTo-Json -Depth 8

Invoke-RestMethod http://127.0.0.1:8090/api/v1/network/links/edge_01__to__cloud__http |
    ConvertTo-Json -Depth 8
```

如果 Reporter 日志偶发超时，同时对应链路状态为 `DISCONNECTED`，这是模拟结果；链路恢复后 Reporter 会继续周期上报，Edge 主服务不应退出。

## 9. 验证 Scheduler 接收状态

### 9.1 HTTP 连接和节点在线验证

Scheduler 初始状态为 `OFFLINE`，连续收到两次有效状态后变为 `ONLINE`。等待约 3 秒：

```powershell
Start-Sleep -Seconds 3

$scheduler = Invoke-RestMethod http://127.0.0.1:8003/health
$scheduler.edge_nodes
```

预期：

```text
registered : 1
online     : 1
offline    : 0
```

该检查可以证明：

- Reporter 成功连接 Scheduler。
- Scheduler 接口返回成功。
- Scheduler 至少连续接收了两次合法状态。
- Scheduler 在线状态机正常工作。

该检查不能单独证明每个字段的具体值，因为 `/health` 只返回节点数量，不返回完整状态。

### 9.2 Scheduler 字段级验证

Scheduler 实际校验并保存以下 Reporter 字段：

```text
edge_node_id
reported_at_ns
resources.logical_cpu_count
resources.cpu_utilization_percent
resources.memory_available_mb
resources.gpu_available
resources.npu_available
resources.queue_length
models[].model_version
models[].load_status
last_task_activity_ns
```

`network_to_scheduler` 是可选字段。Reporter 不生成网络状态；网络状态仍由 Network Module 负责。

当前 Scheduler 没有完整状态 HTTP 查询接口。可以运行下面的内部合同测试，确认字段被校验并保存：

```powershell
@'
import time

from scheduler.node_registry import NodeRegistry


registry = NodeRegistry()

payload = {
    "edge_node_id": "edge_01",
    "reported_at_ns": time.time_ns(),
    "resources": {
        "logical_cpu_count": 8,
        "cpu_utilization_percent": 25.5,
        "memory_available_mb": 4096.0,
        "gpu_available": False,
        "npu_available": False,
        "queue_length": 3,
    },
    "models": [
        {
            "model_version": "bearing_packet_model_v1",
            "load_status": "LOADED",
        }
    ],
    "last_task_activity_ns": time.time_ns(),
}

result = registry.update_status(payload)
stored = registry._nodes["edge_01"].report

assert result["accepted"] is True
assert stored["edge_node_id"] == payload["edge_node_id"]
assert stored["reported_at_ns"] == payload["reported_at_ns"]
assert stored["resources"]["logical_cpu_count"] == 8
assert stored["resources"]["cpu_utilization_percent"] == 25.5
assert stored["resources"]["memory_available_mb"] == 4096.0
assert stored["resources"]["gpu_available"] is False
assert stored["resources"]["npu_available"] is False
assert stored["resources"]["queue_length"] == 3
assert stored["models"][0]["model_version"] == "bearing_packet_model_v1"
assert stored["models"][0]["load_status"] == "LOADED"
assert stored["last_task_activity_ns"] == payload["last_task_activity_ns"]

print("Scheduler 字段接收、校验和保存全部通过")
print(stored)
'@ | & $python -
```

预期输出：

```text
Scheduler 字段接收、校验和保存全部通过
```

## 10. 验证 Cloud 接收完整状态

查询 Cloud 保存的 `edge_01` 最新状态：

```powershell
$cloudStatus = Invoke-RestMethod `
    http://127.0.0.1:8004/cloud/edge-status/edge_01

$cloudStatus | ConvertTo-Json -Depth 10
```

预期返回包含以下字段的完整状态：

```text
edge_node_id
reported_at_ns
resources
models
last_task_activity_ns
```

示例结构：

```json
{
  "edge_node_id": "edge_01",
  "reported_at_ns": 123456789,
  "resources": {
    "logical_cpu_count": 8,
    "cpu_utilization_percent": 20.0,
    "memory_available_mb": 4096.0,
    "gpu_available": false,
    "npu_available": false,
    "queue_length": 0
  },
  "models": [
    {
      "model_version": "bearing_packet_model_v1",
      "load_status": "LOADED"
    }
  ],
  "last_task_activity_ns": 0
}
```

## 11. 验证周期上报

读取两次 Cloud 状态并比较时间戳：

```powershell
$first = Invoke-RestMethod `
    http://127.0.0.1:8004/cloud/edge-status/edge_01

Start-Sleep -Seconds 2

$second = Invoke-RestMethod `
    http://127.0.0.1:8004/cloud/edge-status/edge_01

"第一次：$($first.reported_at_ns)"
"第二次：$($second.reported_at_ns)"
"是否持续上报：$($second.reported_at_ns -gt $first.reported_at_ns)"
```

预期：

```text
是否持续上报：True
```

## 12. 验证业务活动时间

先读取当前状态：

```powershell
$before = Invoke-RestMethod `
    http://127.0.0.1:8004/cloud/edge-status/edge_01
```

向 Edge 发送一个业务请求：

```powershell
try {
    Invoke-WebRequest `
        -Uri http://127.0.0.1:8001/edge/infer `
        -Method POST `
        -ContentType "application/json" `
        -Body "{}"
} catch {
    "空载荷返回业务校验错误属于预期：$($_.Exception.Message)"
}
```

空载荷预计返回 `400`，但 POST 请求仍应被 Reporter 中间件记录为业务活动。等待下一轮上报：

```powershell
Start-Sleep -Seconds 2

$after = Invoke-RestMethod `
    http://127.0.0.1:8004/cloud/edge-status/edge_01

"请求前：$($before.last_task_activity_ns)"
"请求后：$($after.last_task_activity_ns)"
"活动时间已更新：$($after.last_task_activity_ns -gt $before.last_task_activity_ns)"
```

预期：

```text
活动时间已更新：True
```

## 13. 验证非法状态拒绝

向 Cloud 发送非法状态：

```powershell
try {
    Invoke-RestMethod `
        -Uri http://127.0.0.1:8004/cloud/edge-status `
        -Method POST `
        -ContentType "application/json" `
        -Body "{}"
} catch {
    $_.ErrorDetails.Message
}
```

预期 HTTP 状态为 `400`，响应包含：

```json
{
  "error_code": "INVALID_EDGE_STATUS"
}
```

非法状态不应覆盖已有合法状态。再次查询：

```powershell
Invoke-RestMethod http://127.0.0.1:8004/cloud/edge-status/edge_01
```

合法状态仍应存在。

## 14. 验证未知节点查询

```powershell
try {
    Invoke-RestMethod `
        http://127.0.0.1:8004/cloud/edge-status/not-exists
} catch {
    $_.ErrorDetails.Message
}
```

预期 HTTP 状态为 `404`，响应包含：

```json
{
  "error_code": "EDGE_STATUS_NOT_FOUND"
}
```

## 15. 观察服务日志

Reporter 正常运行时，第一个 PowerShell 窗口应周期出现：

```text
POST /scheduler/edge-nodes/status HTTP/1.1 200
POST /cloud/edge-status HTTP/1.1 200
```

不应持续出现：

```text
HTTP_400
HTTP_404
```

如果仍出现旧的 `400` 或 `404`：

1. 先停止所有旧服务。
2. 确认 `8001/8003/8004/8006` 已释放。
3. 重新运行 `start_all.py`。
4. 不要只刷新浏览器；Python 进程必须重启才会加载新代码。

## 16. 故障隔离验证

可以临时关闭 Cloud 服务，观察 Scheduler 是否仍接收状态：

1. 停止全部服务。
2. 分别启动 Scheduler 和 Edge，暂不启动 Cloud。
3. 等待约 3 秒。
4. 查询 Scheduler 健康接口。

Edge 和 Scheduler 的手动启动命令：

```powershell
& $python -m uvicorn scheduler.api:app --host 127.0.0.1 --port 8003
```

另开窗口：

```powershell
cd <仓库目录>\cloud_edge_project
$python = "<Python解释器路径>\python.exe"

& $python -m uvicorn edge_service.app:app --host 127.0.0.1 --port 8001
```

再开一个窗口查询：

```powershell
Start-Sleep -Seconds 3
Invoke-RestMethod http://127.0.0.1:8001/health
Invoke-RestMethod http://127.0.0.1:8003/health
```

预期：

- Cloud 上报出现连接失败日志。
- Scheduler 状态上报仍返回 `200`。
- Scheduler 节点仍能变为 `ONLINE`。
- Edge 健康接口仍返回 `status = ok`。

完成后分别按 `Ctrl+C` 停止服务。

## 17. 停止服务并检查清理

回到运行 `start_all.py` 的窗口，按：

```text
Ctrl+C
```

如果启动了网络模块，再回到网络模块目录停止 Compose：

```powershell
cd <仓库目录>\cloud_edge_project\internet_service\network_simulator
docker compose --env-file .env down
```

该命令停止并删除本次 Compose 容器和网络，不删除命名日志卷。

等待数秒后检查端口：

```powershell
$ports = @(
    8000,8001,8003,8004,8006,8090,8474,
    18031,18032,18033,18041,18042,18043,18044,18045,
    18051,18052,18053,18054,
    1883,18831,18832,18931,18932,19031,19032
)

foreach ($port in $ports) {
    $connection = Get-NetTCPConnection `
        -LocalPort $port `
        -State Listen `
        -ErrorAction SilentlyContinue

    if ($connection) {
        "$port 仍被占用，PID=$($connection.OwningProcess)"
    } else {
        "$port 已释放"
    }
}
```

预期本轮启动过的端口全部释放。`8011` 当前未参与 FastAPI Reporter 流程，因此停止检查中不强制包含。

## 18. 最终验收标准

以下条件全部满足时，本次功能测试通过：

- [ ] Python、pytest 和项目依赖检查通过。
- [ ] 全项目静态编译通过。
- [ ] 全量测试结果为 `81 passed`。
- [ ] Reporter 和接收端专项测试结果为 `47 passed`。
- [ ] 网络链路配置专项测试结果为 `3 passed`。
- [ ] `docker compose --env-file .env config --quiet` 通过。
- [ ] Edge `8001` 健康接口返回 `status = ok`。
- [ ] Scheduler `8003` 健康接口返回 `status = ok`。
- [ ] Cloud `8004` 健康接口返回 `status = ok`。
- [ ] Log `8006` 健康接口返回 `status = ok`。
- [ ] Network Fake Scheduler `8000` 健康接口可访问。
- [ ] Network API `8090` 返回 `status = ok` 和 `link_count = 18`。
- [ ] Toxiproxy API `8474` 可访问并实际创建 `18` 个代理。
- [ ] 6 条 MQTT 和 12 条 HTTP 链路端口与第 2 节一致。
- [ ] Scheduler 显示 `registered = 1`、`online = 1`、`offline = 0`。
- [ ] Scheduler 字段级合同测试通过。
- [ ] Cloud 能查询 `edge_01` 的完整最新状态。
- [ ] Cloud 中的 `reported_at_ns` 持续增长。
- [ ] Edge POST 业务请求后 `last_task_activity_ns` 增长。
- [ ] Scheduler 上报接口周期返回 `200`。
- [ ] Cloud 上报接口周期返回 `200`。
- [ ] 日志中不再持续出现 Scheduler `400` 或 Cloud `404`。
- [ ] Reporter 使用 `18041/18043` 时，真实 Scheduler 和 Cloud 能接收状态。
- [ ] Network Reporter 的数据可从 Fake Scheduler `8000/api/v1/network/cache` 查询。
- [ ] Network API 能查询链路状态、应用参数、评分和可用性。
- [ ] 非法 Cloud 状态被拒绝且不覆盖合法状态。
- [ ] 一个上报目标故障时，另一个目标和 Edge 主业务仍正常。
- [ ] 停止项目和网络 Compose 后，本轮使用的业务端口、控制端口和代理端口全部释放。

## 19. 常见问题

### 19.1 执行 `python` 后没有输出

原因通常是命中了 Windows 应用执行别名。不要使用裸 `python`，改用：

```powershell
& "<Python解释器路径>\python.exe" --version
```

### 19.2 pytest 在 setup 阶段大量报错

如果错误集中在 `tmp_path`，通常是 pytest 默认临时目录权限或历史残留问题。使用新的 `--basetemp`：

```powershell
$testTemp = "$env:TEMP\edge-status-pytest-$([guid]::NewGuid().ToString('N'))"
& $python -m pytest -p no:cacheprovider -q --basetemp $testTemp
```

### 19.3 四个健康接口都连接失败

pytest 不会启动服务。请在项目目录运行：

```powershell
& $python start_all.py
```

并保持该窗口运行。

### 19.4 日志仍显示 Scheduler 400、Cloud 404

通常是修改前启动的旧进程仍占用端口。停止旧服务并重新启动：

```powershell
foreach ($port in 8001,8003,8004,8006) {
    Get-NetTCPConnection `
        -LocalPort $port `
        -State Listen `
        -ErrorAction SilentlyContinue |
        Select-Object LocalPort,OwningProcess
}
```

确认旧服务停止后再次运行 `start_all.py`。

### 19.5 Cloud 查询立即返回 404

Reporter 启动后需要等待首轮上报。先等待 2 秒：

```powershell
Start-Sleep -Seconds 2
Invoke-RestMethod http://127.0.0.1:8004/cloud/edge-status/edge_01
```

### 19.6 Scheduler 短时间仍显示 OFFLINE

Scheduler 需要连续接收两次有效状态才会切换为 `ONLINE`。默认上报周期为 1 秒，建议等待 3 秒后再查询。

### 19.7 `docker compose` 无法连接 Docker Engine

如果 `docker` 命令存在，但提示无法连接 daemon，通常是 Docker Desktop 尚未启动。先启动 Docker Desktop，并等待以下命令显示 `Server` 信息：

```powershell
docker info
```

### 19.8 Network API `8090` 无法连接

先检查容器状态和日志：

```powershell
cd <仓库目录>\cloud_edge_project\internet_service\network_simulator
docker compose --env-file .env ps
docker compose --env-file .env logs network-controller --tail 100
docker compose --env-file .env logs toxiproxy --tail 100
```

如果 `.env` 修改了 `NETWORK_API_HOST_PORT`，访问地址必须使用修改后的端口，而不是 `8090`。

### 19.9 代理端口可监听但请求失败

按顺序检查：

1. 实际上游 `8001/8003/8004` 是否已经启动。
2. `http://127.0.0.1:8474/proxies` 是否存在对应代理。
3. `http://127.0.0.1:8090/api/v1/network/links/{link_id}` 的 `current_state` 是否为 `DISCONNECTED`。
4. Docker 容器是否能通过 `host.docker.internal` 访问宿主机。

默认 Markov 模式会主动模拟断连。链路为 `DISCONNECTED` 时请求超时或失败属于预期，不应直接修改业务代码绕过代理。

### 19.10 Reporter 仍然直接访问 `8003/8004`

Reporter 在 Edge 进程启动时读取环境变量。必须先设置以下变量，再启动或重启 `start_all.py`：

```powershell
$env:EDGE_STATUS_SCHEDULER_URL = "http://127.0.0.1:18041/scheduler/edge-nodes/status"
$env:EDGE_STATUS_CLOUD_URL = "http://127.0.0.1:18043/cloud/edge-status"
```

只修改环境变量但不重启旧 Edge 进程不会生效。

### 19.11 Network Reporter 向真实 Scheduler 返回 404

Network Reporter 与 Edge Status Reporter 使用不同合同。当前 Network Reporter 必须保持：

```text
http://scheduler:8000/api/v1/network/reports
```

真实 Scheduler `8003` 接收 Edge Status Reporter 的地址是：

```text
http://127.0.0.1:8003/scheduler/edge-nodes/status
```

两者不能互换。
