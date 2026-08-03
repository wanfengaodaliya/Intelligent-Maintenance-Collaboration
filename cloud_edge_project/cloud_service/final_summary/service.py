"""Persist final diagnostic summaries from structured enhanced-analysis evidence."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from cloud_service.config import load_cloud_settings
from cloud_service.enhanced_analysis.contracts import EnhancedAnalysisResult
from cloud_service.storage.database import connect


class FinalSummaryService:
    def __init__(self, database_path: Path, *, backend: str | None = None):
        settings = load_cloud_settings()
        self.database_path = Path(database_path)
        self.backend = backend or settings.backend
        self.settings = settings

    def summarize(self, result: EnhancedAnalysisResult) -> dict[str, Any]:
        existing = self.get(result.review_id)
        if existing is not None:
            return existing
        try:
            llm_input = self._llm_input(result)
            if self.backend == "mock":
                summary = _mock_summary(result, llm_input["feature_context"])
            elif self.backend == "vllm":
                from cloud_service.vllm_backend import summarize_enhanced_analysis

                summary = summarize_enhanced_analysis(llm_input, self.settings)
            else:
                raise ValueError(f"unsupported cloud backend: {self.backend}")
        except Exception:
            self._store_failure(result.review_id)
            raise
        self._store(result.review_id, summary)
        return summary

    def get(self, review_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT summary_json FROM final_diagnosis_summary WHERE review_id=? AND status='succeeded'",
                (review_id,),
            ).fetchone()
        return json.loads(row["summary_json"]) if row else None

    def _store(self, review_id: str, summary: dict[str, Any]) -> None:
        now = time.time_ns()
        with connect(self.database_path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO final_diagnosis_summary("
                "review_id,status,backend,model_name,summary_json,error_code,created_at_ns,updated_at_ns"
                ") VALUES (?,?,?,?,?,?,COALESCE((SELECT created_at_ns FROM final_diagnosis_summary WHERE review_id=?),?),?)",
                (review_id, "succeeded", self.backend, summary["model_name"], json.dumps(summary, ensure_ascii=False), None, review_id, now, now),
            )

    def _store_failure(self, review_id: str) -> None:
        now = time.time_ns()
        with connect(self.database_path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO final_diagnosis_summary("
                "review_id,status,backend,model_name,summary_json,error_code,created_at_ns,updated_at_ns"
                ") VALUES (?,?,?,'unavailable',NULL,'SUMMARY_FAILED',COALESCE((SELECT created_at_ns FROM final_diagnosis_summary WHERE review_id=?),?),?)",
                (review_id, "failed", self.backend, review_id, now, now),
            )

    def _llm_input(self, result: EnhancedAnalysisResult) -> dict[str, Any]:
        payload = result.to_dict()
        payload["feature_context"] = self._feature_context(result.review_id)
        return payload

    def _feature_context(self, review_id: str) -> dict[str, Any]:
        with connect(self.database_path) as connection:
            review = connection.execute(
                "SELECT sender_id,cloud_recomputed_features_json,cloud_enhanced_features_json,"
                "advanced_features_json,context_features_json FROM cloud_review WHERE review_id=?",
                (review_id,),
            ).fetchone()
            aggregation = connection.execute(
                "SELECT ar.packet_manifest_json FROM enhanced_analysis_result er "
                "JOIN aggregation_result ar ON ar.aggregation_id=er.aggregation_id "
                "WHERE er.review_id=?",
                (review_id,),
            ).fetchone()
            manifest = json.loads(aggregation["packet_manifest_json"] or "[]") if aggregation else []
            packet_ids = [item["packet_id"] for item in manifest if item.get("packet_id")]
            summaries = []
            if review and packet_ids:
                placeholders = ",".join("?" for _ in packet_ids)
                rows = connection.execute(
                    "SELECT packet_id,summary_json FROM edge_packet_summary WHERE sender_id=? "
                    f"AND packet_id IN ({placeholders})",
                    (review["sender_id"], *packet_ids),
                ).fetchall()
                summaries = [json.loads(row["summary_json"]) for row in rows]
        if review is None:
            return {"cloud_review_features": {}, "context_edge_summaries": []}
        return {
            "cloud_review_features": {
                name: json.loads(review[name]) if review[name] else None
                for name in (
                    "cloud_recomputed_features_json",
                    "cloud_enhanced_features_json",
                    "advanced_features_json",
                    "context_features_json",
                )
            },
            "context_edge_summaries": summaries,
        }


def _mock_summary(
    result: EnhancedAnalysisResult, feature_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    model = result.model_evidence
    edge_inferences = [
        item.get("edge_inference") or {}
        for item in (feature_context or {}).get("context_edge_summaries", [])
    ]
    abnormal_edges = [
        item for item in edge_inferences if item.get("edge_result") == "abnormal"
    ]
    abnormal = model.get("label") == "abnormal" or bool(abnormal_edges)
    edge_confidence = max(
        (float(item.get("confidence") or 0.0) for item in abnormal_edges), default=0.0
    )
    confidence = max(float(model.get("probability") or 0.5), edge_confidence)
    label = "abnormal" if abnormal else "normal"
    high_risk = any(item.get("edge_risk_level") == "high" for item in abnormal_edges)
    if abnormal_edges:
        description = "边缘初判异常，结合预处理质量、增强信号证据和历史特征生成的最终诊断总结。"
    else:
        description = "基于预处理质量、增强信号证据和历史特征生成的最终诊断总结。"
    return {
        "review_id": result.review_id,
        "model_name": "cloud_bearing_mock",
        "label": label,
        "confidence": round(confidence, 4),
        "risk_level": "high" if high_risk or abnormal else "low",
        "action": "send_alert" if abnormal else "record_only",
        "description": description,
    }
