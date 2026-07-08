# Scheduler 调度器说明

## 1. 模块作用

`scheduler` 是云边协同项目中的调度器模块。

当前版本是一个最小可运行的规则调度器，用于根据任务信息、边缘模型结果、网络状态和节点状态，判断任务应该：

- 留在边缘节点执行：`edge`
- 上传云端执行：`cloud`
- 云不可用或网络较差时边缘降级执行：`fallback_edge`

当前版本不包含雾节点调度，也不是完整训练版 PER-DDPG 模型，而是保留 PER-DDPG 的状态输入和动作选择思想，用规则先跑通项目主流程。

## 2. 文件结构

```text
scheduler/
  api.py              HTTP 接口入口
  rule_scheduler.py   核心规则调度逻辑
  __init__.py         Python 包导出入口
  __pycache__/        Python 自动生成的缓存目录
```

## 3. 主要文件说明

### `api.py`

负责对外提供 HTTP 接口。

主要接口：

```text
GET  /health
POST /scheduler/decide
```

如果安装了 FastAPI，可以作为 FastAPI 应用使用；如果没有安装 FastAPI，也可以直接运行：

```powershell
python api.py
```

默认服务地址：

```text
http://127.0.0.1:8003
```

### `rule_scheduler.py`

负责核心调度决策。

主要类：

```python
PreDDPGScheduler
```

主要方法：

```python
decide(request)
```

该方法接收接口文档规定的输入字段，然后返回调度结果。

### `__init__.py`

用于把 `scheduler` 目录声明为 Python 包，并导出常用对象：

```python
PreDDPGScheduler
ScheduleDecision
decide_schedule
```

其他模块可以这样导入：

```python
from scheduler import decide_schedule
```

## 4. 调度接口

### 请求地址

```text
POST http://127.0.0.1:8003/scheduler/decide
```

### 请求体示例

```json
{
  "task": {
    "task_id": "task_0001",
    "source_node": "edge_1",
    "deadline_ms": 200,
    "priority": 0.8,
    "data_size_kb": 128
  },
  "edge_result": {
    "label": "abnormal",
    "confidence": 0.72,
    "edge_latency_ms": 38,
    "need_cloud": true
  },
  "network_state": {
    "latency_ms": 30,
    "bandwidth_mbps": 20,
    "packet_loss": 0.01,
    "cloud_available": true
  },
  "node_state": {
    "edge_cpu_usage": 0.55,
    "edge_memory_usage": 0.62,
    "cloud_queue_length": 3,
    "fog_available": false
  }
}
```

### 响应体示例

```json
{
  "task_id": "task_0001",
  "route": "cloud",
  "target_node": "cloud_1",
  "reason": "edge confidence 0.72 is below 0.80",
  "estimated_total_latency_ms": 187.8,
  "upload_required": true,
  "scheduler": "PER-DDPG-rule-minimal",
  "policy_score": {
    "edge": 0.4861,
    "cloud": 0.5139
  }
}
```

## 5. 当前调度规则

当前规则调度器的主要判断逻辑如下：

```text
cloud_available = false → fallback_edge
packet_loss 太高 → fallback_edge
bandwidth 太低 → fallback_edge
confidence < 0.8 → cloud
need_cloud = true → cloud
规则分数支持 cloud 且 cloud 预计更快 → cloud
边缘预计时延超过 deadline 且 cloud 更快 → cloud
其他情况 → edge
```

## 6. 关键字段含义

| 字段 | 含义 |
|---|---|
| `deadline_ms` | 任务最大允许完成时间，单位毫秒 |
| `priority` | 任务优先级，范围通常为 0 到 1 |
| `data_size_kb` | 任务数据大小，单位 KB |
| `confidence` | 边缘模型置信度 |
| `edge_latency_ms` | 边缘模型推理耗时，来自边缘模型服务 |
| `need_cloud` | 边缘模型是否建议上云 |
| `latency_ms` | 当前网络延迟 |
| `bandwidth_mbps` | 当前网络带宽 |
| `packet_loss` | 当前网络丢包率 |
| `cloud_available` | 云端是否可达 |
| `edge_cpu_usage` | 边缘节点 CPU 占用率 |
| `edge_memory_usage` | 边缘节点内存占用率 |
| `cloud_queue_length` | 云端当前排队任务数量 |

## 7. 运行方式

启动服务：

```powershell
python api.py
```

健康检查：

```text
GET http://127.0.0.1:8003/health
```

调度决策：

```text
POST http://127.0.0.1:8003/scheduler/decide
```

## 8. 后续扩展方向

当前版本用于先跑通接口和主流程。后续可以逐步扩展为：

1. 增加更精细的时延估算模型。
2. 接入真实云端队列和边缘资源监控。
3. 加入完整 PER-DDPG 训练环境。
4. 用训练好的 Actor 网络替换当前规则分数。
5. 根据项目需要重新加入雾节点调度。
