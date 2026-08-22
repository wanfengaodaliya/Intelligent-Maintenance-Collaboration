# 云边协同智能运维系统部署图设计说明

## 用途与输出

- 用途：项目申报书中的系统部署总图。
- 画布：16:9 横向构图，适合插入 Word 或导出 PDF。
- 风格：白底、低饱和蓝灰配色、矢量感技术架构图；字体清晰，避免装饰性背景和写实元素。
- 图题：`云边协同智能运维系统部署架构`。

## 部署边界

图中明确区分两个运行边界：

1. 宿主机：`start_project.ps1`、3 个 Sender、Scheduler、Cloud，以及两个可选 LLM 服务。
2. Docker：Toxiproxy、MQTT Broker、Network Controller、`edge_01`、`edge_02`。

`start_project.ps1` 位于画面顶部，使用虚线控制关系连接两个部署边界，并标注“统一启动与健康门控”。

## 组件与端口

宿主机组件：

- Sender：`sender_01`、`sender_02`、`sender_03`。
- Scheduler：`8003`。
- Cloud：`8004`。
- 可选边缘建议 LLM：`8005`。
- 可选云端模型更新 LLM：`6006`。

Docker 组件：

- Toxiproxy：管理 API `8474`。
- MQTT Broker：`1883`。
- Network Controller：`8090`。
- `edge_01`：宿主机映射端口 `8001`。
- `edge_02`：宿主机映射端口 `8002`。

## MQTT 代理拓扑

必须完整呈现 3×2 共 6 条独立 Sender→Edge MQTT 代理链路。链路先进入 Toxiproxy，再经 MQTT Broker 投递至对应 Edge 的主题；在连线上醒目标注代理端口：

- `sender_01 → edge_01`：`18831`
- `sender_01 → edge_02`：`18832`
- `sender_02 → edge_01`：`18931`
- `sender_02 → edge_02`：`18932`
- `sender_03 → edge_01`：`19031`
- `sender_03 → edge_02`：`19032`

为避免六条连线交叉造成阅读困难，采用端口矩阵或六条平行通道表达；同时保留明确的 3×2 映射关系。

## 业务与控制关系

- 实线箭头：Sender→Toxiproxy→MQTT Broker→Edge 的业务数据流。
- 细实线箭头：Edge 与 Scheduler/Cloud 的主要业务通信，标注“经 Toxiproxy HTTP 代理”，但不展开所有 HTTP 端口，避免申报书总图过密。
- 虚线箭头：`start_project.ps1` 的编排与健康门控关系。
- 点划线箭头：可选 LLM 调用；`edge_01`、`edge_02` 调用边缘建议 LLM `8005`，Cloud 调用云端模型更新 LLM `6006`，并明确标注“不经过网络模拟”。
- Network Controller 与 Toxiproxy 之间绘制控制关系，表示网络状态和故障注入管理。

## 视觉层级

- 宿主机边界使用浅灰蓝底色；Docker 边界使用浅蓝底色并带 Docker 标识文字。
- Sender 使用中性灰蓝，网络模拟组件使用青蓝，Edge 使用绿色，Scheduler/Cloud 使用深蓝，可选 LLM 使用浅紫色。
- 端口号使用等宽字体胶囊标签，六个 MQTT 代理端口优先级最高。
- 右下角提供小型图例：业务数据流、控制/编排、可选直连。

## 验收标准

1. 图中准确区分宿主机与 Docker 部署位置。
2. 图中包含 3 个 Sender、2 个 Edge、3 个网络模拟组件、Scheduler、Cloud 和 2 个可选 LLM。
3. 六条 Sender→Edge MQTT 代理链路及端口 `18831`、`18832`、`18931`、`18932`、`19031`、`19032` 均无遗漏或错配。
4. 清晰标注 `1883`、`8474`、`8090`、`8001`、`8002`、`8003`、`8004`、`8005`、`6006`。
5. 可选 LLM 明确位于宿主机并标注不经过网络模拟。
6. 图中文字可在项目申报书常规页面宽度下阅读，无水印、无多余品牌标识。
