# Intelligent Maintenance Collaboration - Scheduler Service

本分支仅包含 `cloud_edge_project` 的可运行源码与必要配置；不包含测试、隐藏文件、虚拟环境、缓存、数据库、日志和运行产物。

## 模块职责

- `scheduler/`：任务分配、包级路由、节点/链路状态、云复核延迟任务和上传结果持久化。
- `edge_service/`：边缘推理、任务接入、原始包本地保存、云复核执行，以及向调度器提交包级结果。
- `cloud_service/`：云端复核、上下文聚合、设备仲裁、全局分析与模型更新。
- `sender_module/`：发送端数据包构造、调度请求和 MQTT 发布。
- `internet_service/network_simulator/`：网络状态、链路、评分和 Toxiproxy 仿真。

## 本次调度器新增/增强功能

1. `POST /scheduler/packet-route`：接收边缘单包结果，严格校验身份、时间、结果和 `task_complexity = 1 - confidence`。
2. 三条包级路径：高置信 `DIRECT_FINAL_TO_SUMMARY`；低置信且云/网络可用 `CLOUD_REVIEW_NOW`；云不可用、拥塞或弱网时 `EDGE_PROVISIONAL_AND_DEFER_CLOUD`。
3. 云状态和边云链路状态：`/scheduler/cloud-nodes/status`、`/scheduler/link-snapshots` 用于判断云在线、模型就绪、队列长度、吞吐、RTT 和丢包。
4. 延迟云复核：持久化待处理任务，重试前重新判断云和网络条件；通过 `/edge/cloud-review-tasks` 仅向原边缘节点发送控制指令。
5. 云结果回报：`/scheduler/cloud-upload-results` 保存成功、可重试失败和永久失败状态。
6. 正式边缘入口：`POST /edge/packets` 保留简化发送包格式，先把原始包及边缘结果保存在本地，再调用 `/scheduler/packet-route`；原始数据不经过调度器。

## 当前边界

调度规则和本地接口已实现；生产运行仍需要真实在线边缘状态、发送端接入、云节点/链路状态上报和一次端到端联调。模型与云服务中的 mock/rule 实现不代表工业诊断准确性。

详见 [FILE_FUNCTIONS.md](FILE_FUNCTIONS.md)。