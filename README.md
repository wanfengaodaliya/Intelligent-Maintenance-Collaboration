# Intelligent Maintenance Collaboration

面向智能运维场景的云边协同项目，包含 Sender、Edge、Scheduler、Cloud 与 Network Simulator。当前分支集成了 Edge Status Reporter，边缘节点启动后会默认向 Scheduler 和 Cloud 周期上报同一份节点状态快照。

## 项目结构

- `cloud_edge_project/edge_service/`：边缘接入、推理、状态采集与上报。
- `cloud_edge_project/scheduler/`：节点注册、状态接收与任务调度。
- `cloud_edge_project/cloud_service/`：云端推理、状态接收与全局处理。
- `cloud_edge_project/sender_module/`：传感器数据发送端。
- `cloud_edge_project/internet_service/network_simulator/`：基于 Toxiproxy 的网络链路模拟。
- `cloud_edge_project/docs/Edge_Status_Reporter_完整测试流程.md`：完整配置、运行和联调流程。

## 默认端口

| 组件 | 默认地址 |
|---|---|
| Edge HTTP | `127.0.0.1:8001` |
| Scheduler HTTP | `127.0.0.1:8003` |
| Cloud HTTP | `127.0.0.1:8004` |
| MQTT Broker | `127.0.0.1:1883` |
| Network API | `127.0.0.1:8090` |
| Toxiproxy API | `127.0.0.1:8474` |

以上是各服务的监听端口。网络模拟模式下，业务服务之间的出站请求默认经过 Toxiproxy 代理端口（如 Sender→Scheduler `18031/18032/18033`、Edge→Scheduler `18011`、Edge→Cloud `18021`、Cloud→Scheduler `18045`、Scheduler→Edge `18042`），完整链路见 `cloud_edge_project/internet_service/network_simulator/NETWORK_LINK_PORTS.md`。

## 安装与启动

H5 边缘诊断服务使用 Conda 的 `moment` 环境（Python 3.11+）。H5 权重和冻结归一化参数已随仓库分发，并镜像到 [Hugging Face](https://huggingface.co/wanfengaodaliya/intelligent-maintenance-distilled-h5)。

当前单机完整系统统一使用仓库根目录的 `start_project.ps1` 启动。脚本会检查 Docker、Conda、MOMENT、H5 和可选 LLM 模型，启动网络模拟器及 Edge、Scheduler、Cloud，并执行健康检查：

```powershell
.\start_project.ps1
# 如果暂时不启动可选 LLM：
.\start_project.ps1 -SkipLLM
```

启动后可检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
Invoke-RestMethod http://127.0.0.1:8003/health
Invoke-RestMethod http://127.0.0.1:8004/health
```

旧版 `start_all.py`、Consistency 和 Log 服务已从当前启动链路移除，不再作为项目入口或运行依赖。网络模拟器细节见 `cloud_edge_project/internet_service/network_simulator/README.md`。

## 测试

```powershell
cd cloud_edge_project
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
$testTemp = Join-Path $env:TEMP ("edge-status-pytest-" + [guid]::NewGuid().ToString("N"))
python -m pytest -p no:cacheprovider -W error -q --basetemp $testTemp
```

更完整的单节点、多节点、接口和网络链路测试步骤见 `cloud_edge_project/docs/Edge_Status_Reporter_完整测试流程.md`。

## 当前限制

- Sender 当前根据 Scheduler 返回的 MQTT topic 投递，但尚未根据目标 Edge 动态切换不同的 Toxiproxy MQTT 入口。
- Network Simulator 的批量 Reporter 默认向真实 Scheduler 的 `POST /scheduler/network-reports` 上报链路快照；接入时以 `internet_service/network_simulator/config/reporter.yaml` 解析出的 URL 为准。
- 项目尚未声明开源许可证；公开发布前请由仓库所有者选择并添加合适的 `LICENSE`。
