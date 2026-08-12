# 50 ms轴承随机森林边缘临时接入设计

本设计以`origin/main@d867bde`为代码锚点：用可选的
`RandomForestDiagnosticModel`替换当前`MockDiagnosticModel`，使每个50 ms
感知结果生成现有`EdgeResult`。由于模型未通过质量门槛，正式服务必须阻止其进入
`FINAL_EDGE`窗口与设备发布；现有20包窗口兼容性只在离线技术测试中验证。

模型只允许链路联调，元数据必须记录`qualified_for_deployment=false`、
`allowed_use=pipeline_integration_only`以及`locked_test_consumed=false`。

运行时通过以下变量显式选择：

```text
EDGE_DIAGNOSTIC_BACKEND=mock|rf_50ms_integration
EDGE_RF_MODEL_PATH=<D盘joblib路径>
EDGE_RF_METADATA_PATH=<D盘metadata路径>
```

默认仍使用mock。显式选择RF时，缺文件、哈希错误、27维特征顺序错误、缺字段或
非有限数值均拒绝启动或推理，不补数据、不静默回退。现有`EdgeResult`、窗口聚合和
设备聚合合同保持不变。具体三分类只进入诊断记录与CLI输出。

训练构建器只读取开发集，硬性拒绝锁定轴承ID，并要求调用方提供匹配的源文件SHA-256。
只有规范源哈希才绑定第一阶段A2指标，不运行锁定测试。joblib、
元数据、日志和交付ZIP均写D盘且不提交Git。完整验收包括单元测试、80包感知与
推理、四个现有20包窗口、哈希校验以及锁定分组未使用证明。
