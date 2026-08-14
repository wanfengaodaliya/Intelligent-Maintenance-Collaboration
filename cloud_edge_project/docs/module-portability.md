# 网络与边缘感知模块可迁移化说明

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
| Consistency 服务 | `8005` |
| Log 服务 | `8006` |
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
