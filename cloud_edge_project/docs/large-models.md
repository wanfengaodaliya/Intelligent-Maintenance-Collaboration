# 大模型资产说明（未上传至仓库）

本仓库不随 Git 分发体积较大的预训练权重与训练产物。以下资产均已被 `.gitignore` 忽略，仅保留在本地，不上传至 GitHub。

边缘端当前默认使用本地蒸馏 H5 模型（`model.edge_backend: "local_h5"`）。H5 制品同样不随 Git 分发，需要按 `edge_service/models/distilled_h5/active_version.json` 指向的版本目录放置 `best_model.pt`。下表中的 MOMENT 大模型用于云端 `moment_light_adapt` 复核链路。

## 未上传资产清单

| 资产 | 体积 | 用途 |
| --- | --- | --- |
| `model_assets/moment/pretrained/MOMENT-1-small/model.safetensors` | 约 145MB | MOMENT-1-small 时间序列基础模型（基于 `google/flan-t5-small`） |
| `model_assets/moment/releases/moment-scl05-final/best_model.pt` | 约 145MB | 当前 MOMENT SCL05 微调后的最终分类 checkpoint |
| `model_assets/moment/releases/moment-scl05-final/condition_norm.json` | 很小 | 当前模型使用的条件归一化参数 |
| `model_assets/moment/releases/moment-scl05-final/moment_model.py` | 很小 | 当前模型使用的部署代码 |

## 如何获取

### 1. MOMENT-1-small 预训练骨架

模型来源：Hugging Face `AutonLab/MOMENT-1-small`（MIT 协议）。可通过 `momentfm` 库自动下载：

```bash
pip install momentfm
```

```python
from momentfm import MOMENTPipeline

model = MOMENTPipeline.from_pretrained(
    "AutonLab/MOMENT-1-small",
    model_kwargs={"task_name": "classification", "n_channels": 1, "num_class": 3},
)
model.init()
```

也可使用 Hugging Face CLI 预下载到本仓库约定的目录：

```bash
hf download AutonLab/MOMENT-1-small --local-dir model_assets/moment/pretrained/MOMENT-1-small
```

### 2. LIGHT_ADAPT 微调 checkpoint

`best_model.pt` 与本仓库训练流水线产出的 `condition_norm.json` 为本地训练结果，不对外分发，需在具备训练数据的环境下重新训练生成。

## 外部路径配置

大模型位置支持通过环境变量覆盖，未设置时使用仓库内的相对默认路径。这样在未放置大模型的机器上也可将权重指向外部存储：

| 环境变量 | 默认值（相对 `PROJECT_ROOT`） |
| --- | --- |
| `CLOUD_MOMENT_PRETRAINED_PATH` | `model_assets/moment/pretrained/MOMENT-1-small` |
| `CLOUD_MOMENT_CHECKPOINT_PATH` | `model_assets/moment/releases/moment-scl05-final/best_model.pt` |
| `CLOUD_MOMENT_CONDITION_NORM_PATH` | `model_assets/moment/releases/moment-scl05-final/condition_norm.json` |
| `CLOUD_MOMENT_DEPLOYMENT_DIR` | `model_assets/moment/releases/moment-scl05-final` |
| `CLOUD_MOMENT_DEVICE` | `auto` |

## 边缘本地 H5 模型

当前边缘诊断默认路线为本地蒸馏 H5 模型：

- 配置值：`model.edge_backend: "local_h5"`；
- 制品根目录：`edge_service/models/distilled_h5/`；
- 版本选择：`active_version.json` 中的 `version`；
- 必需制品：`edge_service/models/distilled_h5/<version>/best_model.pt`。

`official` 是可选的正式模型服务后端，并非当前默认路线。早期随机森林模型仅作历史归档，不应作为当前边缘诊断后端。若本地 H5 诊断不可用，当前链路会将任务标记为待云端复核，而不是切换到随机森林模型。
