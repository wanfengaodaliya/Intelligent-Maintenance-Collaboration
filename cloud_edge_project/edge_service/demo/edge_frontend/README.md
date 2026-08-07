# 边缘逐包诊断演示页

这个页面用于演示一个数据包在边缘节点中的四个阶段：

```text
原始数据输入 → 感知模块输出 → 模型/代码降级输出 → 最终 EdgeResult
```

页面和 API 只使用 Python 标准库提供 HTTP 服务，不增加前端构建工具或第三方 Web 依赖。数据由现有最小闭环中的可重复合成信号产生；任务接入、校验缓存、降采样、感知和边缘模型流水线均调用项目当前实现。

## 默认启动：开发测试模式

在 Windows PowerShell 的仓库根目录执行：

```powershell
.venv\Scripts\python.exe scripts\edge_demo_server.py
```

浏览器打开：

```text
http://127.0.0.1:8088
```

默认使用 `fallback` 模式。感知特征来自真实感知模块；当模型服务不可用时，最终结果来自 `edge_rule_test_v1` 开发测试规则。页面会明确显示 `CODE_FALLBACK`，该结果不能解释为真实轴承诊断结论。

## 接入当前 Transformers 模型服务

先按照 `src/model_service/README.md` 在 WSL 中启动模型服务，并确认以下地址可用：

```text
http://127.0.0.1:8001/health
http://127.0.0.1:8001/readiness
```

然后启动演示页：

```powershell
.venv\Scripts\python.exe scripts\edge_demo_server.py --model-mode real
```

`real` 模式会在启动时检查模型服务；模型未就绪时直接停止，不会悄悄切换为伪造模型输出。

## 页面操作

- `处理下一包`：同步处理一个新数据包。
- `连续演示`：按照选择的间隔连续处理，最多 80 包。
- `暂停演示`：当前包处理完成后暂停。
- `重置`：新建一个 80 包演示任务并清空页面记录。
- 左侧逐包记录：点击任意已处理数据包，重新查看该包的四阶段快照。

显示的原始通道值是波形预览，不会把每包全部原始数组发送给浏览器；采样率、采样点数和其他输入字段仍按真实结构展示。
