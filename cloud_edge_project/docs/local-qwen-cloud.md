# 本地 Qwen 云端联调

本地联调保持模型服务与业务项目解耦：Qwen/vLLM 在 WSL 中监听 `127.0.0.1:6006`，本项目的 `cloud_service` 在 Windows 上监听 `127.0.0.1:6008`。

## 1. 启动模型服务

模型文件位于 WSL 外部目录，不提交到仓库：

```bash
/home/jason/cloud_local_service/start_vllm.sh
```

该脚本使用 conda 环境 `local_vllm`，默认模型为 `/home/jason/aigc_project/Qwen3.5-2B/Qwen/Qwen3___5-2B`，服务模型名为 `qwen3.5-2b-local`。针对 RTX 50 系列及 8GB 显存的本机联调，脚本会禁用不兼容的 FlashInfer 采样器，并限制为 64 个并发序列。

服务就绪后运行：

```bash
/home/jason/miniconda3/bin/conda run --no-capture-output -n local_vllm \
  python /home/jason/cloud_local_service/smoke_test.py
```

预期输出：

```json
{"model": "qwen3.5-2b-local", "status": "ok"}
```

## 2. 启动项目云端服务

从仓库根目录的 PowerShell 运行：

```powershell
.\cloud_edge_project\scripts\start_cloud_service_local.ps1
```

服务只监听 `127.0.0.1:6008`。检查：

```powershell
Invoke-RestMethod http://127.0.0.1:6008/health
```

预期 `model_backend` 是 `vllm`，`port` 是 `6008`。

## 3. 验证项目推理

向 `POST http://127.0.0.1:6008/cloud/infer` 发送合法 V0.1 `CloudRequest`。成功响应必须包含：

- `model_name: qwen3.5-2b-local`；
- `label: normal` 或 `abnormal`；
- 0 到 1 的 `confidence`；
- 合法 `risk_level` 和 `decision`。

`CLOUD_BACKEND=mock` 仍是默认值，供确定性测试使用。只有显式通过本脚本设置 `CLOUD_BACKEND=vllm` 时，V0.1 和轴承单包复核才调用本地模型。模型连接失败或返回不符合 JSON 契约时，云端服务返回明确错误，不会伪造规则模型结果。
