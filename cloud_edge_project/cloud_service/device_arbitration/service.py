from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from cloud_service.device_arbitration.fusion import calculate_fusion
from cloud_service.device_arbitration.repository import DeviceArbitrationRepository
from cloud_service.storage.database import initialize_database
from core.arbitration_contracts import ScenarioArbitrationAdapter
from core.arbitration_engine import ArbitrationEngine


class DeviceArbitrationService:
    def __init__(self, database_path: Path, adapter: ScenarioArbitrationAdapter):
        self.adapter = adapter
        self.database_path = Path(database_path)
        initialize_database(self.database_path)
        self.repository = DeviceArbitrationRepository(self.database_path)

    def arbitrate(self, request: dict[str, Any]) -> dict[str, Any]:
        conflict_id = request.get("conflict_id") if isinstance(request, dict) else None
        if isinstance(conflict_id, str) and conflict_id.strip():
            existing = self.repository.get_by_conflict_id(conflict_id.strip())
            if existing is not None:
                return existing

        context = self.adapter.build_context(request)
        decision = ArbitrationEngine(self.adapter, calculate_fusion).decide(context)

        final_action = decision["final_action"]
        final_state = (
            self.adapter.action_to_state(final_action)
            if isinstance(final_action, str)
            else "unknown"
        )
        result: dict[str, Any] = {
            "arbitration_id": f"arbitration_{uuid.uuid4().hex}",
            "scenario_type": context.scenario_type,
            "conflict_id": context.conflict_id,
            "subject_id": context.subject_id,
            "task_id": context.task_id,
            "status": decision["status"],
            "final_state": final_state,
            "final_action": final_action,
            "confidence": decision["confidence"],
            "resolution_method": decision["resolution_method"],
            "dominant_unit_id": decision["dominant_unit_id"],
            "action_scores": decision["action_scores"],
            "scenario_result": self.adapter.build_scenario_result(
                context=context,
                dominant_unit_id=decision["dominant_unit_id"],
                triggered_rule_id=decision["triggered_rule_id"],
                reason=decision["reason"],
            ),
            "created_at_ns": time.time_ns(),
        }
        if decision["resolution_method"] == "weighted_fusion":
            result["decision_margin"] = decision["decision_margin"]
        persisted = self.repository.save(request=request, result=result)
        return persisted

    def get(self, conflict_id: str) -> dict[str, Any] | None:
        return self.repository.get_by_conflict_id(conflict_id)
