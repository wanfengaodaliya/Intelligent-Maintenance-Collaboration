# 50 ms 边缘诊断模型接线说明

此实现用于把当前 50 ms 感知特征接入边缘流水线，验证接口和数据链路。它不是正式部署模型：开发集轴承级交叉验证 Macro-F1 为 `0.569355`，没有通过质量门槛，锁定最终测试集也没有被用于调参或评估。

## 运行位置

数据路径为：原始包 -> 校验与缓存 -> 感知模块产生 27 维特征 -> 随机森林单包诊断。离线技术测试可以继续验证现有 20 包窗口兼容性；正式服务会阻止这个不合格模型进入 `FINAL_EDGE` 聚合和设备级发布。

模型输出三类：`healthy`、`outer_ring_damage`、`inner_ring_damage`。为兼容现有 `EdgeResult`，健康映射为 `normal/low`，两类损伤映射为 `fault/high`；具体位置只保留在单包 CLI/适配器输出的 `diagnosis_label` 中，冻结的四字段正式接口暂不携带故障位置。

该模型复用当前进程内备用运行器通道，因此运行记录中的 `execution_mode` 仍为 `CODE_FALLBACK`；是否真的使用随机森林，应以 `model_version=bearing-rf-50ms-integration-only-v1` 判断。

## 启用方式

默认仍使用 mock，不配置环境变量不会改变现有行为。启用临时模型需要设置：

```powershell
$env:EDGE_DIAGNOSTIC_BACKEND = "rf_50ms_integration"
$env:EDGE_RF_MODEL_PATH = "D:\path\random_forest_integration_only.joblib"
$env:EDGE_RF_METADATA_PATH = "D:\path\model_metadata.json"
```

单包调试：

```powershell
python scripts/rf_packet_diagnose.py `
  --input examples/rf_50ms/perception_result.json `
  --model D:\path\random_forest_integration_only.joblib `
  --metadata D:\path\model_metadata.json
```

80 包边缘技术链路：

```powershell
python scripts/minimal_local_loop.py `
  --model-mode rf-integration `
  --model-path D:\path\random_forest_integration_only.joblib `
  --metadata-path D:\path\model_metadata.json
```

## 产物边界

模型必须和 `model_metadata.json` 一起使用，并且只能加载来自可信同学或本项目构建脚本的 joblib 文件。加载时会校验 SHA-256、27 维特征顺序、类别列表以及 `qualified_for_deployment=false`。模型文件属于本地生成产物，不提交 Git；安装 `requirements-rf-build.txt` 后，可以用 `scripts/build_rf_integration_artifact.py` 从开发特征表重建。

构建时必须传入事先核对的特征表 SHA-256；构建器还会硬性拒绝 K006、KA30、KI17 出现在开发行中。只有已知的规范特征表哈希才会附带第一阶段 A2 交叉验证指标，其他特征表会明确记为未评估。

后续真正通过质量门槛的 20 包模型会替换此单包运行器，届时才能解除正式聚合/发布保护；感知接口、运行时选择方式和现有轴承窗口聚合可以继续保留。
