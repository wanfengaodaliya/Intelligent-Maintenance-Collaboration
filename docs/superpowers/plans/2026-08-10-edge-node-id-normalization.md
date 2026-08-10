# Edge Node ID Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将边缘节点编号统一为两位数字格式，并让发送器联调说明与当前仓库内容一致。

**Architecture:** 只修改节点 ID 的默认值、Mock Scheduler 的主题格式和相关文档示例。保留现有模块边界，不新增兼容层或边缘 MQTT 消费实现。

**Tech Stack:** Python 3、MQTT/Mosquitto、Markdown

## Global Constraints

- 正式节点 ID 只使用 `edge_01`、`edge_02`。
- MQTT 主题使用 `edge/edge_01/input`、`edge/edge_02/input`。
- 不重新引入已删除的测试订阅器和测试目录。
- PUBACK 只代表 Broker 确认，不代表边缘推理完成。

---

### Task 1: Normalize Runtime Defaults

**Files:**
- Modify: `cloud_edge_project/edge_service/model.py`
- Modify: `cloud_edge_project/scheduler/rule_scheduler.py`
- Modify: `cloud_edge_project/sender_module/tools/mock_scheduler.py`

**Interfaces:**
- Consumes: `bearing_id` 尾部的轴承编号和调度任务的 `source_node`。
- Produces: 两位格式的边缘节点 ID 和 MQTT `target_topic`。

- [x] **Step 1: Run a failing HTTP behavior check**

启动 `SchedulerHandler` 的临时 HTTP 服务，分别提交 `bearing_01`、`bearing_02`，断言返回 `edge/edge_01/input`、`edge/edge_02/input`。当前代码应因返回 `edge_1`、`edge_2` 而失败。

- [x] **Step 2: Implement the minimal formatting change**

将主题格式改为：

```python
f"edge/edge_{(bearing_number - 1) % 2 + 1:02d}/input"
```

并将两个运行时默认值从 `edge_1` 改为 `edge_01`。

- [x] **Step 3: Re-run the HTTP behavior check**

预期两个请求均通过，并返回两位编号主题。

### Task 2: Synchronize Integration Documentation

**Files:**
- Modify: `cloud_edge_project/sender_module/README.md`
- Modify: `cloud_edge_project/scheduler/README.md`
- Modify: `云边协同项目接口说明文档.md`

**Interfaces:**
- Consumes: Task 1 确定的正式节点 ID 和主题格式。
- Produces: 与当前代码及联调阶段一致的命令和 JSON 示例。

- [x] **Step 1: Replace legacy node examples**

将 `edge_1`、`edge_2` 示例分别替换为 `edge_01`、`edge_02`，并将发送器响应示例改为 `edge/edge_02/input`。

- [x] **Step 2: Replace deleted subscriber instructions**

删除 `python tools/test_subscriber.py`，改为：

```powershell
& "C:\Program Files\mosquitto\mosquitto_sub.exe" -h 127.0.0.1 -p 1883 -t "edge/+/input" -q 1 -v
```

注明这只是观察 Broker 上的消息；正式联调应启动边缘模块实际提供的 MQTT 消费入口。

- [x] **Step 3: Replace obsolete automated-test section**

删除对不存在的 `tests` 目录的命令和覆盖范围描述，改为当前联调检查边界。

### Task 3: Verify the Repository

**Files:**
- Verify all tracked Python and Markdown files.

**Interfaces:**
- Consumes: Tasks 1-2 的全部修改。
- Produces: 无旧命名、无失效路径且 Python 文件语法有效的工作树。

- [x] **Step 1: Search for legacy IDs and deleted paths**

使用 `rg` 检查 `edge_1`、`edge_2`、`tools/test_subscriber.py` 和不存在的 `tests` 命令，预期无匹配。

- [x] **Step 2: Validate Python syntax**

使用 Python AST 解析三个修改过的 Python 文件，预期全部解析成功且不生成缓存文件。

- [x] **Step 3: Review the diff**

使用 `git diff --check` 和 `git diff` 确认没有空白错误或范围外修改。
