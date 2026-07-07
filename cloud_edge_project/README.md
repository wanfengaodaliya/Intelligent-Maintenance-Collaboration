# 云边协同感知与决策原型系统

本项目从 `docs/api.md` 的 V0.3 接口开始实现，第一阶段只跑通一个工业设备轴承检测场景，不修改 API 字段和路径。

第一阶段主链路：

```text
SensorPacket
-> POST /edge/infer
-> POST /scheduler/decide
-> POST /cloud/infer 仅 route = cloud 时调用
-> POST /logs/task_trace
-> GET /dashboard/metrics, GET /dashboard/tasks
```

赛题关注云边协同、弱网可用性、端到端时延、资源与通信效率、稳定性和可量化结果。因此当前实现先保留这些可统计字段，再逐步替换真实模型和复杂调度算法。

## 目录结构

```text
common/          公共配置、接口校验、JSONL 日志与指标
simulator/       轴承时序 SensorPacket 生成器
edge_service/    边缘推理服务，端口 8001
scheduler/       规则调度服务，端口 8003
cloud_service/   云端推理服务，端口 8004
log_service/     日志与指标服务，端口 8006
configs/         本地配置
examples/        示例数据生成说明
logs/            运行日志
docs/api.md      冻结接口文档
quick_demo.py    单条链路演示脚本
start_all.py     一键启动服务脚本
```

`log_service/` 是基于 API 增加的目录，因为 V0.3 已明确日志与指标服务运行在 8006。

## 当前能力

- 生成符合 API 的 800 点 `bearing_timeseries` 传感器包。
- 边缘 mock 模型输出 `EdgeResult`。
- 调度器严格按 API 规则返回 `edge`、`cloud`、`fallback_edge`。
- 云端 mock 模型在上云路径返回更高置信度和处理建议。
- 日志服务写入 `logs/task_trace.jsonl`。
- 指标服务统计成功率、平均端到端时延、上云比例、边缘完成比例、回退比例和异常比例。

## 快速运行

不安装 HTTP 依赖也可以先验证主流程：

```bash
python quick_demo.py
```

生成严格 800 点示例数据：

```bash
python -m simulator.task_generator --out examples
```

安装服务依赖：

```bash
pip install -r requirements.txt
```

启动全部服务：

```bash
python start_all.py
```

服务启动后，用真实 HTTP 接口跑一条链路：

```bash
python quick_demo.py --http
```

健康检查地址：

```text
http://127.0.0.1:8001/health
http://127.0.0.1:8003/health
http://127.0.0.1:8004/health
http://127.0.0.1:8006/health
```

## 第一阶段验收标准

1. 一条 `SensorPacket` 可以成功进入 `/edge/infer`。
2. `/edge/infer` 返回相同 `packet_id` 的 `EdgeResult`。
3. `/scheduler/decide` 能根据 `confidence` 和 `cloud_available` 返回三种合法路径。
4. `route = cloud` 时调用 `/cloud/infer`。
5. `route = edge` 或 `fallback_edge` 时直接使用边缘结果生成最终日志。
6. `/logs/task_trace` 能保存完整 `TaskTrace`。
7. `/dashboard/metrics` 和 `/dashboard/tasks` 能从日志中返回统计结果。
8. 所有服务 `/health` 返回 `status = ok`。
9. 错误输入返回统一错误响应。

## 后续扩展

论文中的 DAG、优先级/紧迫性排序和 PER-DDPG 调度应放在 `scheduler/` 下作为规则调度器之后的增强实现。扩展时仍然保持 `docs/api.md` 的外部接口不变，只替换 `/scheduler/decide` 内部的决策核心。

