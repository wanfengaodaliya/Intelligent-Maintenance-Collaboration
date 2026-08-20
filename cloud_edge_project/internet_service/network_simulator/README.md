# 网络模拟模块 V3

## 1. 模块简介

本模块为发送器、边缘节点、调度器和云端之间的真实 TCP 通信提供可重复的动态网络模拟。业务程序不需要调用模拟函数，也不会修改 `SensorPacket`；业务只需连接指定的 Toxiproxy 代理入口，延迟、抖动、带宽限制和断连便会作用于实际 MQTT/HTTP 流量。

V3 的核心能力包括：

- 根据发送器和边缘节点配置自动生成 Sender→Edge 笛卡尔积链路；
- 每条链路拥有独立 Proxy、随机源、随机种子、状态和运行时数据；
- 支持 `GOOD`、`MEDIUM`、`BAD`、`DISCONNECTED` 四种网络状态；
- 默认每秒生成参数并应用到 Toxiproxy；
- 区分期望状态（desired）和实际成功应用状态（applied）；
- 计算 0～100 的 Link Reliability Score；
- 异步批量向 Scheduler 主动上报完整 Tick 快照；
- 提供只读 FastAPI 查询接口和五类可轮转日志；
- 单条链路、Reporter、Logger 和可选 API 故障不会破坏其他链路的核心模拟。

> 重要：业务通信如果直接连接 MQTT Broker 或原 HTTP 服务、没有经过对应 Proxy，则网络模拟不会生效。

## 2. 组成与作用

| 组成 | 主要内容 | 作用 |
|---|---|---|
| Controller | 生命周期、Tick 编排、运行时快照、信号处理 | 按固定周期组织“生成→应用→评分→记录→上报”流程 |
| ConfigLoader | 8 个 YAML 文件、环境变量覆盖、跨文件校验 | 在创建 Proxy 前发现拼写、端口、状态或权重错误 |
| Markov/Fixed 插件 | 状态转移、参数采样、每链路独立 RNG | 生成可持续、可复现的动态网络序列，或固定状态实验 |
| Toxiproxy 插件 | Proxy 幂等创建、toxic 应用、恢复和退出清理 | 对真实 TCP 流量施加 latency、bandwidth、timeout/reset_peer |
| Score 插件 | 参数归一化、加权评分、失败限分 | 生成可解释的调度参考指标 |
| Reporter 插件 | 快照构建、有界队列、重试、Bearer 认证 | 主动把实际已应用链路状态批量 POST 给 Scheduler |
| API/Health 插件 | `/health`、链路和运行时查询 | 提供内部调试和实验观测接口，不修改模拟状态 |
| Logger 插件 | 控制器日志、四类 JSONL、轮转和敏感信息脱敏 | 记录真值、toxic 操作、评分与上报结果 |
| Fake Scheduler | 报告接收、幂等校验、最新链路缓存 | 用于本地联调，不执行真实调度决策 |
| Docker Compose | Toxiproxy、Mosquitto、Fake Scheduler、Controller | 一条命令启动 V3 的完整本地实验环境 |

每个 Tick 的顺序如下：

```text
TickStarted
  → 每条链路独立生成 desired 状态和参数
  → Toxiproxy 应用 latency/bandwidth/disconnect toxic
  → 成功时更新 applied；失败时保留上一次 applied
  → 基于 applied 计算 Link Reliability Score
  → 冻结完整 RuntimeSnapshot
  → 写状态和评分日志
  → Reporter 异步提交同一份完整快照
```

## 3. 项目目录

```text
network_simulator/
├── controller/                  # 配置、运行时、插件编排、Tick 与生命周期
├── domain/                      # 枚举、事件、异常和领域模型
├── plugins/
│   ├── api/                     # 只读 FastAPI
│   ├── health/                  # 健康状态聚合
│   ├── logger/                  # controller.log 与 JSONL
│   ├── markov/                  # Markov、fixed 和参数映射
│   ├── reporter/                # 报文、队列、HTTP 客户端
│   ├── score/                   # 可靠性评分
│   └── toxiproxy/               # 管理客户端和状态应用
├── scheduler_stub/              # 本地 Fake Scheduler
├── config/                      # V3 配置及 Mosquitto 配置
├── scripts/                     # Windows/Linux 本地启动脚本
├── tests/
│   ├── unit/                    # 单元测试
│   ├── contract/                # API/Reporter 契约测试
│   ├── integration/             # 真实 Toxiproxy 效果测试
│   └── fixtures/                # 测试配置工厂
├── logs/                        # 非容器运行时默认日志目录
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
├── .env.example
└── .gitignore
```

## 4. 前置条件

- Docker Desktop（Windows/macOS）或 Docker Engine（Linux）；
- Docker Compose V2，可通过 `docker compose version` 检查；
- 本地运行或测试时需要 Python 3.11+；
- 默认仅在宿主机 `127.0.0.1` 绑定端口 `8000`、`8090`、`8474`、`1883`、`18831`、`18832`、`18931`、`18932`，这些端口需要可用；
- 接入真实 Scheduler 时，应提供 `POST /scheduler/network-reports`（见 `reporter.yaml`）;
- Windows 使用 Docker Desktop 时，必须先启动 Docker Desktop 并等待 Engine 就绪。

建议先检查：

```bash
docker version
docker compose version
docker info
```

`docker` 命令存在但 `docker info` 无法连接 daemon，通常表示 Docker Desktop/Engine 尚未启动，而不是本工程配置错误。

## 5. 快速启动

以下流程会一次启动网络模拟模块所需的四个服务：Toxiproxy、MQTT Broker、Fake Scheduler 和 Network Controller。

### 5.1 Windows PowerShell（推荐）

#### 第一步：启动 Docker

打开 Docker Desktop，等待 Docker Engine 启动完成，然后执行：

```powershell
docker info
```

命令能显示 `Server` 信息后再继续。

#### 第二步：进入项目并启动全部服务

复制下面整个代码块执行：

```powershell
cd "<克隆后的项目目录>\network_simulator"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose --env-file .env up -d --build --wait
```

例如仓库直接克隆为 `network_simulator` 时，先进入其父目录，再执行上面的命令。不要照抄某位开发者电脑上的绝对路径。

第一次启动需要下载镜像和安装依赖，等待时间可能较长。以后代码没有变化时，可以使用：

```powershell
docker compose --env-file .env up -d --wait
```

#### 第三步：确认启动成功

```powershell
docker compose ps
```

以下四个服务都显示 `Healthy` 即表示容器启动成功：

| 服务 | 作用 |
|---|---|
| `toxiproxy` | 施加真实网络延迟、带宽限制和断连 |
| `mqtt-broker` | 转发 MQTT 传感器数据 |
| `scheduler` | 接收 Reporter 上报的本地 Fake Scheduler |
| `network-controller` | 生成网络状态、应用参数、评分并提供 API |

继续检查 Controller：

```powershell
curl.exe http://127.0.0.1:8090/health
```

返回内容包含以下字段即表示整个网络模拟模块运行正常：

```json
{
  "status": "ok",
  "toxiproxy_available": true,
  "scheduler_reporter_healthy": true,
  "link_count": 18
}
```

Windows 默认使用 `127.0.0.1`，避免主机名优先解析到 IPv6 `::1` 时出现空响应或超时。如果在 `.env` 中修改了 `NETWORK_API_HOST_PORT`，请同步替换上面的 8090。

Compose 的全部宿主机端口默认绑定到 `127.0.0.1`，只能从本机访问。只有明确需要局域网访问时，才可在 `.env` 中设置 `NETWORK_BIND_ADDRESS=0.0.0.0`；此时必须同时配置主机防火墙、必要的认证，并确保网络可信。该 Compose 环境包含匿名 MQTT 和无鉴权调试 API，禁止直接暴露到公网或生产网络。

#### 第四步：查看链路和 Scheduler 上报结果

```powershell
curl.exe http://127.0.0.1:8090/api/v1/network/links
curl.exe http://127.0.0.1:8000/api/v1/network/cache
```

默认应能看到 18 条链路，其中 6 条 Sender→Edge MQTT 链路、12 条 HTTP 链路。

### 5.2 Linux/macOS

```bash
cd network_simulator
cp -n .env.example .env
docker compose --env-file .env up -d --build --wait
curl http://127.0.0.1:8090/health
```

### 5.3 停止项目

```powershell
docker compose --env-file .env down
```

该命令删除容器和 Compose 网络，但不会删除保存实验日志的命名卷。Controller 会等待 Toxiproxy 和 Broker 健康；Scheduler 是可选依赖，Scheduler 或 Reporter 暂时失败不会停止核心网络模拟。

## 6. 默认独立链路与业务接入

默认 `entities.yaml` 定义 3 个发送器和 2 个边缘节点，`cartesian` 模式生成 6 条独立 MQTT Proxy：

| link_id | Sender | Edge | 容器内接入地址 | 宿主机接入地址 | 上游 |
|---|---|---|---|---|---|
| `sender_01__to__edge_01__mqtt` | sender_01 | edge_01 | `toxiproxy:18831` | `127.0.0.1:18831` | `mqtt-broker:1883` |
| `sender_01__to__edge_02__mqtt` | sender_01 | edge_02 | `toxiproxy:18832` | `127.0.0.1:18832` | `mqtt-broker:1883` |
| `sender_02__to__edge_01__mqtt` | sender_02 | edge_01 | `toxiproxy:18931` | `127.0.0.1:18931` | `mqtt-broker:1883` |
| `sender_02__to__edge_02__mqtt` | sender_02 | edge_02 | `toxiproxy:18932` | `127.0.0.1:18932` | `mqtt-broker:1883` |
| `sender_03__to__edge_01__mqtt` | sender_03 | edge_01 | `toxiproxy:19031` | `127.0.0.1:19031` | `mqtt-broker:1883` |
| `sender_03__to__edge_02__mqtt` | sender_03 | edge_02 | `toxiproxy:19032` | `127.0.0.1:19032` | `mqtt-broker:1883` |

发送器获得 Scheduler 返回的 `edge_id` 后，必须选择对应代理入口，例如：

```yaml
edge_routes:
  edge_01:
    host: toxiproxy
    port: 18831
  edge_02:
    host: toxiproxy
    port: 18832
```

上例适用于 `sender_01`。`sender_02` 使用 18931/18932，`sender_03` 使用 19031/19032。宿主机进程把 `toxiproxy` 改为 `127.0.0.1`；其他 Compose 容器使用 Docker DNS 名 `toxiproxy`。

当前项目还配置了 12 条 HTTP 通信链路，覆盖 edge_01 和 edge_02。完整映射、代理端口及虚拟机上游覆盖方法见 `NETWORK_LINK_PORTS.md`。

每个入口虽然默认都连接同一个 Broker，但拥有独立 Proxy 和 toxic。只有业务按 Sender→Edge 路由使用不同入口，这些链路才在实际通信拓扑中真正独立。仅在内存里区分 `link_id`、但所有流量仍连接同一入口，不能形成独立网络链路。

发送器仍需自行实现 MQTT 自动重连、发送队列和断连缓存。网络模块不会替代这些业务容错逻辑。

## 7. 配置文件

所有 YAML 使用安全加载，模型默认拒绝未知字段。配置错误会在 Proxy 创建前使程序以非零退出码结束。

| 文件 | 主要内容 |
|---|---|
| `config/entities.yaml` | Sender/Edge 标识、每个 Sender 的起始端口、Edge Broker 上游 |
| `config/links.yaml` | Toxiproxy、Controller、可用性、笛卡尔积生成规则和补充显式链路 |
| `config/network_states.yaml` | 四种状态的参数范围和断连方式 |
| `config/transition_matrix.yaml` | Markov 状态顺序和转移矩阵 |
| `config/score.yaml` | 分项权重、归一化边界和失败策略 |
| `config/reporter.yaml` | Scheduler 地址、重试、队列和认证 |
| `config/plugins.yaml` | 插件启用/必需属性和日志轮转 |
| `config/experiment.yaml` | 实验标识、模式、种子、时长、时区和 fixed 配置 |

### 7.1 环境变量

| 变量 | 作用 |
|---|---|
| `NETWORK_SIMULATOR_ROOT` | 工程根目录，容器内为 `/app` |
| `NETWORK_CONFIG_DIR` | 配置目录 |
| `NETWORK_LOG_DIR` | 日志目录 |
| `TOXIPROXY_API_BASE_URL` | 覆盖 `links.yaml` 的管理 API 地址 |
| `NETWORK_SCHEDULER_URL` | 覆盖 Reporter 的 Scheduler URL |
| `NETWORK_REPORT_TOKEN` | `auth.mode: bearer` 时读取的 Token |
| `NETWORK_BIND_ADDRESS` | 所有 Compose 宿主机端口的绑定地址，默认 `127.0.0.1` |
| `NETWORK_API_HOST_PORT` | Compose 暴露的 API 宿主机端口 |
| `TZ` | 容器时区 |

Token 不写入 YAML、启动摘要或日志。`.env` 不应提交，仓库只提供 `.env.example`。`auth.mode: none` 不读取也不发送 Token；启用 Bearer 认证必须使用 HTTPS，并同时提供非空 `NETWORK_REPORT_TOKEN`，否则 Controller 会拒绝启动。

### 7.2 新增发送器或边缘节点

1. 在 `entities.yaml` 增加 Sender 或 Edge；
2. 给 Sender 分配不会与现有范围冲突的 `base_listen_port`；
3. 确认每个新 Sender 将占用连续的 `Edge 数量` 个端口；
4. 在 `docker-compose.yml` 的 Toxiproxy `ports` 中暴露宿主机需要访问的端口；
5. 重新执行 `docker compose up -d --build`；
6. 查询 `http://127.0.0.1:8474/proxies` 检查自动创建结果；
7. 查询 Fake/真实 Scheduler 缓存，确认 Reporter 中出现新链路。

笛卡尔积模式的端口计算为：

```text
advertised_port = sender.base_listen_port + edge 在 entities.yaml 中的零基序号
```

同一 Docker 网络内可使用未映射到宿主机的 Proxy 端口；宿主机业务必须同步更新 Compose 端口映射。

### 7.3 添加显式 HTTP 链路

在 `links.yaml` 的 `links` 中添加完整定义。显式链路可与自动生成的 MQTT 链路同时存在，例如：

```yaml
links:
  - link_id: edge_01__to__scheduler__http
    link_type: edge_to_scheduler
    sender_id: null
    edge_id: edge_01
    protocol: http
    proxy_name: edge_01__to__scheduler__http
    listen: "0.0.0.0:18011"
    advertised_host: toxiproxy
    advertised_port: 18011
    upstream: scheduler:8000
    seed_offset: 100
    latency_stream: upstream
    bandwidth_stream: upstream
    disconnect_stream: upstream
    disconnect_mode: auto
    report_enabled: true
```

`link_id`、`proxy_name`、`listen` 和 advertised endpoint 必须分别唯一。

### 7.4 修改状态参数

编辑 `network_states.yaml` 后重启 Controller。连接状态必须配置 latency、jitter、bandwidth 和 packet loss 范围；`DISCONNECTED` 的前三项必须为 `null`，packet loss 必须为 `100.0`。

这些范围只是初始实验值，应根据真实 `SensorPacket` 大小、发送频率、TCP 缓冲和宿主机性能校准。

### 7.5 带宽单位

配置、API、日志和 Reporter 中的 `bandwidth_kbps` 是 Kbps（千比特/秒）。Toxiproxy `bandwidth` toxic 的 `rate` 是整数 KB/s，严格执行：

```text
rate_KB_per_second = round_half_up(bandwidth_kbps / 8)
```

例如 `500 Kbps ÷ 8 = 62.5 KB/s`，半入取整后传给 Toxiproxy 的 `rate` 为 `63`。

**最小粒度限制**：Toxiproxy 只能表示整数 KB/s，即可表示的最小带宽为 `1 KB/s = 8 Kbps`。配置校验会直接拒绝 `1~7 Kbps` 的正数带宽（启动报错 `connected bandwidth must be at least 8 Kbps`），而不会静默抬高为 8 Kbps。若确需低于 8 Kbps 的极端带宽，需改用 `tc / netem` 等其他实现，本项目当前不支持。

### 7.6 修改转移矩阵

`transition_matrix.yaml` 的行列顺序必须与 `states` 完全一致，每行概率非负、有限且总和为 1。状态集合必须恰好为 `GOOD`、`MEDIUM`、`BAD`、`DISCONNECTED`。

相同 `global_seed` 和配置可重现实验。每条链路使用：

```text
link_seed = global_seed + seed_offset
```

没有显式 `seed_offset` 的显式链路使用基于 `link_id` 的稳定 SHA-256 偏移，不使用 Python 进程随机 hash。

### 7.7 Markov 与 fixed 模式

默认：

```yaml
experiment:
  mode: markov
```

固定状态实验可改为：

```yaml
experiment:
  mode: fixed

fixed_state:
  default: MEDIUM
  overrides:
    sender_01__to__edge_02__mqtt: BAD
```

`fixed` 仍使用同一参数采样、Toxiproxy、评分和 Reporter 流程，只是不发生状态转移。V3 不提供旧配置兼容层，也未实现 `scripted` 模式。

## 8. Link Reliability Score

评分从实际 `applied_parameters` 读取 latency、jitter、bandwidth、packet loss 和状态先验，分别归一化到 0～100，再按 `score.yaml` 加权。默认权重为 0.30、0.15、0.25、0.20、0.10，总和必须为 1。

评分含义：

- 它是用于比较候选链路的启发式调度指标，不是真实硬件故障概率；
- `DISCONNECTED` 或没有成功 applied 参数时固定为 0；
- 连续应用失败达到阈值后标记不可用，并按配置限分；
- 同状态下不同具体参数可以得到不同分数；
- Reporter 不重新计算分数，只读取 Tick 完成后的不可变快照。

### packet loss 的边界

当前固定的 Toxiproxy 2.12.0 不实际施加 packet loss。`packet_loss_percent` 由模型生成并参与评分、API、日志和 Reporter，但 `packet_loss_applied` 始终为 `false`。这项数据不能冒充业务侧实测丢包率。

真实断连由 `timeout` 或 `reset_peer` toxic 实现：`disconnect_mode: auto` 对 MQTT 使用 `reset_peer`，对 HTTP 使用 `timeout`。

## 9. Scheduler/Reporter 接入

真实 Scheduler 应提供（本项目的批量端点，见 `scheduler/api.py` 的 `update_network_report_batch`）：

```http
POST /scheduler/network-reports
Content-Type: application/json
Authorization: Bearer <token>   # 仅 bearer 模式
```

成功响应必须为 HTTP 200，并返回：

```json
{
  "accepted": true,
  "report_sequence": 123
}
```

Reporter 的关键行为：

- 每次只读取一个已完成 Tick 的完整快照；
- 默认只上报 `report_enabled: true` 的链路；
- 发送失败按配置重试，同一次重试保持相同 sequence 和报文；
- 有界队列满时执行 `drop_oldest`，优先保留新状态；
- 正常停止时先拒绝新快照，并在有界时间内排空队列；若出现 `shutdown_timeout`，取消在途请求、逐条审计被丢弃的剩余报告，然后安全关闭客户端；
- Scheduler 或 Reporter 失败不停止核心网络模拟；
- 敏感 Token 和认证头会被日志脱敏。

Bearer 认证必须使用 HTTPS；本地 Fake Scheduler 默认使用 `auth.mode: none`，因此可以使用 HTTP。

Scheduler 应以 `(experiment_id, reporter_id, report_sequence)` 做幂等处理，并按 `link_id` 缓存最新状态。业务调度还应实现 stale 规则：当前 V3 配置提供 `availability.stale_after_seconds`，但 Fake Scheduler 仅保存 `received_at_ns`，不替真实 Scheduler 自动执行过期判定。

本地 Fake Scheduler 查询：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/v1/network/cache
```

## 10. 只读 API

API 默认仅绑定宿主机 `127.0.0.1:8090`，不支持修改网络状态，也不启用 Swagger/OpenAPI 页面。

```bash
# 综合健康状态
curl http://127.0.0.1:8090/health

# 全部链路
curl http://127.0.0.1:8090/api/v1/network/links

# 按 Sender、Edge、状态和可用性过滤
curl "http://127.0.0.1:8090/api/v1/network/links?sender_id=sender_01&edge_id=edge_02"
curl "http://127.0.0.1:8090/api/v1/network/links?state=BAD&available=true"

# 单条链路；未知 link_id 返回 404
curl http://127.0.0.1:8090/api/v1/network/links/sender_01__to__edge_01__mqtt

# Controller 运行时摘要
curl http://127.0.0.1:8090/api/v1/network/runtime
```

API 只应暴露在受控实验网络中。业务调度应优先结合业务侧实测 RTT、吞吐、丢包和队列指标，而不是把调试 API 当作唯一依据。

## 11. 日志

| 文件 | 内容 |
|---|---|
| `controller.log` | 启停、Tick、配置摘要、插件和生命周期错误 |
| `state_updates.jsonl` | 每条链路每个 Tick 的 desired 状态、生成和应用结果 |
| `toxic_operations.jsonl` | Proxy/toxic 请求、HTTP 状态和脱敏错误 |
| `score_calculations.jsonl` | 分项、权重、总分、可用性和评分原因 |
| `reporter_operations.jsonl` | sequence、链路数、HTTP 状态、耗时、重试和丢弃 |

时间戳使用配置时区的 timezone-aware ISO 8601 和 `time.time_ns()` 纳秒值。文件按 `plugins.yaml` 中 `logging.max_bytes`、`backup_count` 轮转。JSONL 写入失败只使 Logger 健康状态降级，不终止模拟。

Compose 使用命名卷 `network-simulator-logs`，不是宿主机 `./logs` 绑定目录。查看日志：

```bash
docker compose logs -f network-controller
docker compose exec network-controller sh -c 'tail -n 20 /app/logs/state_updates.jsonl'
docker compose exec network-controller sh -c 'tail -n 20 /app/logs/score_calculations.jsonl'
docker compose exec network-controller sh -c 'tail -n 20 /app/logs/reporter_operations.jsonl'
```

导出日志到当前目录（Linux、macOS 和 Windows PowerShell 均可使用）：

```bash
docker compose cp network-controller:/app/logs ./network-simulator-logs
```

## 12. 本地运行与测试

创建 Python 3.11+ 环境：

```bash
python -m venv .venv
```

Linux/macOS：

```bash
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
pytest -q
./scripts/start-controller.sh
```

Windows PowerShell：

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
pytest -q
.\scripts\start-controller.ps1
```

测试分层：

```bash
pytest tests/unit -q
pytest tests/contract -q
pytest tests/integration -q
pytest -q
```

当前项目端口和链路配置验证：

```bash
pytest verification -q
```

普通测试使用 mock，不要求运行 Toxiproxy。真实效果集成测试要求单独启动可访问的 Toxiproxy，并设置：

```bash
TOXIPROXY_INTEGRATION_URL=http://127.0.0.1:8474 pytest tests/integration -q
```

测试还会临时使用默认端口 28661/28662 和本地 Echo 服务。Docker 场景下可按测试文件中的环境变量覆盖代理主机、上游主机和测试端口。

## 13. 停止和重复实验

前台运行按 `Ctrl+C`，后台运行执行：

```bash
docker compose down
```

Controller 收到 SIGINT/SIGTERM 后停止新 Tick，并按插件逆序退出。Reporter 先停止接收新快照并在有界时间内排空队列；超时后以 `shutdown_timeout` 记录剩余丢弃项。若 `clear_toxics_on_exit: true`，正常退出时清理动态 toxic；强制终止可能来不及排空或清理，但下次启动会幂等确认 Proxy 并修复 toxic。

`duration_seconds: 0` 表示持续运行，大于 0 表示固定时长实验。`network-controller` 不自动重启，避免固定时长实验结束后从同一随机序列起点重新运行并混写日志。

## 14. 常见问题

### Toxiproxy API 无法连接

运行 `docker compose ps`、`docker compose logs toxiproxy` 和 `curl http://127.0.0.1:8474/version`。若 Docker CLI 本身无法连接 daemon，先启动 Docker Desktop/Engine。

### Proxy 端口冲突

检查 `entities.yaml` 的 Sender 端口段、显式链路的 `listen`/`advertised_port` 和 Compose 端口映射。配置加载器会拒绝重复入口，但宿主机端口也可能被其他进程占用。

### MQTT 能连接但不受影响

确认客户端连接的是该 Sender→Edge 对应的 `toxiproxy:<port>`，而不是 `mqtt-broker:1883`。再查询 `/proxies`，检查该 Proxy 是否存在及 toxic 是否已创建。

### Reporter 一直失败

检查 `NETWORK_SCHEDULER_URL`、Scheduler 日志、HTTP 200 响应格式、超时配置和 Bearer Token。查看 `reporter_operations.jsonl`；Reporter 失败不会停止模拟。

### Scheduler 状态过期

真实 Scheduler 应比较报告的 `generated_at_ns`/本地接收时间，并使用 stale 阈值停止采用过期链路。Fake Scheduler 只缓存和展示时间，不自动改变 `available`。

### score 全为 0

常见原因是尚无成功 applied 参数、链路处于 `DISCONNECTED`、评分阶段失败，或 Toxiproxy 应用连续失败。联合查看链路 API、`score_calculations.jsonl` 和 `toxic_operations.jsonl`。

### bandwidth 单位不符合预期

确认 YAML 使用 Kbps，Toxiproxy 使用 KB/s，代码严格执行 Kbps ÷ 8 后半入取整。TCP 缓冲、容器调度和消息大小仍会使实际吞吐与理论值存在差异。

### Docker Desktop 网络问题

容器内使用服务名 `toxiproxy`、`mqtt-broker`、`scheduler`；宿主机使用 `127.0.0.1` 和映射端口。不要在容器内用 `127.0.0.1` 访问另一个容器，因为该地址只指向当前容器自身。

### 链路实际未独立

确认每个 Sender→Edge 组合具有独立 Proxy 端口，并且业务确实根据目标 Edge 选择对应入口。不同逻辑 ID 如果共享同一实际入口，流量仍不独立。

### 日志目录权限不足

容器以非 root 用户运行，默认命名卷可写。自定义绑定目录时需授予容器 UID/GID 10001 写权限；日志失败会降级但不会终止模拟。

## 15. 当前限制

- Toxiproxy 主要工作在 TCP 流层，不模拟无线物理层或逐包路由；
- 当前 packet loss 只进入模型、评分、日志和上报，没有真实 toxic；
- MQTT 独立 Sender→Edge 链路依赖业务按目标 Edge 选择不同代理入口；
- Score 是启发式指标，不是真实故障概率或业务端到端测量；
- 状态生成和评分按确定性顺序执行；Toxiproxy 应用阶段使用由 `max_parallel_updates` 限制的线程池；
- `availability.stale_after_seconds` 供真实 Scheduler 制定 stale 规则，Fake Scheduler 不自动执行；
- 支持 `markov` 和 `fixed`，不支持 `scripted`、metrics、checkpoint；
- 第一阶段没有图形化管理页面，API 只读；
- Toxiproxy 多个管理请求之间不是事务，失败时保留最后一次成功 applied 状态并在下一 Tick 重试；
- 当前 Compose 是本地实验环境，默认仅绑定 `127.0.0.1`；Mosquitto 允许匿名访问，API 也没有鉴权，即使手动改为 `0.0.0.0` 也不适合直接暴露到公网或生产网络。

## 16. 验收检查

部署后可按以下顺序验收：

1. `docker compose config --quiet` 成功；
2. 四个服务启动，Toxiproxy、Broker、Scheduler 健康；
3. `/proxies` 出现 18 个默认独立 Proxy；
4. `/health` 的 `last_tick` 持续递增；
5. `/api/v1/network/links` 返回 desired、applied、score 和 available；
6. Fake Scheduler 缓存出现同样 18 条链路；
7. latency、bandwidth、断连和恢复真实影响经 Proxy 的 TCP 流量；
8. 停止 Scheduler 后网络状态仍继续更新；
9. 日志卷中生成五类日志；
10. `pytest -q` 全部通过；真实 Toxiproxy 条件具备时再运行集成效果测试。

## 17. 拓扑与监控语义（P3 补充说明）

### 17.1 ENV-2：compose `scheduler`（stub）与真实 Scheduler 的区别

当前环境下存在两个“接收报告”的对象，容易误以为“容器 Up = 真实链路正常”：

- compose 里的 **`scheduler`（stub / Fake Scheduler）**：仅用于 network simulator 的独立联调，`docker-compose.yml` 中绑定 `127.0.0.1:8000`，不做真实调度决策，只缓存/展示报告。**ENV-2：该服务已通过 `profiles: [standalone]` 隔离，默认完整栈启动不会拉起它**，仅当显式 `docker compose --profile standalone up -d` 时才运行（container_name `scheduler-stub`）。
- **真实 Scheduler**：完整系统运行时 Reporter 的真实目标。本模块 Controller 实际把报告发给谁，取决于 `NETWORK_SCHEDULER_URL`（环境变量）或 `config/reporter.yaml` 的 `scheduler_url`，**不是**看 compose 里哪个容器在运行。Controller 启动成功会打印一次 `network reporter configured: target=<url>`。

**两种运行模式：**

- **模式 A（Standalone Network Simulator）**：`Controller + Toxiproxy + Mosquitto + Scheduler Stub`，用于独立测试网络模拟与 Reporter 请求。
  ```bash
  docker compose --profile standalone up -d --build
  ```
  stub 只验证 Reporter 网络请求 / 缓存展示报告，**不能代表完整 Scheduler**。
- **模式 B（完整项目）**：`Controller + Toxiproxy + Mosquitto + 真实 Scheduler + Edge + Cloud`。Network Reporter 的权威目标是 `reporter.yaml` / 环境变量最终解析出的真实 Scheduler URL（默认 `host.docker.internal:8003/scheduler/network-reports`），默认启动不包含 stub。

当前默认配置（以仓库内实际 YAML 为准）：

- standalone stub：`127.0.0.1:8000`（仅 `--profile standalone` 启动）
- 真实 Scheduler：`host.docker.internal:8003/scheduler/network-reports`

因此：

- 仅启动 compose（stub 正常）并不代表真实链路已通；真实 Scheduler 取决于 8003 是否可达；
- “真实 Scheduler 不可达 → Controller 的 Reporter health 降级 → Docker health `unhealthy`”是**正确的故障可观测行为**，不代表任何已知告警（例如 AUD-13）又坏了。

### 17.2 ENV-2：LinkSnapshot 与 Edge `network_to_scheduler` 的数据源边界

- **Network Simulator 的 `LinkSnapshot`**：当前模拟场景下 Scheduler 网络决策的主数据源（`sender → edge` 链路笛卡尔积，含 apply 结果与评分）。
- **Edge 侧 `network_to_scheduler`**：Edge 状态报告里的网络观测/诊断数据，当前**不直接参与** Assignment 调度排序。

原因：两者不是同一条物理/逻辑链路；`measurement_status=FAILED` 表示“测量失败”，不等于“链路实际断开”。强行混用会造成链路语义错配或双数据源冲突。未来真实部署若移除 Network Simulator，再单独设计数据源切换/融合策略。

### 17.3 NET-5：`skipped` / 未支持链路的监控语义

Scheduler 目前只消费 `sender → edge` 的 MQTT 链路，其他方向的 HTTP 链路在上报中被标记为 `skipped`。这是**现有设计边界**，不是错误。因此：

- `skipped_count > 0` 不直接作为 unhealthy 条件；
- `unsupported/skipped` 若符合配置预期，只作为信息统计；
- 告警应聚焦于：`transport delivery failure`、batch `accepted=false`、`rejected_count > 0`、以及 `accepted_count < 当前配置期望支持数`。

“期望支持数”应从当前 `links`/拓扑/配置推导，**不要硬编码**成固定数字。例如：默认拓扑下 18 条链路中有 12 条 HTTP 被 `skipped`、6 条 MQTT 被接受，**仅当当前拓扑恰好如此时**这个示例成立；今后 sender/edge 数量变化后应立即随之变化。

### 17.4 NET-2：`report_sequence` 语义（已知限制）

当前 `report_sequence` 是**纳秒时间戳式的单调值**（默认取 `time.time_ns()` 作为起点，之后自增），**不是**从 1 开始的连续整数序号。因此 Scheduler **不能**通过 `seq > last + 1` 判断“缺报”，否则在 1、2、3 等合法序号之外会大量误报。

本模块本轮**不实现** `last + 1` 缺口检测。若未来确需缺口检测，应另行设计：真正的单调 counter + reporter instance/restart identity，或基于报告周期的时间缺口观测。本轮不改消息合同。

> 报告丢失的观测请使用 Reporter 的 `dropped_report_count`（NET-3）：一次报告经 Transport 内部所有重试后仍最终未交付才 +1，恢复成功不清零，表示为累计历史。
