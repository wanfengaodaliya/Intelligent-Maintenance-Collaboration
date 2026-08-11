# Edge MQTT 接入实施计划

> **执行要求：** 按任务逐项实施并使用测试先行；每个行为先补失败测试，再写最小实现，最后运行相关回归测试。

**目标：** 让 `sender` 发布到 `edge/edge_01/input` 的正式数据包进入 `edge_service`，与 HTTP `/edge/packets` 共用同一条处理链，并保持任务身份、重复投递和冲突检测语义。

**架构：** 新增共享的 `EdgePacketProcessor` 作为唯一处理入口。HTTP 路由和 MQTT 消费者只负责协议适配，正式数据包依次经过任务接入、校验缓存、感知、模型流水线和现有 `PacketRoutingBridge`。幂等状态由任务接入和校验缓存共同维护；模型已完成但调度路由失败时，只重试路由，不重复推理。

**技术栈：** Python、FastAPI、Paho MQTT、pytest、现有 `edge_runtime` 组件。

## 全局约束

- 正式任务身份保持为 `task_id`，包身份保持为 `(task_id, device_id, bearing_id, sender_id, packet_id, sequence_number)`。
- MQTT 仅订阅精确主题 `edge/edge_01/input`，使用 QoS 1、持久客户端标识和手动确认。
- 完全重复的数据包返回幂等成功，不再次校验、感知、推理或路由。
- 相同身份但内容不同的数据包返回冲突，不覆盖已有记录。
- `TASK_NOT_FOUND` 视为任务注册与数据到达竞态：属于可重试结果，MQTT 不确认消息。
- JSON/契约错误等永久失败允许确认，防止坏消息无限重投。
- 路由瞬时失败不确认消息；再次投递时复用已缓存推理结果，只重试 `/scheduler/packet-route`。
- 不修改 sender 的正式数据包契约，不新增身份字段，不发明新的调度状态映射。
- 当前目录不是 Git 仓库，实施前后以快照、文件哈希和测试结果留证，不伪造提交记录。

## 任务一：扩展正式结果路由适配

**文件：**
- 修改：`cloud_edge_project/edge_service/src/packet_routing_bridge.py`
- 修改：`cloud_edge_project/edge_service/tests/test_packet_routing_bridge.py`

- [ ] 先写测试：正式 sender 数据包与推理结果能生成现有 `/scheduler/packet-route` 请求。
- [ ] 断言顶层 `task_id` 不变，`input_ref` 原样保留 `device_id`、`bearing_id`、`sender_id`、`packet_id`、`sequence_number`。
- [ ] 断言旧 `route(payload, edge_result)` 行为保持不变。
- [ ] 实现 `route_formal(raw_packet, edge_result, *, started_at_ns, finished_at_ns)`，复用现有发送、异常与响应检查逻辑。
- [ ] 运行：`python -m pytest edge_service/tests/test_packet_routing_bridge.py -q`。

## 任务二：建立 HTTP 与 MQTT 共用处理器

**文件：**
- 新增：`cloud_edge_project/edge_service/src/edge_packet_processor.py`
- 新增：`cloud_edge_project/edge_service/tests/test_edge_packet_processor.py`

- [ ] 先写测试：首次处理只触发一次校验、感知、模型提交和路由。
- [ ] 先写测试：完全重复返回幂等成功，副作用计数保持一次。
- [ ] 先写测试：同一身份不同内容返回 `409`，不产生第二次推理或路由。
- [ ] 先写测试：首次路由超时返回可重试；重复投递只重试路由，不重新推理。
- [ ] 定义统一结果 `PacketProcessOutcome(status_code, body, acknowledge, retryable)`。
- [ ] 实现 `EdgePacketProcessor.process(payload)`，串联 `EdgeTaskIngress`、`EdgeValidationCache`、`EdgePerception`、`EdgeModelPipeline` 和 `PacketRoutingBridge`。
- [ ] 实现模型完成回调，把结果与原始包身份关联；路由成功后才把端到端状态标记为完成。
- [ ] 为处理器补齐 `start()`、`stop()`，保证模型线程生命周期可控。
- [ ] 运行：`python -m pytest edge_service/tests/test_edge_packet_processor.py -q`。

## 任务三：实现 MQTT 协议适配层

**文件：**
- 新增：`cloud_edge_project/edge_service/src/edge_mqtt_ingress.py`
- 新增：`cloud_edge_project/edge_service/tests/test_edge_mqtt_ingress.py`
- 修改：`cloud_edge_project/edge_service/requirements.txt`
- 修改：`cloud_edge_project/requirements.txt`

- [ ] 先写可注入假客户端的测试，验证连接后精确订阅 `edge/edge_01/input`、QoS 1。
- [ ] 先写测试：消息回调只做解码和入有界队列，工作线程调用共享处理器。
- [ ] 先写测试：成功、幂等重复、永久坏消息会确认；可重试失败和 `TASK_NOT_FOUND` 不确认。
- [ ] 先写测试：队列满时不确认并记录状态，避免无界内存增长。
- [ ] 定义 MQTT 配置数据类，包含启用开关、主机、端口、主题、客户端标识、QoS、队列容量和重连参数。
- [ ] 实现 Paho MQTT 2.x 回调，启用手动确认、自动重连和可观测连接状态。
- [ ] 在两份 requirements 中固定与 sender 一致的 `paho-mqtt==2.1.0`。
- [ ] 运行：`python -m pytest edge_service/tests/test_edge_mqtt_ingress.py -q`。

## 任务四：接入 FastAPI 生命周期与配置

**文件：**
- 修改：`cloud_edge_project/edge_service/app.py`
- 修改：`cloud_edge_project/configs/local.yaml`
- 修改：`cloud_edge_project/scheduler/node_registry.py`
- 修改或新增：`cloud_edge_project/edge_service/tests/test_app_packet_ingress.py`

- [ ] 先写测试：`POST /edge/packets` 委托共享处理器，并透传其状态码和响应体。
- [ ] 先写测试：应用启动和关闭时分别启动、停止模型流水线与 MQTT 接入。
- [ ] 先写测试：健康检查暴露 MQTT 是否启用、连接状态、队列深度和最后错误。
- [ ] 用 FastAPI lifespan 统一管理资源，避免导入模块时建立网络连接。
- [ ] 在 `local.yaml` 增加 MQTT 配置，默认主题为 `edge/edge_01/input`。
- [ ] 将 scheduler 默认节点身份和主题统一为 `edge_01`、`edge/edge_01/input`，保持已有外部覆盖机制。
- [ ] 运行：`python -m pytest edge_service/tests/test_app_packet_ingress.py scheduler/tests -q`。

## 任务五：补可验证的端到端测试

**文件：**
- 新增：`cloud_edge_project/tests/e2e/test_sender_mqtt_edge_service.py`
- 可选新增：`cloud_edge_project/tests/e2e/test_sender_mqtt_edge_service_live.py`
- 修改：`cloud_edge_project/pytest.ini`（仅在增加真实代理测试标记时）

- [ ] 使用 `sender.packet.build_sensor_packet` 和 `serialize_packet` 构造正式数据，禁止手写简化替身契约。
- [ ] 使用可注入的 Paho 兼容客户端穿过真实 MQTT 回调、队列、共享处理器、正式校验与感知。
- [ ] 使用可控的 `TestRuleRunner` 完成回调，并用记录型调度端验证 `/scheduler/packet-route` 请求。
- [ ] 断言首次消息保留完整身份且只产生一次推理和一次路由。
- [ ] 以相同 MQTT 消息再次投递，断言获得确认且副作用计数不增加。
- [ ] 以相同身份和不同内容投递，断言冲突且不产生第二个结果。
- [ ] 可选真实代理测试通过环境变量显式启用，默认测试套件不依赖本机 Mosquitto。
- [ ] 运行：`python -m pytest tests/e2e/test_sender_mqtt_edge_service.py -q`。

## 任务六：完整验证与留证

- [ ] 运行相关测试：`python -m pytest edge_service/tests scheduler/tests tests/e2e/test_sender_mqtt_edge_service.py -q`。
- [ ] 运行项目全量测试；若受既有临时目录权限影响，记录具体命令和错误，不把环境失败写成代码失败。
- [ ] 运行语法检查：`python -m compileall edge_service sender_module scheduler tests`。
- [ ] 比较实施前快照与当前文件，确认没有覆盖用户的无关修改。
- [ ] 汇总正式 sender 包的实际通过证据、重复投递计数、冲突结果和调度请求身份字段。
