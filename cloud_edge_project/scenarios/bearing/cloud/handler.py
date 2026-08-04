from __future__ import annotations

from pathlib import Path
from typing import Any

from cloud_service.enhanced_analysis.service import EnhancedAnalysisService
from cloud_service.final_summary.service import FinalSummaryService
from cloud_service.model import infer_cloud


class BearingCloudHandler:
    scenario_type = "bearing"

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def infer(
        self,
        payload: dict[str, Any],
        *,
        context_transport: Any,
    ) -> dict[str, Any]:
        return infer_cloud(payload, context_transport=context_transport)

    def run_enhanced_analysis(self, review_id: str) -> None:
        result = EnhancedAnalysisService(self.database_path).analyze(review_id)
        FinalSummaryService(self.database_path).summarize(result)

    def get_final_summary(self, review_id: str) -> dict[str, Any] | None:
        return FinalSummaryService(self.database_path).get(review_id)
