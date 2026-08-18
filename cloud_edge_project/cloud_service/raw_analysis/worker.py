from __future__ import annotations

import json

from .service import RawAnalysisSampleService, build_physical_evidence


class SignalAnalysisWorker:
    def __init__(self, service: RawAnalysisSampleService) -> None:
        self.service = service

    def run_once(self, *, now_ns: int) -> int:
        row = self.service.claim_pending()
        if row is None:
            return 0
        try:
            self.service.complete(
                row,
                build_physical_evidence(json.loads(row["metadata_json"]), self.service.payload_for(row)),
                now_ns=now_ns,
            )
        except Exception as error:
            self.service.fail(row, error, now_ns=now_ns)
        return 1
