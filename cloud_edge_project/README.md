# 云边协同项目主流程目录说明

本文档用于说明第一阶段需要创建的**主流程最小目录结构**。  
第一阶段目标不是把完整系统一次性做完，而是先跑通一条最小主链路：

```text
TaskRequest
→ /edge/infer
→ /scheduler/decide
→ /cloud/infer
→ TaskLog
```

也就是：

```text
周：任务生成器
    ↓
贾：边缘推理服务
    ↓
彭：调度决策服务
    ↓
贾：云端推理服务
    ↓
刘：日志记录与后续可视化
```

---

## 一、第一阶段推荐目录结构

```text
cloud_edge_project/
├── common/
│   ├── __init__.py
│   ├── schemas.py
│   ├── config.py
│   └── logger.py
│
├── simulator/
│   ├── __init__.py
│   └── task_generator.py
│
├── edge_service/
│   ├── __init__.py
│   ├── app.py
│   └── model.py
│
├── scheduler/
│   ├── __init__.py
│   ├── api.py
│   └── rule_scheduler.py
│
├── cloud_service/
│   ├── __init__.py
│   ├── app.py
│   └── model.py
│
├── configs/
│   └── local.yaml
│
├── examples/
│   ├── task_industrial.json
│   ├── edge_result.json
│   └── schedule_decision.json
│
├── logs/
│   └── .gitkeep
│
├── docs/
│   └── api.md
│
├── quick_demo.py
├── start_all.py
├── requirements.txt
└── README.md
```

---

## 二、目录总体说明

这个目录是第一阶段的**主流程最小骨架**，只保留当前最需要的部分。

暂时不创建：

```text
dashboard/
consistency/
experiments/
models/
docker/
```

这些属于后期模块。  
第一阶段先把主流程跑通，后面再逐步扩展。

---

## 三、各目录和文件说明

---

## 1. `common/`：公共基础模块

```text
common/
├── __init__.py
├── schemas.py
├── config.py
└── logger.py
```

### 作用

`common/` 放所有模块都会用到的公共代码。

它相当于整个项目的“公共约定区”。

主要包括：

- 统一数据格式；
- 配置文件读取；
- 日志工具；
- 公共工具函数。

### `common/__init__.py`

Python 包标识文件。

有了这个文件后，其他代码可以这样导入：

```python
from common.schemas import TaskRequest
```

### `common/schemas.py`

定义统一通信数据格式。

第一阶段至少需要定义：

```text
TaskRequest       任务输入格式
EdgeResult        边缘模型输出格式
ScheduleDecision  调度器输出格式
CloudResult       云端模型输出格式
TaskLog           任务日志格式
```

作用是防止四个人各写各的字段。

例如：

- 周生成任务时，必须符合 `TaskRequest`；
- 贾的边缘模型必须输出 `EdgeResult`；
- 彭的调度器必须输出 `ScheduleDecision`；
- 刘记录日志时必须使用 `TaskLog`。

### `common/config.py`

用于读取配置文件。

例如读取：

```text
configs/local.yaml
```

后续端口、模型路径、服务地址都从配置文件读取，不要写死在代码里。

例如不要在代码中写：

```python
CLOUD_URL = "http://192.168.1.10:8004"
```

应该从配置文件读取：

```yaml
cloud:
  host: "127.0.0.1"
  port: 8004
```

### `common/logger.py`

统一日志工具。

第一阶段可以先简单实现：

- 打印日志；
- 保存任务运行记录；
- 写入 `logs/task_trace.jsonl`。

后期可以再扩展为更完整的日志系统。

---

## 2. `simulator/`：任务生成器

```text
simulator/
├── __init__.py
└── task_generator.py
```

### 作用

`simulator/` 用来模拟真实业务环境中的任务输入。

第一阶段主要由周负责。

### `simulator/task_generator.py`

用于生成模拟任务。

例如工业场景任务：

```json
{
  "task_id": "task_0001",
  "scenario": "industrial",
  "source_node": "edge_1",
  "task_type": "fault_detection",
  "deadline_ms": 200,
  "priority": 0.8,
  "data_size_kb": 128,
  "data": {
    "device_id": "machine_01",
    "temperature": 78.5,
    "vibration": 0.63,
    "current": 13.2,
    "load": 0.76
  }
}
```

第一阶段先生成一条工业任务即可。

后续再扩展：

- 批量任务生成；
- 能源场景任务；
- 弱网任务；
- 冲突任务；
- 任务流回放。

---

## 3. `edge_service/`：边缘推理服务

```text
edge_service/
├── __init__.py
├── app.py
└── model.py
```

### 作用

`edge_service/` 是边缘节点服务。

主要由贾负责。

它接收任务，调用边缘轻量模型，输出初步判断。

### `edge_service/app.py`

FastAPI 接口文件。

第一阶段实现两个接口：

```text
POST /edge/infer
GET /health
```

#### `POST /edge/infer`

作用：

```text
接收 TaskRequest
→ 调用边缘模型
→ 返回 EdgeResult
```

#### `GET /health`

作用：

```text
检查边缘服务是否正常启动
```

### `edge_service/model.py`

边缘模型逻辑文件。

第一阶段先写 mock 模型，不需要真实大模型。

例如：

```text
温度高、振动大 → abnormal
否则 → normal
```

后续可以逐步升级：

```text
mock 模式 → small 小模型模式 → real 真实边缘模型模式
```

---

## 4. `scheduler/`：调度服务

```text
scheduler/
├── __init__.py
├── api.py
└── rule_scheduler.py
```

### 作用

`scheduler/` 用来判断任务应该在哪里执行。

主要由彭负责调度逻辑，贾负责接口接入。

### `scheduler/api.py`

FastAPI 接口文件。

第一阶段实现：

```text
POST /scheduler/decide
GET /health
```

#### `POST /scheduler/decide`

作用：

```text
接收任务信息、边缘结果、网络状态、节点状态
→ 判断任务走 edge / cloud / fallback_edge
→ 返回 ScheduleDecision
```

### `scheduler/rule_scheduler.py`

第一版规则调度器。

先不要等 PER-DDPG。

可以先写简单规则：

```text
如果云端不可用 → fallback_edge
如果边缘置信度高 → edge
如果边缘置信度低且网络好 → cloud
否则 → edge
```

后续彭完成 DAG / PER-DDPG 后，再替换或增强这里的逻辑。

---

## 5. `cloud_service/`：云端推理服务

```text
cloud_service/
├── __init__.py
├── app.py
└── model.py
```

### 作用

`cloud_service/` 是云端模型服务。

主要由贾负责。

它处理边缘上传的复杂任务，返回更高置信度结果或全局决策。

### `cloud_service/app.py`

FastAPI 接口文件。

第一阶段实现：

```text
POST /cloud/infer
GET /health
```

#### `POST /cloud/infer`

作用：

```text
接收任务和边缘模型结果
→ 调用云端模型
→ 返回 CloudResult
```

### `cloud_service/model.py`

云端模型逻辑文件。

第一阶段先用 mock 逻辑：

```text
云端返回比边缘更高的置信度
```

后续可以升级为：

```text
mock 模式
small 模式
full 云端模型模式
remote 远程模型服务模式
```

---

## 6. `configs/`：配置文件目录

```text
configs/
└── local.yaml
```

### 作用

配置文件用于管理：

- 服务地址；
- 服务端口；
- 模型模式；
- 模型路径；
- 日志保存路径。

### `configs/local.yaml`

第一阶段本地运行配置。

示例：

```yaml
mode: local

services:
  edge:
    host: "127.0.0.1"
    port: 8001

  scheduler:
    host: "127.0.0.1"
    port: 8003

  cloud:
    host: "127.0.0.1"
    port: 8004

model:
  edge_backend: "mock"
  cloud_backend: "mock"

log:
  path: "logs/task_trace.jsonl"
```

以后切换云服务器、多机部署时，只改配置文件，不改业务代码。

---

## 7. `examples/`：示例 JSON 数据

```text
examples/
├── task_industrial.json
├── edge_result.json
└── schedule_decision.json
```

### 作用

这个目录用于放接口示例数据。

全队成员照着这些示例写代码，避免字段不统一。

### `examples/task_industrial.json`

工业任务示例。

给周参考，用于生成任务。

### `examples/edge_result.json`

边缘模型输出示例。

给贾和刘参考。

### `examples/schedule_decision.json`

调度器输出示例。

给彭和刘参考。

---

## 8. `logs/`：运行日志目录

```text
logs/
└── .gitkeep
```

### 作用

保存系统运行日志和实验结果。

第一阶段先用 `.gitkeep` 保留空目录。

项目运行后会生成：

```text
logs/task_trace.jsonl
logs/results.csv
```

这些日志后续用于：

- 计算平均时延；
- 计算云端调用比例；
- 计算任务成功率；
- 画图；
- 写报告；
- 做可视化。

---

## 9. `docs/`：文档目录

```text
docs/
└── api.md
```

### 作用

放项目文档。

第一阶段先放接口文档。

### `docs/api.md`

接口说明文档。

主要记录：

- 每个接口的作用；
- 请求方式；
- 请求路径；
- 请求字段；
- 响应字段；
- 示例 JSON；
- 字段注释。

这个文档主要由刘维护，贾负责技术确认。

---

## 10. `quick_demo.py`：最小流程演示脚本

### 作用

这是第一阶段最重要的脚本之一。

它用于跑通最小主流程：

```text
生成一条任务
→ 调用 /edge/infer
→ 调用 /scheduler/decide
→ 如果需要上云，调用 /cloud/infer
→ 生成 TaskLog
→ 打印结果
```

运行方式：

```bash
python quick_demo.py
```

期望输出：

```text
任务ID: task_0001
边缘结果: abnormal, confidence=0.72
调度路径: cloud
云端结果: abnormal, confidence=0.93
总时延: 154 ms
日志已保存: logs/task_trace.jsonl
```

---

## 11. `start_all.py`：一键启动脚本

### 作用

用于一键启动本地服务。

第一阶段可以启动：

```text
edge_service    端口 8001
scheduler       端口 8003
cloud_service   端口 8004
```

后续再加入：

```text
dashboard       端口 8501
```

目标是让队友不用手动开多个终端。

---

## 12. `requirements.txt`：依赖文件

### 作用

记录 Python 依赖。

第一阶段建议：

```text
fastapi
uvicorn
pydantic
requests
pyyaml
```

安装方式：

```bash
pip install -r requirements.txt
```

---

## 13. `README.md`：项目说明

### 作用

给团队成员和评委看的项目入口说明。

第一版至少包含：

```text
1. 项目简介
2. 目录结构
3. 环境安装
4. 如何启动服务
5. 如何运行 quick_demo.py
6. 常见问题
```

---

## 四、第一阶段暂不创建的目录

以下目录属于后期模块，第一阶段可以不建：

```text
dashboard/      前端展示界面
consistency/    冲突检测与一致性仲裁
experiments/    批量实验与图表
models/         真实模型权重
docker/         容器化部署
tests/          系统测试代码
```

原因：

第一阶段目标是主链路最小闭环，不要一开始把目录做得太复杂。

等主流程跑通后，再逐步扩展。

---

## 五、第一阶段目录对应的主流程

```text
simulator/task_generator.py
        ↓
edge_service/app.py
        ↓
scheduler/api.py
        ↓
cloud_service/app.py
        ↓
common/logger.py
        ↓
logs/task_trace.jsonl
```

对应角色：

```text
周：simulator/task_generator.py
贾：edge_service/、cloud_service/、common/
彭：scheduler/
刘：docs/、logs/、后续 dashboard/
```

---

## 六、第一阶段完成标准

目录创建完成后，第一阶段的最低验收标准是：

```text
1. 项目目录结构清晰
2. 所有人知道代码应该放在哪里
3. common/schemas.py 能定义统一数据格式
4. edge_service/app.py 能实现 /edge/infer
5. scheduler/api.py 能实现 /scheduler/decide
6. cloud_service/app.py 能实现 /cloud/infer
7. quick_demo.py 能串起主流程
8. logs/ 能保存任务运行记录
9. docs/api.md 能记录接口说明
```

最终目标：

```text
TaskRequest
→ EdgeResult
→ ScheduleDecision
→ CloudResult
→ TaskLog
```

只要这个链路跑通，项目就真正启动了。
