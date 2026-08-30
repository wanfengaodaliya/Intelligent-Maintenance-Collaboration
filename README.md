# Intelligent Maintenance Collaboration

面向智能运维场景的云边协同项目，包含 Sender、Edge、Summary、Scheduler、Cloud 与 Network Simulator。当前分支集成了 Edge Status Reporter，边缘节点启动后会默认向 Scheduler 和 Cloud 周期上报同一份节点状态快照。

## 项目结构

- `cloud_edge_project/edge_service/`：边缘接入、推理、状态采集与上报。
- `cloud_edge_project/scheduler/`：节点注册、状态接收与任务调度。
- `cloud_edge_project/cloud_service/`：云端推理、状态接收与全局处理。
- `cloud_edge_project/summary_service/`：跨 Edge 结果汇总、最终维护建议翻译与发布。
- `cloud_edge_project/sender_module/`：传感器数据发送端。
- `cloud_edge_project/internet_service/network_simulator/`：基于 Toxiproxy 的网络链路模拟。
- `cloud_edge_project/docs/Edge_Status_Reporter_完整测试流程.md`：完整配置、运行和联调流程。

## 默认端口

| 组件 | 默认地址 |
|---|---|
| Edge HTTP | `127.0.0.1:8001` |
| Scheduler HTTP | `127.0.0.1:8003` |
| Cloud HTTP | `127.0.0.1:8004` |
| Summary HTTP | `127.0.0.1:8006` |
| Summary 建议 LLM | `127.0.0.1:8005` |
| MQTT Broker | `127.0.0.1:1883` |
| Network API | `127.0.0.1:8090` |
| Toxiproxy API | `127.0.0.1:8474` |

以上是各服务的监听端口。网络模拟模式下，业务服务之间的出站请求默认经过 Toxiproxy 代理端口（如 Sender→Scheduler `18031/18032/18033`、Edge→Scheduler `18011`、Edge→Cloud `18021`、Cloud→Scheduler `18045`、Scheduler→Edge `18042`），完整链路见 `cloud_edge_project/internet_service/network_simulator/NETWORK_LINK_PORTS.md`。

## 安装与启动

H5 边缘诊断服务使用 Conda 的 `moment` 环境（Python 3.11+）。H5 权重和冻结归一化参数已随仓库分发，并镜像到 [Hugging Face](https://huggingface.co/wanfengaodaliya/intelligent-maintenance-distilled-h5)。

当前单机完整系统统一使用仓库根目录的 `start_project.ps1` 启动。脚本会检查 Docker、Conda、MOMENT、H5 和可选 LLM 模型，启动网络模拟器及 Edge、Summary、Scheduler、Cloud，并执行健康检查：

```powershell
Copy-Item .env.example .env
# 按新机器的目录、IP、端口和节点信息修改 .env，然后先校验配置：
.\start_project.ps1 -CheckConfig
# 正式启动：
.\start_project.ps1
# 如果暂时不启动可选 LLM（本机无 llama-server / Qwen 时推荐）：
.\start_project.ps1 -SkipLLM
```

Edge 镜像策略（默认不重建、不下载）：

- 启动 Edge 时默认以 `--no-build` 复用本机已有的
  `cloud-edge/edge-service:latest`；只要镜像存在，就不会触发 pip install、
  PyTorch/CUDA 下载或镜像重建。
- 仅当你显式传 `-RebuildEdgeImage` 时，Edge stage 才会执行 `docker compose
  ... up -d --build` 从当前源码重建镜像。重建前请先确认网络与编译资源可用。
- 若镜像不存在，脚本会给出可复制的构建命令并退出，不会自动 `pull` 或下载。
- 若镜像内的 `EDGE_BUILD_REVISION` 与当前源码 revision 不一致，脚本会打印警告
  （可选择 `.\start_project.ps1 -RebuildEdgeImage` 有意重建），但不会静默运行
  或自动构建。

端口占用与进程归属（默认“发现占用即报错并退出”）：

- 启动 Scheduler / Cloud / Summary（8003/8004/8006）以及 Edge（8001/8002）前
  脚本会先探测端口。默认情况下，只要端口被占用就打印 PID、进程名和安全的处理
  提示并退出，绝不杀死无法确认归属于本项目的进程。
- 若占用进程是上一次遗留的本项目服务，可显式传 `-RestartHostServices` 让脚本
  先校验进程确实属于本项目（按模块名匹配）再停止并重启。

各开关汇总：

| 参数 | 作用 |
| --- | --- |
| `-CheckConfig` | 只读预检：Docker、环境变量、镜像、模型、端口占用，不起服务 |
| `-RebuildEdgeImage` | 允许 Edge 从源码 `--build` 重建镜像（默认禁止） |
| `-SkipLLM` | 跳过可选 LLM（Summary 建议 / Cloud 模型更新），核心链路以固定模板运行 |
| `-SkipCloudUpdateLLM` | 仅关闭 Cloud 模型更新的 LLM 建议，核心链路与 Summary 不受影响 |
| `-RestartHostServices` | 只允许停止并重启用占用端口的本项目遗留进程（按模块校验归属） |

Sender 数量、身份和链路使用 `cloud_edge_project/sender_module/config/local.json`；
Network 的链路监听/上游与 Reporter 周期使用
`cloud_edge_project/internet_service/network_simulator/config/`。这些都是部署配置，
更换环境无需修改 Python 源码。

启动后可检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
Invoke-RestMethod http://127.0.0.1:8003/health
Invoke-RestMethod http://127.0.0.1:8004/health
Invoke-RestMethod http://127.0.0.1:8006/health
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

- Network Simulator 的批量 Reporter 默认向真实 Scheduler 的 `POST /scheduler/network-reports` 上报链路快照；接入时以 `internet_service/network_simulator/config/reporter.yaml` 解析出的 URL 为准。
- 项目尚未声明开源许可证；公开发布前请由仓库所有者选择并添加合适的 `LICENSE`。
