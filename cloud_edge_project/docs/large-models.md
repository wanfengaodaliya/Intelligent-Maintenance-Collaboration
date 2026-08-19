# 大模型资产说明（未上传至仓库）

本仓库不随 Git 分发体积较大的预训练权重与训练产物。以下资产均已被 `.gitignore` 忽略，仅保留在本地，不上传至 GitHub。

默认的小型随机森林模型（6.3MB）不受影响，依旧随仓库分发并可直接运行。下表中的 MOMENT 大模型仅用于增强型轴承复核链路（由 `legacy_context_enhanced_pipeline_enabled` / `moment_light_adapt` 控制，默认关闭），缺失时不改变默认运行行为。

## 未上传资产清单

| 资产 | 体积 | 用途 |
| --- | --- | --- |
| `experiments/diagnosis_models/moment/pretrained/MOMENT-1-small/model.safetensors` | 约 145MB | MOMENT-1-small 时间序列基础模型（基于 `google/flan-t5-small`） |
| `local_experiment/analysis/final_model/moment_final_chance/{SCL05,LIGHT_ADAPT_REPRO}/fold_3/best_model.pt` | 约 145MB × 2 | LIGHT_ADAPT 微调后的最终分类 checkpoint |

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
hf download AutonLab/MOMENT-1-small --local-dir experiments/diagnosis_models/moment/pretrained/MOMENT-1-small
```

### 2. LIGHT_ADAPT 微调 checkpoint

`best_model.pt` 与本仓库训练流水线产出的 `condition_norm.json` 为本地训练结果，不对外分发，需在具备训练数据的环境下重新训练生成。

## 外部路径配置

大模型位置支持通过环境变量覆盖，未设置时使用仓库内的相对默认路径。这样在未放置大模型的机器上也可将权重指向外部存储：

| 环境变量 | 默认值（相对 `PROJECT_ROOT`） |
| --- | --- |
| `CLOUD_MOMENT_PRETRAINED_PATH` | `experiments/diagnosis_models/moment/pretrained/MOMENT-1-small` |
| `CLOUD_MOMENT_CHECKPOINT_PATH` | `local_experiment/analysis/final_model/moment_final_chance/SCL05/fold_3/best_model.pt` |
| `CLOUD_MOMENT_CONDITION_NORM_PATH` | `local_experiment/analysis/final_model/moment_final_chance/SCL05/fold_3/condition_norm.json` |
| `CLOUD_MOMENT_DEPLOYMENT_DIR` | `local_experiment/deploy/light_adapt` |
| `CLOUD_MOMENT_DEVICE` | `auto` |

## 默认小模型（已废弃，仅存档说明）

> **阶段 6 收口（2026-08）**：边缘本地诊断模型路线已全部淘汰，以下资产仅作历史归档，
> **不可运行**，仓库中已删除对应代码与制品：
>
> - `edge_service/models/bearing_random_forest/random_forest.joblib`（早期随机森林模型）
> - `edge_service/models/distilled_h5/`（蒸馏三分支 H5 模型，含 `best_model.pt`）
> - `edge_service/src/edge_diagnosis/`（H5 推理运行时代码）
>
> 当前边缘诊断唯一路线为**正式模型服务**（`model.edge_backend: "official"`，
> HTTP `/infer`，见 `edge_service/src/model_service/`）。降级语义为"诊断不可用/待云复核"，
> 不允许回退旧模型。训练流水线产出的 checkpoint 归档见上文 LIGHT_ADAPT 章节，
> 与边缘运行时不再耦合。