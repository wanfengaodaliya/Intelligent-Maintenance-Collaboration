# AutoDL 云端模型服务部署说明

## 1. 运行结构

AutoDL 上运行两个长期服务：

```text
start_vllm.sh
    -> vLLM 127.0.0.1:6006
    -> Qwen3-14B-AWQ

start_cloud_service.sh
    -> FastAPI 0.0.0.0:6008
    -> POST /cloud/infer
    -> 调用本机 vLLM
```

6006 只供 AutoDL 内部的云端业务服务调用；团队和调度器只访问映射到 6008 的 `/cloud/infer`。

## 2. 推荐目录

```text
/root/autodl-tmp/
├── models/
│   └── Qwen3-14B-AWQ/
└── Intelligent-Maintenance-Collaboration/
    └── cloud_edge_project/
        ├── cloud_service/
        ├── scripts/
        └── requirements.txt
```

模型权重保留在数据盘，不提交到 GitHub。应用代码从 GitHub 拉取，不再手工维护 `/root/autodl-tmp/model_server/cloud_service` 中的另一份副本。

## 3. 第一次部署

进入数据盘并克隆仓库：

```bash
cd /root/autodl-tmp
git clone https://github.com/wanfengaodaliya/Intelligent-Maintenance-Collaboration.git
cd Intelligent-Maintenance-Collaboration/cloud_edge_project
```

激活已经安装 vLLM 的环境并安装业务依赖：

```bash
source /root/miniconda3/bin/activate cloud_llm
pip install -r requirements.txt
```

给两个脚本增加执行权限：

```bash
chmod +x scripts/start_vllm.sh
chmod +x scripts/start_cloud_service.sh
```

确认模型文件存在：

```bash
ls /root/autodl-tmp/models/Qwen3-14B-AWQ
```

目录中应包含 `config.json`、tokenizer 文件和 `.safetensors` 权重文件。

## 4. 启动顺序

终端一进入仓库中的项目目录并启动模型：

```bash
cd /root/autodl-tmp/Intelligent-Maintenance-Collaboration/cloud_edge_project
./scripts/start_vllm.sh
```

等待日志显示 vLLM 启动完成。然后打开终端二：

```bash
cd /root/autodl-tmp/Intelligent-Maintenance-Collaboration/cloud_edge_project
./scripts/start_cloud_service.sh
```

第二个脚本会自动设置：

```text
CLOUD_BACKEND=vllm
```

因此同一套 `cloud_service` 在 AutoDL 上会调用真实模型，而本地未设置该变量时默认使用 mock。

## 5. 服务器内部测试

检查 vLLM：

```bash
curl http://127.0.0.1:6006/v1/models
```

检查云端业务服务：

```bash
curl http://127.0.0.1:6008/health
```

预期健康结果包含：

```json
{
  "service": "cloud_service",
  "node_id": "cloud_1",
  "status": "ok",
  "port": 6008,
  "model_backend": "vllm"
}
```

`port` 与启动脚本实际监听的 6008 保持一致。

使用仓库中的合法800点示例测试 `/cloud/infer`：

```bash
cd /root/autodl-tmp/Intelligent-Maintenance-Collaboration/cloud_edge_project
python - <<'PY'
import json
import requests

with open("examples/sensor_packet_abnormal.json", encoding="utf-8") as file:
    packet = json.load(file)

response = requests.post(
    "http://127.0.0.1:6008/cloud/infer",
    json={
        "packet": packet,
        "edge_result": {
            "label": "abnormal",
            "confidence": 0.72,
            "risk_level": "medium",
        },
    },
    timeout=180,
)
response.raise_for_status()
print(json.dumps(response.json(), ensure_ascii=False, indent=2))
PY
```

## 6. 公网调用

在 AutoDL 控制台中找到映射到实例6008端口的公网服务地址。调度器设置：

```text
CLOUD_SERVICE_URL=AutoDL提供的公网地址
```

调度器随后调用：

```text
POST ${CLOUD_SERVICE_URL}/cloud/infer
```

不要把6006上的vLLM接口直接暴露给队友。系统提示词、请求校验、输出格式和错误处理都由6008上的云端业务服务负责。

## 7. 更新代码

本地代码推送到 GitHub 后，AutoDL 执行：

```bash
cd /root/autodl-tmp/Intelligent-Maintenance-Collaboration
git pull --ff-only origin main
```

如果 Python 依赖发生变化，再执行：

```bash
cd cloud_edge_project
pip install -r requirements.txt
```

重启两个服务后，新代码生效。

## 8. 可选环境变量

代码提供以下默认值，一般不需要修改：

```text
VLLM_URL=http://127.0.0.1:6006/v1/chat/completions
VLLM_MODEL_NAME=qwen-cloud
VLLM_TIMEOUT_SECONDS=120
```

需要鉴权时，在启动云端业务服务前单独设置：

```bash
export VLLM_API_KEY="实际密钥"
```

禁止把真实密钥写入脚本或提交到 GitHub。
