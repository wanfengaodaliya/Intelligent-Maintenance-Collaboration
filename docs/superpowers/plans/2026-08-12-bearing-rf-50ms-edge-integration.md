# 50 ms Bearing Random-Forest Edge Integration Implementation Plan

基于`origin/main@d867bde`，依次完成：

1. TDD实现严格的27维`RandomForestDiagnosticModel`；
2. 增加显式运行时后端选择，默认行为保持mock；
3. 恢复聚焦的第一阶段训练构建器，生成仅供联调的D盘joblib与元数据；
4. 增加共用runner的单包CLI、JSON样例和说明；
5. 验证80包感知、推理及离线四窗口兼容性，并验证正式服务阻止临时模型进入最终聚合/发布，运行全套测试、哈希与泄漏审计；
6. 在D盘生成同学可直接使用的ZIP，不提交、不推送。

全程不读取K006、KA30、KI17，不修改输出和聚合合同，不把不合格模型描述为正式模型。
