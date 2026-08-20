# 云边协同智能运维作战台

面向云边协同轴承智能运维场景的多页面前端，基于 React + TypeScript + Vite + Ant Design + ECharts。

## 技术栈

- **框架**: React 19 + TypeScript 6 (strict mode)
- **构建**: Vite 8
- **UI 组件**: Ant Design 6 + @ant-design/icons
- **图表**: ECharts + echarts-for-react
- **路由**: React Router 7
- **数据请求**: TanStack Query 5
- **时间处理**: dayjs
- **测试**: Vitest + React Testing Library

## 目录结构

```
src/
  api/          # 统一 API 客户端和各服务接口
  adapters/     # 数据适配器（任务归一化、指标、时间转换）
  components/   # 公共组件
    layout/       # 布局（侧边栏、状态栏、链路带）
    status/       # 状态展示组件
    charts/       # 图表组件
    feedback/     # 加载、错误、空状态组件
  pages/        # 五个一级页面
    Overview/     # 系统指标
    Tasks/        # 任务结果
    Network/      # 网络链路
    EdgeNodes/    # 边缘节点
    Cloud/        # 云端中心
  hooks/        # 自定义 Hook
  mock/         # Mock 数据
  router/       # 路由配置
  styles/       # 设计令牌和全局样式
  types/        # TypeScript 类型定义
  utils/        # 工具函数
  tests/        # 测试文件
```

## 安装

```bash
cd cloud_edge_project/frontend
npm install
```

## 环境变量

复制 `.env.example` 为 `.env` 并根据实际情况修改：

```env
VITE_LOG_API_BASE_URL=http://127.0.0.1:8006
VITE_CLOUD_API_BASE_URL=http://127.0.0.1:8004
VITE_EDGE_API_BASE_URL=http://127.0.0.1:8001
VITE_SCHEDULER_API_BASE_URL=http://127.0.0.1:8003
VITE_NETWORK_API_BASE_URL=http://127.0.0.1:8090

VITE_EDGE_NODE_IDS=edge_1
VITE_POLL_INTERVAL_MS=5000
VITE_STALE_THRESHOLD_MS=15000
VITE_OFFLINE_THRESHOLD_MS=30000

VITE_ENABLE_MOCK=false
VITE_ENABLE_CLOUD_ACTIONS=false
```

## 启动开发服务器

```bash
npm run dev
```

默认地址：`http://localhost:5173`

### 开发环境代理

Vite 已配置代理，将所有以 `/api/log`、`/api/cloud`、`/api/edge`、`/api/scheduler`、`/api/network` 开头的请求转发到对应的后端服务。实际开发时可直接使用各服务的真实地址（通过环境变量配置）。

## 联调方式

1. 按后端项目 README 启动所有后端服务（start_all.py）
2. 确认各服务健康检查可通过：
   ```powershell
   Invoke-RestMethod http://127.0.0.1:8001/health
   Invoke-RestMethod http://127.0.0.1:8003/health
   Invoke-RestMethod http://127.0.0.1:8004/health
   Invoke-RestMethod http://127.0.0.1:8006/health
   ```
3. 网络模拟器单独启动（参考 internet_service/network_simulator/README.md）
4. 启动前端：`npm run dev`
5. 浏览器打开 `http://localhost:5173`

## Mock 模式

设置 `VITE_ENABLE_MOCK=true` 启用 Mock 模式，在前端独立演示时使用。

- Mock 数据与真实接口使用相同的 TypeScript 类型
- 页面顶部始终显示"演示数据"标识
- 默认配置必须使用真实接口
- 真实接口失败时不会自动切换到 Mock

## 云侧写操作

模型更新操作（批准、拒绝、分发、回滚）默认关闭。设置 `VITE_ENABLE_CLOUD_ACTIONS=true` 以启用。

所有写操作具备：
- 二次确认对话框
- 提交期间禁用重复点击
- 正确显示成功结果和错误码
- 完成后刷新真实状态

## 构建

```bash
npm run build  # TypeScript 类型检查 + Vite 生产构建
```

构建产物位于 `dist/` 目录。

## 测试

```bash
npm test           # 运行所有测试
npm run test:watch # 监听模式
```

## 页面说明

### 系统指标 (`/overview`)
- 关键指标卡片（数据包总数、成功率、延迟等）
- 本次会话趋势折线图（基于轮询快照，标注"本次会话趋势"）
- 赛题指标达标矩阵
- 任务路由分布饼图
- 冲突与异常分析

### 任务结果 (`/tasks`)
- 任务统计概览
- 可筛选、排序的任务表格
- 任务详情抽屉（基本信息、运行路径时间线、设备运行建议）
- 设备建议规则：优先显示后端返回的建议，无后端建议时根据状态和风险等级确定性映射，标注"前端规则映射"

### 网络链路 (`/network`)
- 链路统计卡片
- 表格视图和拓扑视图切换
- 链路详情抽屉
- 期望参数与实际施加参数区分显示
- 丢包率模型参数提示

### 边缘节点 (`/edge-nodes`)
- 节点卡片总览（CPU、内存、队列、模型版本）
- 多节点对比表格
- 节点详情抽屉
- 在线状态基于上报时间判断（在线/陈旧/离线/未知）

### 云端中心 (`/cloud`)
- 云端服务状态
- 复核结果查询（按 review_id）
- 模型更新生命周期时间线
- 受控的模型更新操作（受 VITE_ENABLE_CLOUD_ACTIONS 控制）
- 最近访问记录（本地存储，不代表服务器完整列表）

## 后端接口

已接入的真实接口：

| 服务 | 接口 | 用途 |
|------|------|------|
| Log (8006) | GET /health | 健康检查 |
| Log (8006) | GET /dashboard/metrics | 系统指标 |
| Log (8006) | GET /dashboard/tasks?limit=N | 任务列表 |
| Cloud (8004) | GET /health | 健康检查 |
| Cloud (8004) | GET /cloud/edge-status/{node_id} | 边缘节点状态 |
| Cloud (8004) | GET /cloud/packet-reviews/{review_id} | 数据包复核 |
| Cloud (8004) | GET /cloud/bearing-window-reviews/{review_id} | 轴承窗口复核 |
| Cloud (8004) | GET /cloud/device-reviews/{review_id} | 设备复核 |
| Cloud (8004) | GET /cloud/reviews/{review_id}/summary | 复核摘要 |
| Cloud (8004) | GET /cloud/model-update/{update_id} | 模型更新 |
| Cloud (8004) | POST /cloud/model-update/{update_id}/approve | 批准更新 |
| Cloud (8004) | POST /cloud/model-update/{update_id}/reject | 拒绝更新 |
| Cloud (8004) | POST /cloud/model-update/{update_id}/handoff-distribution | 分发 |
| Cloud (8004) | POST /cloud/model-update/{update_id}/request-rollback | 回滚 |
| Edge (8001) | GET /health | 边缘节点健康检查 |
| Scheduler (8003) | GET /health | 调度器健康检查 |
| Network (8090) | GET /health | 网络模拟器健康检查 |
| Network (8090) | GET /api/v1/network/links | 网络链路列表 |
| Network (8090) | GET /api/v1/network/links/{link_id} | 网络链路详情 |
| Network (8090) | GET /api/v1/network/runtime | 网络运行时 |

## 已知限制

- 后端没有复核任务列表和模型更新列表接口，不支持按列表查询，仅支持按 ID 查询
- 网络模拟器需单独启动（基于 Toxiproxy）
- 边缘节点状态通过 Cloud 服务间接获取，不直接查询 Edge 服务