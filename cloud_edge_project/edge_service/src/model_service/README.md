# 边缘真实模型服务启动与协作指南

本文说明如何在 Windows + WSL2 中使用 Conda 环境启动真实 Qwen 模型服务，并从 Windows 边缘程序完成真实模型最小闭环验证。

当前服务用于技术闭环和开发测试。合成数据闭环通过不代表真实轴承诊断准确率已经得到验证；在取得健康轴承、故障轴承和现场噪声数据前，不得把模型输出描述为经过业务验证的诊断结论。

## 1. 当前统一运行时

以下为项目统一`moment`环境的当前冻结版本。Qwen 已完成导入与GPU能力预检；实际权重加载和完整闭环仍须按本文验收清单执行：

| 项目 | 已验证值 |
|---|---|
| Windows/WSL | WSL2，Ubuntu |
| Conda环境 | `moment`（项目统一环境） |
| Python | `3.11.15` |
| PyTorch | `2.13.0+cu130` |
| CUDA Runtime | `13.0` |
| GPU | NVIDIA GeForce RTX 5060 Laptop GPU |
| BF16 | 支持 |
| Transformers | `5.15.0` |
| Accelerate | `1.14.0` |
| Hugging Face Hub | `1.27.0` |
| Safetensors | `0.8.0` |
| 基础模型 | `Qwen/Qwen2.5-1.5B-Instruct` |
| 项目模型ID | `edge-bearing-qwen` |
| 项目模型版本 | `qwen2.5-1.5b-instruct/phase1` |
| 推理精度 | `bfloat16` |
| 量化 | 无 |
| 最大输出 | 64 tokens |
| 服务地址 | `http://127.0.0.1:8001` |

项目运行时统一使用`moment`环境及`cloud_edge_project/requirements-moment.txt`。Qwen 在该环境已完成导入与GPU能力预检；首次使用某份实际权重时仍须完成一次完整加载和最小闭环验证。

## 2. 运行结构

```text
Windows边缘程序
  └─ EdgeModelPipeline / ModelClient
       └─ HTTP http://127.0.0.1:8001
            └─ WSL2 Ubuntu
                 └─ moment Conda环境
                      └─ src.model_service.app
                           └─ Qwen2.5-1.5B-Instruct（GPU/BF16）
```

Windows侧负责逐包任务封装、有界队列、超时、熔断和代码替代路线。WSL服务只负责加载模型、GPU预热、推理以及输出结构校验。

模型服务内部只有一把非阻塞推理锁。同一时刻只执行一次`generate()`；服务忙时立即返回`MODEL_BUSY`，不会在WSL侧形成第二条隐式队列。

## 3. 准备WSL和Conda环境

### 3.1 使用统一`moment`环境

在Windows PowerShell进入WSL：

```bash
wsl -d Ubuntu
```

激活环境：

```bash
source ~/miniconda3/etc/profile.d/conda.sh
conda activate moment
```

确认解释器和关键依赖：

```bash
echo "CONDA_ENV=$CONDA_DEFAULT_ENV"
which python
python --version
python -m pip show torch transformers accelerate huggingface-hub safetensors
```

确认GPU：

```bash
nvidia-smi
```

```bash
python -c 'import torch; print("torch =", torch.__version__); print("cuda_available =", torch.cuda.is_available()); print("cuda_version =", torch.version.cuda); print("gpu =", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None); print("bf16 =", torch.cuda.is_bf16_supported() if torch.cuda.is_available() else False)'
```

必须满足：

```text
cuda_available = True
bf16 = True
```

### 3.2 新协作者准备环境

先安装支持WSL的NVIDIA驱动、WSL2、Ubuntu和Miniconda。不要在WSL内部另外安装Linux NVIDIA显示驱动；WSL使用Windows宿主机提供的GPU驱动接口。

创建环境：

```bash
source ~/miniconda3/etc/profile.d/conda.sh
conda create -n moment python=3.11.15 -y
conda activate moment
```

在仓库根目录的`cloud_edge_project`目录安装唯一依赖源：

```bash
python -m pip install -r requirements-moment.txt
```

安装后必须重新执行GPU检查。若包仓库不再提供完全相同的版本，不要自行选择“最新版本”后宣称环境等价，应记录实际版本并重新执行模型加载、可用性检查和真实闭环测试。

## 4. 准备模型制品

### 4.1 当前冻结制品

推荐放在每位协作者自己的WSL主目录：

```text
$HOME/models/Qwen2.5-1.5B-Instruct
```

当前目录约2.9 GiB，至少包含：

```text
config.json
generation_config.json
model.safetensors
tokenizer.json
tokenizer_config.json
vocab.json
merges.txt
```

当前本地目录没有保留Hugging Face snapshot revision或Git提交信息，因此不能从现有目录证明上游仓库的准确commit。当前阶段使用本地制品SHA-256冻结同一份模型：

| 文件 | SHA-256 |
|---|---|
| `model.safetensors` | `dd924a11b4c220f385b51ffa522daea7c9f3d850e31b162bb5661df483c6d3ee` |
| `config.json` | `98d2ff8cc47488d08a2b0b3acf4eb99ef210779b42bd48605f6b8e36acdbf670` |
| `tokenizer.json` | `c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539` |
| `tokenizer_config.json` | `5b5d4f65d0acd3b2d56a35b56d374a36cbc1c8fa5cf3b3febbbfabf22f359583` |

协作者收到模型后执行：

```bash
export EDGE_MODEL_PATH="$HOME/models/Qwen2.5-1.5B-Instruct"

sha256sum \
  "$EDGE_MODEL_PATH/model.safetensors" \
  "$EDGE_MODEL_PATH/config.json" \
  "$EDGE_MODEL_PATH/tokenizer.json" \
  "$EDGE_MODEL_PATH/tokenizer_config.json"
```

四个哈希必须与上表完全一致。模型权重不能提交到本项目Git仓库。

### 4.2 模型共享方式

推荐顺序：

1. 从项目批准的内部文件存储复制当前冻结目录；
2. 校验上述SHA-256；
3. 仅在明确允许联网时，从官方仓库按固定revision下载。

如果使用内部文件服务器，可以把完整目录复制到协作者的`$HOME/models`。不要只复制`safetensors`权重，Tokenizer和配置文件必须作为一个完整制品一起分发。

如果允许从Hugging Face下载，必须先确定不可变revision，不要长期使用会变化的`main`：

```bash
export MODEL_REPO=Qwen/Qwen2.5-1.5B-Instruct
export MODEL_REVISION="填写项目确认的Hugging_Face_commit_SHA"
export EDGE_MODEL_PATH="$HOME/models/Qwen2.5-1.5B-Instruct"

python -c 'import os; from huggingface_hub import snapshot_download; print(snapshot_download(repo_id=os.environ["MODEL_REPO"], revision=os.environ["MODEL_REVISION"], local_dir=os.environ["EDGE_MODEL_PATH"]))'
```

下载后重新生成制品哈希和模型清单。新的哈希未经评审时，不能继续使用现有`qwen2.5-1.5b-instruct/phase1`版本名冒充同一制品。

模型的复制、共享和使用还必须遵守基础模型许可证及组织内部的数据和软件分发要求。

## 5. 离线完整性检查

设置模型路径和离线模式：

```bash
export EDGE_MODEL_PATH="$HOME/models/Qwen2.5-1.5B-Instruct"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
```

确认目录：

```bash
realpath "$EDGE_MODEL_PATH"
du -sh "$EDGE_MODEL_PATH"
```

离线读取配置和Tokenizer：

```bash
python -c 'import os; from transformers import AutoConfig, AutoTokenizer; p=os.environ["EDGE_MODEL_PATH"]; c=AutoConfig.from_pretrained(p, local_files_only=True, trust_remote_code=True); t=AutoTokenizer.from_pretrained(p, local_files_only=True, trust_remote_code=True); print("config =", type(c).__name__); print("tokenizer =", type(t).__name__); print("vocab =", len(t))'
```

预期至少包含：

```text
config = Qwen2Config
tokenizer = Qwen2TokenizerFast
```

这一步只检查配置和Tokenizer，不能替代完整权重加载、GPU预热和模型输出检查。

## 6. 启动模型服务

进入项目在WSL中的路径。下面以Windows仓库`D:\Projects\edge`为例：

```bash
cd /mnt/d/Projects/edge
```

确认仍在正确环境：

```bash
echo "$CONDA_DEFAULT_ENV"
echo "$EDGE_MODEL_PATH"
```

前台启动：

```bash
python -m src.model_service.app \
  --model "$EDGE_MODEL_PATH" \
  --host 127.0.0.1 \
  --port 8001 \
  --dtype bfloat16 \
  --max-new-tokens 64
```

启动会依次执行：

```text
模型关键文件检查
→ Tokenizer和权重加载
→ GPU短预热
→ 一次完整轴承特征推理
→ JSON结构和字段合法性检查
→ readiness=true
```

只有看到下面内容才表示服务可用：

```text
模型服务就绪: http://127.0.0.1:8001
```

保持该终端运行。开发阶段建议使用`tmux`保留会话：

```bash
tmux new -s edge-model
```

在`tmux`中运行启动命令；按`Ctrl+B`后按`D`退出会话，重新进入使用：

```bash
tmux attach -t edge-model
```

停止服务时回到服务终端按`Ctrl+C`。不要通过模糊匹配批量终止Python进程。

## 7. 健康检查

打开第二个WSL终端：

```bash
curl http://127.0.0.1:8001/health
curl http://127.0.0.1:8001/readiness
```

必须得到：

```json
{"status": "ok"}
{"ready": true, "load_error": null}
```

含义不同：

- `/health`只说明HTTP进程存活；
- `/readiness`说明模型加载、GPU预热和完整合法JSON推理均已通过；
- 客户端只能在两者均通过后把该服务视为真实模型路线可用。

Windows PowerShell也可以检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
Invoke-RestMethod http://127.0.0.1:8001/readiness
```

## 8. 运行真实模型最小闭环

模型服务保持运行，在Windows PowerShell进入仓库：

```powershell
Set-Location D:\Projects\edge
```

执行：

```powershell
.\.venv\Scripts\python.exe scripts\minimal_local_loop.py --model-mode real
```

测试数据来源固定标记为：

```text
synthetic_development_test
```

程序生成一个轴承的80个数学信号包，并真实执行：

```text
任务派发
→ 任务匹配
→ 严格校验
→ 原始环形缓存
→ FIR降采样
→ 感知特征
→ 80个独立包级模型任务
→ WSL真实Qwen逐包推理
→ 80个PacketResult
```

真实模型通过必须同时满足：

```text
status = PASS
model_service_health = true
model_service_readiness = true
ingress_accepted_packets = 80
cache_available_slots = 80
downsampled_packets = 80
perceived_packets = 80
bearing_data_completeness = COMPLETE
model_packet_tasks = 80
unique_model_request_ids = 80
packet_results = 80
execution_modes = {"LOCAL_MODEL": 80}
fallback_reasons = {}
model_versions = {"qwen2.5-1.5b-instruct/phase1": 80}
```

只要出现`CODE_FALLBACK`或非空`fallback_reasons`，就不能宣称真实模型闭环通过。
该最小闭环会等待上一包完成后再提交下一包，只验证逐包正确性，不验证`20包/秒`吞吐。

当前闭环尚未包含包摘要、`BearingTaskResult`和`DeviceTaskResult`，因此报告中的`device_result_generated=false`是预期结果。

## 9. 生成模型清单

模型路径、代码、提示词和输出契约稳定后生成Manifest：

```bash
cd /mnt/d/Projects/edge
export EDGE_MODEL_PATH="$HOME/models/Qwen2.5-1.5B-Instruct"

python src/model_service/manifest.py \
  --out var/model_manifest.json \
  --model-id edge-bearing-qwen \
  --model-version qwen2.5-1.5b-instruct/phase1 \
  --base-model Qwen/Qwen2.5-1.5B-Instruct \
  --max-new-tokens 64
```

Manifest记录权重哈希、Tokenizer哈希、Python、PyTorch、Transformers、CUDA、GPU、Prompt版本和输出Schema版本。当前脚本会把`base_model_revision`写为`unknown`；在后续重新获取模型时，必须把下载时使用的Hugging Face commit SHA作为独立发布信息保存，并改进Manifest生成流程后再冻结新版本。

## 10. 多人协作建议

### 10.1 推荐：每位开发者本地运行

当前最安全、最容易复现的方式是：

1. 每位开发者在自己的WSL2中使用统一的`moment`环境；
2. 从批准的内部存储获得同一模型目录；
3. 校验SHA-256；
4. 只监听自己的`127.0.0.1:8001`；
5. 运行真实最小闭环确认环境一致。

这样不需要把无认证的开发服务暴露到局域网，也不会让多人请求争抢同一把推理锁。

### 10.2 暂不推荐：直接共享一台GPU服务

当前`src.model_service.app`基于开发用HTTP服务，没有身份认证、TLS、租户隔离或请求持久队列。不要直接使用`--host 0.0.0.0`把8001端口暴露到公司网络或互联网。

如果将来必须共享一台GPU主机，至少需要先补充：

- 反向代理和TLS；
- 身份认证与访问控制；
- 防火墙和允许来源列表；
- 请求限流；
- 服务日志和审计；
- 模型版本发布与回滚；
- 明确的`MODEL_BUSY`客户端重试或降级策略；
- GPU服务进程托管和健康恢复。

在这些能力实现前，共享模型权重制品、由每位协作者本地启动服务，是当前推荐方案。

## 11. 已知警告

### 11.1 C++扩展版本警告

旧的`model-train`基线可能输出：

```text
Skipping import of cpp extensions due to incompatible torch version.
Please upgrade to torch >= 2.11.0 (found 2.10.0+cu128).
```

统一`moment`环境使用Torch 2.13.0+cu130，满足该扩展的最低版本要求；若仍出现警告，应以实际权重加载和最小闭环结果为准。

### 11.2 生成参数被忽略

模型制品的`generation_config.json`包含`temperature`、`top_p`和`top_k`，而项目运行器明确使用：

```text
do_sample = false
```

因此Transformers可能提示这些采样参数无效并被忽略。这符合当前确定性JSON生成设计，不是推理失败。

## 12. 常见问题

### `KeyError: EDGE_MODEL_PATH`

当前终端没有设置模型路径：

```bash
export EDGE_MODEL_PATH="$HOME/models/Qwen2.5-1.5B-Instruct"
```

### `cuda_available = False`

不要启动服务。检查Windows NVIDIA驱动、WSL版本、`nvidia-smi`和PyTorch CUDA轮子。CPU模式不是当前已验证部署路线。

### `/health`成功但`/readiness`失败

HTTP进程仍在，但模型未完成加载或完整输出检查失败。查看启动终端中的`load_error`，不要让Windows客户端把它当作可用模型。

### Windows访问8001超时

先在WSL内部执行：

```bash
curl http://127.0.0.1:8001/health
ss -ltnp | grep ':8001'
```

若WSL内部成功、Windows失败，检查WSL localhost forwarding和Windows防火墙。仅在可信本机开发环境且理解安全风险时，才考虑把监听地址临时改为`0.0.0.0`；当前服务没有认证和TLS，不能直接暴露到非可信网络。

### 端口已占用

只读检查占用者：

```bash
ss -ltnp | grep ':8001'
```

确认是自己启动的旧服务后，回到旧服务终端使用`Ctrl+C`停止。不要批量终止所有Python进程。

### 测试出现`CODE_FALLBACK`

查看报告中的`fallback_reasons`。常见原因包括：

```text
MODEL_UNAVAILABLE
MODEL_INFERENCE_TIMEOUT
MODEL_OUTPUT_INVALID
MODEL_BUSY
QUEUE_TIMEOUT
```

只要发生降级，技术链路可能仍会产生合法结果，但真实模型路线验收应判定失败。

## 13. 协作者验收清单

新环境交付前逐项确认：

- [ ] 已激活统一的`moment`环境并安装`requirements-moment.txt`；
- [ ] Python和关键运行库版本已记录；
- [ ] `torch.cuda.is_available()`为`True`；
- [ ] GPU名称和CUDA版本已记录；
- [ ] 模型目录关键文件齐全；
- [ ] 四个SHA-256与冻结制品一致；
- [ ] 离线Config和Tokenizer加载成功；
- [ ] 模型服务启动时完整可用性检查通过；
- [ ] `/health`返回`ok`；
- [ ] `/readiness`返回`ready=true`；
- [ ] 最小闭环产生80个独立`LOCAL_MODEL`包级记录；
- [ ] 80个包具有80个唯一`request_id`并与结果一一对应；
- [ ] `fallback_reasons`为空；
- [ ] 模型版本为`qwen2.5-1.5b-instruct/phase1`；
- [ ] 明确记录当前只完成技术闭环，不宣称真实轴承诊断准确率。
