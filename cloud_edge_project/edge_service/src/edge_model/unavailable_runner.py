# -*- coding: utf-8 -*-
"""阶段 6 收口后的降级执行器：诊断不可用，不产生伪诊断。

方案 7.3 失败语义：正式模型忙、排队超时、推理超时、输出合同非法或服务
不可用时，达到业务截止时间后必须返回"诊断不可用/待云复核"，不允许复用
已淘汰模型产生看似正常的诊断结果。

实现：run() 恒定抛出 ModelUnavailable，由 pipeline 记录
CODE_FALLBACK_FAILED 的包级 FAILED 终态（error_code=REASON_CODE_FALLBACK_FAILED），
下游按超时/云复核路径收敛，而非拿到一个 normal/fault 的伪结果。
"""
from __future__ import annotations

from .code_fallback import CodeFallbackRunner
from .contracts import EdgeResult, PacketInferenceTask


class DiagnosisUnavailableRunner(CodeFallbackRunner):
    """正式模型路线唯一合法 fallback：明确失败，等待云复核。"""

    def __init__(self, rule_version: str = "diagnosis_unavailable_v1"):
        self.rule_version = rule_version
        self.model_version = "unavailable"
        self.deployment_status = "no_local_model"

    def run(self, task: PacketInferenceTask) -> EdgeResult:
        raise RuntimeError(
            "MODEL_UNAVAILABLE: edge diagnosis is only served by the official "
            "model service; degraded packets must fail explicitly for cloud review"
        )
