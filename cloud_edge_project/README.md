# 云边协同项目主流程目录说明

本文档说明第一阶段的主流程最小目录结构。第一阶段目标不是一次性完成完整系统，而是先跑通最小主链路：

```text
TaskRequest
-> /edge/infer
-> /scheduler/decide
-> /cloud/infer
-> TaskLog
```

## 第一阶段目录结构

```text
cloud_edge_project/
├── common/
│   ├── __init__.py
│   ├── schemas.py
│   ├── config.py
│   └── logger.py
├── simulator/
│   ├── __init__.py
│   └── task_generator.py
├── edge_service/
│   ├── __init__.py
│   ├── app.py
│   └── model.py
├── scheduler/
│   ├── __init__.py
│   ├── api.py
│   └── rule_scheduler.py
├── cloud_service/
│   ├── __init__.py
│   ├── app.py
│   └── model.py
├── configs/
│   └── local.yaml
├── examples/
│   ├── task_industrial.json
│   ├── edge_result.json
│   └── schedule_decision.json
├── logs/
│   └── .gitkeep
├── docs/
│   └── api.md
├── quick_demo.py
├── start_all.py
├── requirements.txt
└── README.md
```

## 目录说明

- `common/`：公共数据格式、配置读取、日志工具。
- `simulator/`：任务生成器。
- `edge_service/`：边缘推理服务。
- `scheduler/`：调度服务。
- `cloud_service/`：云端推理服务。
- `configs/`：本地配置文件。
- `examples/`：示例 JSON 数据。
- `logs/`：日志目录。
- `docs/`：接口文档。
- `quick_demo.py`：最小流程演示脚本。
- `start_all.py`：一键启动脚本。
- `requirements.txt`：项目依赖。

## 暂不创建的后期目录

第一阶段暂不创建以下后期模块目录：

```text
dashboard/
consistency/
experiments/
models/
docker/
```
