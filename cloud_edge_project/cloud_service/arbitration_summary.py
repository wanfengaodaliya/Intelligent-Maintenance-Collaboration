"""LLM-only explanation layer; it never changes structured arbitration fields."""
from __future__ import annotations
import os, time
from pathlib import Path
from typing import Any
from cloud_service.storage.database import connect, initialize_database
import requests

class ArbitrationSummaryService:
    def __init__(self, database_path: Path): self.database_path=Path(database_path); initialize_database(self.database_path)
    def summarize(self, result: dict[str, Any]) -> dict[str, Any]:
        try:
            summary, advice=self._generate(result)
            status,error="succeeded",None
        except Exception:
            summary,advice,status,error=None,None,"failed","LLM_SUMMARY_FAILED"
        with connect(self.database_path) as c:
            c.execute("INSERT INTO arbitration_summary(arbitration_id,status,summary,maintenance_advice,error_code,created_at_ns) VALUES (?,?,?,?,?,?) ON CONFLICT(arbitration_id) DO NOTHING",(result['arbitration_id'],status,summary,advice,error,time.time_ns()))
        return {"summary_status":status,"summary":summary,"maintenance_advice":advice}
    @staticmethod
    def _generate(result: dict[str, Any]) -> tuple[str,str]:
        if os.getenv('CLOUD_BACKEND','mock').lower() == 'mock':
            return (f"设备仲裁结果：{result['final_state']}，建议执行 {result['final_action']}。", "按仲裁动作安排维护并保留复核记录。")
        if os.getenv('CLOUD_BACKEND','').lower() != 'vllm':
            raise RuntimeError('LLM backend is unavailable')
        response=requests.post(os.getenv('VLLM_URL','http://127.0.0.1:6006/v1/chat/completions'), json={"model":os.getenv('VLLM_MODEL_NAME','qwen-cloud'),"messages":[{"role":"user","content":f"仅解释以下已确定仲裁结果，不得修改状态、动作或置信度：{result}"}],"temperature":0}, timeout=float(os.getenv('VLLM_TIMEOUT_SECONDS','120')))
        response.raise_for_status(); content=response.json()['choices'][0]['message']['content'].strip()
        return content, "请依照已持久化的仲裁动作安排维护。"
