# 前端（frontend/）

智能运维协作平台的本地 Web 前端。纯静态 HTML/CSS/JS（无框架、无构建步骤），
配套一个 Python 网关服务器解决浏览器跨域与 MQTT 实时推送问题。

## 页面一览

| 页面 | 文件 | 功能 | 数据来源 |
|---|---|---|---|
| 总览大屏 | index.html | 实时诊断结果 / 维护建议 / 数据包推送 + 四服务健康条 | MQTT 实时流 + /health 轮询 |
| 边缘节点 | edge-health.html | Edge 节点全量健康指标（模型/队列/线程/Outbox/链路） | GET /health 每 5 秒 |
| 诊断演示 | diagnosis-demo.html | 构造正常/故障数据包，同步调用边缘推理，看完整结果 | POST /edge/infer |
| 调度拓扑 | topology.html | 系统拓扑图（在线状态着色）、路由策略、网络链路质量 | /health + /scheduler/routing-policy + 网络模拟器 |
| 设备仲裁 | arbitration.html | 按冲突 ID 查询边缘 vs 云端对比与最终裁定 | /cloud/device-arbitration/* |
| 全局分析 | analysis.html | 触发/读取设备健康全局分析报告 | /cloud/global-analysis* |

## 启动步骤

前置条件：后端已按项目主 README 启动
（网络模拟器 + MQTT Broker + Edge ×2 + Scheduler + Cloud，至少要有 Edge 服务可用）。

1. 打开一个新的终端窗口，激活 moment 环境：

   ```
   conda activate moment
   ```

   > 网关依赖 paho-mqtt（requirements-moment.txt 已包含），用于桥接 MQTT 消息。

2. 进入前端目录并启动网关：

   ```
   cd d:\desktop\Intelligent-Maintenance-Collaboration\cloud_edge_project\frontend
   python server.py
   ```

   看到如下输出即启动成功（默认端口 8088，如被占用可 `python server.py --port 8099`）：

   ```
   智能运维协作平台 前端已启动
   地址:   http://127.0.0.1:8088
   ```

3. 浏览器打开 <http://127.0.0.1:8088>

## 网关做了什么（为什么需要它）

浏览器有同源安全策略，而后端各服务（8001/8002/8003/8004）没有开 CORS，
前端页面直接调用会被浏览器拦截。同时 MQTT 是 TCP 协议，浏览器无法直连。

`server.py` 一个进程解决两件事：

| 网关路径 | 转发目标 | 说明 |
|---|---|---|
| `/api/edge01/*` | http://127.0.0.1:8001/* | Edge 节点 01 |
| `/api/edge02/*` | http://127.0.0.1:8002/* | Edge 节点 02 |
| `/api/scheduler/*` | http://127.0.0.1:8003/* | 调度器 |
| `/api/cloud/*` | http://127.0.0.1:8004/* | 云端服务 |
| `/api/network/*` | http://127.0.0.1:8090/* | 网络模拟器（链路质量） |
| `/api/events` | MQTT 1883 → SSE | 订阅 `summary/device-results`、`summary/suggestions`、`edge/+/input`，实时推给浏览器 |

因此**不修改任何后端代码和 Docker 配置**即可接入。

## 常见问题

- **总览大屏右上角"MQTT 实时流未连接"**：
  网关启动时会自动连 127.0.0.1:1883，连不上每 5 秒重试。
  请确认网络模拟器栈（mqtt-broker 容器）已启动。HTTP 轮询部分不受影响。

- **服务卡片显示"不可达"**：对应后端服务没启动或端口不符，
  用 `docker compose -f compose.multi-edge.yml ps`（Edge）/宿主机进程（Scheduler/Cloud）确认。

- **实时流没有消息**：实时数据由 Sender 产生，需要 Sender 正在发数据；
  或到「诊断演示」页手动触发一次单包推理。

## 目录结构

```
frontend/
├── server.py            # 网关：静态托管 + API 代理 + MQTT→SSE 桥接
├── index.html           # 总览大屏
├── edge-health.html     # 边缘节点健康
├── diagnosis-demo.html  # 单包诊断演示
├── topology.html        # 调度拓扑
├── arbitration.html     # 设备仲裁
├── analysis.html        # 全局分析
├── css/main.css         # 共享样式（深色监控主题）
└── js/common.js         # 共享逻辑（导航/API客户端/SSE/格式化）
```
