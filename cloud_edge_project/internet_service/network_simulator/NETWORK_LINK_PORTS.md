# Network Simulator 链路端口表

## 组件默认端口

| 组件 | 默认端口 |
|---|---:|
| Edge Service（edge_01，宿主机映射） | 8001 |
| Edge Service（edge_02，宿主机映射） | 8002 |
| Scheduler Service | 8003 |
| Cloud Service | 8004 |
| Edge 建议 LLM（宿主机 llama.cpp 0.5B，不经网络模拟） | 8005 |
| 云端模型更新 LLM（宿主机 llama.cpp 3B，不经网络模拟） | 6006 |
| MQTT Broker | 1883 |
| Network API | 8090 |
| Toxiproxy API | 8474 |

## Sender 到 Edge MQTT 链路

| 链路 | 宿主机代理地址 | 容器内代理地址 | 上游 |
|---|---|---|---|
| sender_01 → edge_01 | `127.0.0.1:18831` | `toxiproxy:18831` | `mqtt-broker:1883` |
| sender_01 → edge_02 | `127.0.0.1:18832` | `toxiproxy:18832` | `mqtt-broker:1883` |
| sender_02 → edge_01 | `127.0.0.1:18931` | `toxiproxy:18931` | `mqtt-broker:1883` |
| sender_02 → edge_02 | `127.0.0.1:18932` | `toxiproxy:18932` | `mqtt-broker:1883` |
| sender_03 → edge_01 | `127.0.0.1:19031` | `toxiproxy:19031` | `mqtt-broker:1883` |
| sender_03 → edge_02 | `127.0.0.1:19032` | `toxiproxy:19032` | `mqtt-broker:1883` |

## HTTP 链路

| 链路 | 宿主机代理地址 | 容器内代理地址 | 实际上游 |
|---|---|---|---|
| sender_01 → Scheduler | `127.0.0.1:18031` | `toxiproxy:18031` | `host.docker.internal:8003` |
| sender_02 → Scheduler | `127.0.0.1:18032` | `toxiproxy:18032` | `host.docker.internal:8003` |
| sender_03 → Scheduler | `127.0.0.1:18033` | `toxiproxy:18033` | `host.docker.internal:8003` |
| edge_01 → Scheduler | `127.0.0.1:18011` | `toxiproxy:18011` | `host.docker.internal:8003` |
| Scheduler → edge_01 | `127.0.0.1:18042` | `toxiproxy:18042` | `host.docker.internal:8001` |
| edge_01 → Cloud | `127.0.0.1:18021` | `toxiproxy:18021` | `host.docker.internal:8004` |
| Cloud → edge_01（预留，当前无消费者） | `127.0.0.1:18044` | `toxiproxy:18044` | `host.docker.internal:8001` |
| Cloud → Scheduler | `127.0.0.1:18045` | `toxiproxy:18045` | `host.docker.internal:8003` |
| edge_02 → Scheduler | `127.0.0.1:18051` | `toxiproxy:18051` | `host.docker.internal:8003` |
| Scheduler → edge_02 | `127.0.0.1:18052` | `toxiproxy:18052` | `host.docker.internal:8002` |
| edge_02 → Cloud | `127.0.0.1:18053` | `toxiproxy:18053` | `host.docker.internal:8004` |
| Cloud → edge_02（预留，当前无消费者） | `127.0.0.1:18054` | `toxiproxy:18054` | `host.docker.internal:8002` |

## 业务接入规则

宿主机上的业务进程使用 `127.0.0.1:<代理端口>`，Compose 容器内的业务进程使用 `toxiproxy:<代理端口>`。业务如果继续直接访问 `8001`、`8002`、`8003` 或 `8004`，流量不会经过网络模拟链路。

Edge 建议 LLM（宿主机 `8005`）由 Edge 容器经 `host.docker.internal:8005` 直连调用，云端模型更新 LLM（宿主机 `6006`）由 Cloud 直接调用；两者均不经过网络模拟，也不占用本表中的任何代理端口，且互不相干（详见启动手册第 6 节）。

Edge Status Reporter 经过网络模拟时，宿主机启动前设置：

```powershell
$env:EDGE_STATUS_SCHEDULER_URL = "http://127.0.0.1:18011/scheduler/edge-nodes/status"
$env:EDGE_STATUS_CLOUD_URL = "http://127.0.0.1:18021/cloud/edge-status"
```

edge_02 使用：

```powershell
$env:EDGE_NODE_ID = "edge_02"
$env:EDGE_STATUS_SCHEDULER_URL = "http://127.0.0.1:18051/scheduler/edge-nodes/status"
$env:EDGE_STATUS_CLOUD_URL = "http://127.0.0.1:18053/cloud/edge-status"
```

虚拟机部署时可在网络模块 `.env` 中覆盖显式 HTTP 链路的实际上游：

```dotenv
NETWORK_BIND_ADDRESS=0.0.0.0
NETWORK_LINK_UPSTREAMS_JSON={"sender_01__to__scheduler__http":"192.168.56.10:8003","sender_02__to__scheduler__http":"192.168.56.10:8003","sender_03__to__scheduler__http":"192.168.56.10:8003","edge_01__to__scheduler__http":"192.168.56.10:8003","scheduler__to__edge_01__http":"192.168.56.21:8001","edge_01__to__cloud__http":"192.168.56.11:8004","cloud__to__edge_01__http":"192.168.56.21:8001","cloud__to__scheduler__http":"192.168.56.10:8003","edge_02__to__scheduler__http":"192.168.56.10:8003","scheduler__to__edge_02__http":"192.168.56.22:8001","edge_02__to__cloud__http":"192.168.56.11:8004","cloud__to__edge_02__http":"192.168.56.22:8001"}
```

映射的键必须是 `config/links.yaml` 中已存在的 `link_id`。未覆盖的链路继续使用本地默认上游。

Sender 调度请求应按发送器分别使用：

```text
sender_01: http://127.0.0.1:18031/scheduler/decide
sender_02: http://127.0.0.1:18032/scheduler/decide
sender_03: http://127.0.0.1:18033/scheduler/decide
```

当前未配置以下链路：

- Scheduler → Sender：Sender 没有 HTTP 服务端口。
- Scheduler → Cloud：当前 Scheduler 没有对应直接调用。
- Network Reporter → Scheduler：通过正式 `/scheduler/network-reports/{link_id}` 适配入口上报。
