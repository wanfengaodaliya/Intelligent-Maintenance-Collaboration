from __future__ import annotations

from pathlib import Path

from .contracts import AggregationError
from .repository import ContextAggregationRepository
from .service import ContextAggregationService


class ContextAggregationCoordinator:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def aggregate_eligible(self, *, config_version: str = "cloud-preprocess-v1", limit: int = 20) -> int:
        completed = 0
        repository = ContextAggregationRepository(self.database_path)
        repository.recover_expired_leases()
        service = ContextAggregationService(self.database_path)
        for review_id in repository.eligible_review_ids(limit):
            try:
                if service.aggregate(review_id, config_version=config_version)["aggregation_status"] == "succeeded":
                    completed += 1
            except Exception:
                pass
        return completed
