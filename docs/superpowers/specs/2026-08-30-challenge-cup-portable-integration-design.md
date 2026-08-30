# 挑战杯可迁移化集成设计

## 目标

以 `origin/main` 的正式业务流程为唯一功能基线，将
`origin/challenge-cup-application` 中的场景插件、兼容层、路径外置和部署配置能力
集成到 `release/challenge-cup-portable`。集成后的系统必须保留主分支既有接口、数据
契约、调度规则和运行结果，同时能够在新机器通过外部配置和独立模型资产完成部署。

## 输入版本与分支策略

- 功能基线：`origin/main`，集成开始时为
  `c268fc0643ed83c4e1048b466ca3c80045272eba`。
- 可迁移化来源：GitHub 远程分支 `challenge-cup-application`，本地远程跟踪引用为
  `origin/challenge-cup-application`，审计时为
  `e3a174bf25fac8113b81dc9cf8fe03bbd557c465`。
- 集成分支：`release/challenge-cup-portable`，从上述 `origin/main` 创建。
- 集成在独立 Git worktree 中完成，不切换或修改现有主工作区。
- 不改写两个来源分支的历史，不使用 force-push，不以可迁移化分支的完整目录树覆盖
  主分支。
- 通过普通 merge 保留双方历史；冲突必须按本设计逐项解决。

## 决策优先级

发生冲突或行为差异时，按以下优先级决策：

1. `main` 的公开 API、MQTT 报文、数据库语义、调度规则、最终决策和启动流程是功能
   契约，默认保留。
2. 可迁移化分支的场景注册、依赖注入、兼容转发、配置外置和路径解析是目标架构，
   在不改变第 1 项行为的前提下采用。
3. 当主分支实现已被可迁移化分支移动到 `scenarios/bearing` 时，不保留两份独立业务
   实现；把主分支最新逻辑移植到场景实现，旧路径只保留薄兼容入口。
4. 无法由现有测试证明等价的冲突不得通过简单选择整文件 `ours` 或 `theirs` 解决，
   必须增加或强化回归测试。

## 集成范围

### 平台与场景边界

保留可迁移化分支的 `core` 场景协议、场景注册表、启动装配和
`compatibility/bearing_v12`。轴承专有的推理、感知、汇总、全局分析和模型更新实现位于
`scenarios/bearing`，平台入口通过协议和 provider 使用它们。

旧模块导入路径继续有效，但只负责重新导出或委托。兼容入口不得复制业务算法、状态
机或存储规则，避免正式逻辑在新旧目录中分叉。

### 主分支功能同步

需要把 `main` 在可迁移化分支最后一次同步后新增的功能带入新结构，重点包括：

- `run_id` 的 Sender、Scheduler、Edge 和 Cloud 全链路传播；
- 最终决策合同与动作等级派生规则；
- Summary 存储职责拆分、同步合同和可观测性；
- 模型更新仓库、训练和待分发状态；
- 干净演示运行隔离及启动脚本改进。

### 数据包兼容

`run_id` 是可选字段。调用者未提供 `run_id` 时，构造结果和序列化字节必须保持主分支
既有行为；调用者提供非空 `run_id` 时才传播该字段。兼容入口与场景入口必须生成完全
相同的字典和二进制报文。所有接收端必须同时接受不含 `run_id` 的旧报文和包含合法
`run_id` 的新报文。

### 部署与资产

- Python 运行环境使用 Conda `moment`，Python 3.11.15 和仓库固定依赖。
- `EDGE_CONTROL_SHARED_SECRET` 必须由部署环境提供，至少 32 字节；预检不得创建密钥。
- Cloud MOMENT 模型资产不进入 Git，由发布资产包或受控下载步骤提供，并通过
  `CLOUD_MOMENT_*` 路径配置。
- H5 模型继续按仓库的 Git LFS/镜像约定交付。
- 默认路径必须相对项目根目录解析，绝对路径可由环境变量覆盖；运行不得依赖当前工作
  目录或开发者机器目录。
- Docker 镜像源码版本不一致时必须告警，正式展示前必须重建并验证目标 revision。

## 冲突处理分组

合并模拟发现 19 个内容冲突，按下列顺序处理：

1. Sender 与 Scheduler：数据包、`run_id`、期望包数量、任务分配和 MQTT 路由。
2. Cloud：应用装配、汇总合同、周期分析和模型更新服务。
3. Summary：动作评分、聚合、合同、存储、运行时、服务和建议生成；保留主分支存储拆分
   的语义，将轴承专有实现放入场景目录。
4. Edge：运行协调器和场景装配。
5. `start_project.ps1`：以主分支正式流程为基线，加入外部路径、预检和可迁移配置。

每组先运行该组件的主分支回归，再运行可迁移化契约测试，最后运行跨组件流程测试。

## 测试策略

### 合并前基线

在干净的 `origin/main` worktree、正确的临时控制密钥、外部 MOMENT 资产和 Windows 短
临时目录下，全量 pytest 结果为 `916 passed, 2 failed`：

- `test_edge_runtime_environment_keeps_existing_defaults`：测试期望 Scheduler 默认地址
  `8003`，实现为网络代理端口 `18011`。
- `test_two_sender_config_loads_the_two_configured_senders`：测试引用未纳入 Git 的
  `sender_module/config/local.two-senders.json`。

这两项是合并前已存在的基线偏差，不能归因于可迁移化合并；正式验收仍需将它们处理到
零失败。

### 自动化门禁

- 冲突解决期间按组件运行测试，任何新增失败必须在进入下一组前解决。
- 完整 pytest 使用 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`、短 `--basetemp` 和显式模型资产
  路径，最终必须 `0 failed, 0 errors`。
- `run_id` 必须覆盖缺省旧报文、合法新报文、空字符串拒绝、超长字符串拒绝和序列化
  字节稳定性。
- 架构测试必须证明平台核心不反向导入轴承实现，旧导入路径只通过兼容层访问场景代码。
- 虚拟电厂示例和 reference inspection 示例必须通过，证明新增场景不需要修改平台核心。

### 运行验收

1. `start_project.ps1 -CheckConfig -SkipLLM` 在展示配置下通过。
2. 重建 Edge 镜像并确认镜像 revision 与集成提交一致。
3. 启动 Network、两个 Edge、Summary、Scheduler 和 Cloud。
4. 使用固定输入执行 Sender 到 Cloud 的完整轴承流程，核对 API、MQTT、最终决策和数据库
   记录。
5. 停止全部组件后重新启动并重复流程，确认不依赖残留进程或历史数据库。
6. 在第二台干净机器或 VM 使用独立资产包和 `.env` 重复预检与核心演示。

## 完成标准

- `release/challenge-cup-portable` 同时包含指定的 main 基线和可迁移化提交历史。
- 19 个冲突均有明确解决结果，没有未合并文件或冲突标记。
- 自动化测试零失败，主分支正式功能不存在行为回退。
- 可迁移化专用架构、契约和新场景测试全部通过。
- Docker 全链路和干净机器部署验收通过。
- 模型资产、环境配置、启动和回滚步骤形成可复制的展示交付包。
- 通过 PR 合并回 `main`；合并前的 main 和最终展示提交分别打可回滚标签。
