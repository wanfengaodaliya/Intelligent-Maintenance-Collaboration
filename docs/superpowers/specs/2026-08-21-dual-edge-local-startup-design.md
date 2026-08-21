# 本机双边缘节点启动设计

**目标：** 让 `start_project.ps1` 在单台开发机上按依赖顺序启动网络模拟器、宿主机服务与 `edge_01`、`edge_02` 两个 Edge 容器，使调度器可将三个 Sender 的任务分发到两个节点。

## 范围

- 正式运行模式为双 Edge 容器：`compose.multi-edge.yml` 是唯一正式 Edge Compose（旧 `compose.network-sim.yml` 已删除），启动命令固定 `docker compose -f compose.multi-edge.yml up -d --no-build`；宿主机 Edge 仅作为显式开发模式，且启动前必须停掉容器 Edge，两者不得同时运行。
- 保留网络模拟器既有的两条边缘通道：
  - `edge_01`：宿主机映射 `8001`，出站代理 `18011`（Scheduler）和 `18021`（Cloud），调度回调代理 `18042`，MQTT 主题 `edge/edge_01/input`。
  - `edge_02`：宿主机映射 `8002`，出站代理 `18051`（Scheduler）和 `18053`（Cloud），调度回调代理 `18052`，MQTT 主题 `edge/edge_02/input`。
- Scheduler 通过默认节点注册（`node_registry.py`）同时注册两个节点及各自的回调地址 `18042/18052` 和主题，宿主机启动无需显式设置 `SCHEDULER_EDGE_NODES_JSON`。
- 建议 LLM 服务固定为 `8005`；两个 Edge 容器的 LLM 调用地址为 `http://host.docker.internal:8005`。云端模型更新 LLM（llama.cpp 3B）固定为 `6006`，与边缘建议 LLM 互不相干（见启动手册第 6 节）。启动脚本传 `-SkipLLM` 时同时跳过两者，并向容器注入 `EDGE_SUGGESTION_LLM_ENABLED=false` 显式禁用建议调用。
- 两个 Edge 容器使用独立数据卷（`edge_01_data`、`edge_02_data`）：各自的 SQLite 数据库、路由错误日志、云复核缓存、原始样本目录和模型更新状态文件互不共享。
- 启动脚本按四个阶段执行，每阶段带健康门（每 2 秒轮询、默认 180 秒超时）：
  1. 网络模拟器（project name 固定 `network_simulator`）：toxiproxy / mqtt-broker / network-controller 全部 healthy，且 Toxiproxy 中存在本项目所需代理；
  2. 宿主机 Scheduler `8003` 与 Cloud `8004`（`moment_light_adapt` 后端且模型已加载）HTTP ready；
  3. 两个 LLM `/v1/models` ready（边缘建议 `8005` + 云端模型更新 `6006`；`-SkipLLM` 时显式禁用）；
  4. 两个 Edge 容器启动后轮询 `/health/ready`（Docker HEALTHCHECK 仅代表存活）。全部通过后才允许 Sender 发送。
- 任一健康门失败时打印对应容器/进程状态与最近日志并终止后续阶段，不依赖 `restart: unless-stopped` 充当启动排序。

## 不在本次范围内

- 不修改网络模拟器的链路、端口、MQTT Broker 或 Sender 配置（`cloud__to__edge_*` 的 `18044/18054` 为预留链路，当前无运行时消费者）。
- 不处理 Cloud 对诊断结果返回 HTTP 400 的独立接口契约问题。
- 不改变 H5 模型文件或模型版本；自动模型更新（Poller）默认关闭，仅通过 `compose.model-update.yml` overlay 且挂载 Ed25519 公钥后启用。
- 正式 Compose 的拓扑地址（MQTT、Scheduler、Cloud、建议 LLM）固定取值，不接受 `.env` 覆盖；其他拓扑只能通过独立 Compose override 文件实现。

## 成功标准

1. 运行启动脚本后，Scheduler 的健康接口显示两个已注册且在线的边缘节点，且启动过程不创建任何宿主机 Edge 进程。
2. `edge_01` 与 `edge_02` 分别在 `8001`、`8002` 通过 `/health/ready`（`ready : True`），`/health` 返回各自 `node_id` 且 MQTT 已连接。
3. 网络模拟器、Scheduler 或 Cloud 未就绪时，Edge 容器阶段不会执行。
4. 未传 `-SkipLLM` 时，边缘建议 LLM 在 `8005`、云端模型更新 LLM 在 `6006` 可用，两个 Edge 的配置均指向 `8005`；传 `-SkipLLM` 时两个容器 `EDGE_SUGGESTION_LLM_ENABLED=false`。
5. 双节点运行时不共享同一个 SQLite 数据库或其他可写运行目录。
