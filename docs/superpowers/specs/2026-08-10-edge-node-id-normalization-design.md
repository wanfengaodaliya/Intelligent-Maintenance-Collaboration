# Edge Node ID Normalization Design

## Goal

统一项目中的边缘节点编号和 MQTT 主题格式，正式使用 `edge_01`、`edge_02`，并修正发送器 README 中已经失效的联调命令。

## Scope

- 边缘服务默认节点 ID 改为 `edge_01`。
- 调度器边缘路由的默认节点改为 `edge_01`。
- 发送器 Mock Scheduler 按两位数字生成 `edge/edge_01/input`、`edge/edge_02/input`。
- 发送器、调度器和项目接口文档中的示例同步使用两位编号。
- 删除 README 对已移除的 `tools/test_subscriber.py` 和已移除测试目录的依赖。
- 使用 `mosquitto_sub.exe` 作为当前联调阶段的临时消息观察方式，并明确 PUBACK 或观察到消息都不等于边缘推理完成。

## Boundaries

- 不增加 `edge_1` 与 `edge_01` 的双格式兼容逻辑，避免错误配置被静默接受。
- 不为边缘模块虚构当前仓库中不存在的 MQTT 消费入口。
- 不修改发送、调度或推理业务流程。

## Success Criteria

- 相关代码生成或回退到 `edge_01`、`edge_02`。
- 代码和当前文档不再出现旧节点 ID `edge_1`、`edge_2`。
- README 中的命令均指向当前存在的程序或明确标注为外部联调工具。
