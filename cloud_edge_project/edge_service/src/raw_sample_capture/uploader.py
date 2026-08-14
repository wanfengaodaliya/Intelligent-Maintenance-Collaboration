"""Non-blocking retrying uploader for raw waveform evidence."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable, Mapping

import requests

from .repository import RawSampleRepository


@dataclass(frozen=True)
class UploadBatchOutcome:
    acknowledged: int = 0
    conflicted: int = 0
    retried: int = 0


class RawAnalysisSampleUploader:
    def __init__(
        self,
        repository: RawSampleRepository,
        upload: Callable[[dict, bytes], Mapping[str, object]],
        *,
        batch_size: int = 1,
        max_backoff_seconds: int = 300,
    ) -> None:
        if batch_size <= 0 or max_backoff_seconds <= 0:
            raise ValueError("uploader limits must be positive")
        self.repository = repository
        self.upload = upload
        self.batch_size = batch_size
        self.max_backoff_seconds = max_backoff_seconds

    def run_once(self, now_ns: int) -> UploadBatchOutcome:
        acknowledged = conflicted = retried = 0
        for queued in self.repository.claim_due(now_ns=now_ns, limit=self.batch_size):
            try:
                response = self.upload(queued.sample.as_dict(), queued.sample.payload)
                status = response.get("status")
                if status in {"accepted", "duplicate"}:
                    self.repository.acknowledge(queued.sample.sample_id)
                    acknowledged += 1
                elif status == "conflict":
                    self.repository.mark_conflict(queued.sample.sample_id)
                    conflicted += 1
                else:
                    raise RuntimeError("invalid raw sample upload response")
            except Exception as error:
                self.repository.retry(
                    queued.sample.sample_id,
                    now_ns=now_ns,
                    error=f"{type(error).__name__}: {error}",
                    max_backoff_seconds=self.max_backoff_seconds,
                )
                retried += 1
        return UploadBatchOutcome(acknowledged, conflicted, retried)


class HttpRawSampleTransport:
    """The confirmed multipart transport: immutable metadata plus raw payload."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 2.0) -> None:
        self.url = base_url.rstrip("/") + "/cloud/raw-analysis-samples"
        self.timeout_seconds = timeout_seconds

    def upload(self, metadata: dict, payload: bytes) -> Mapping[str, object]:
        response = requests.post(
            self.url,
            data={"metadata": json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))},
            files={"payload": ("raw-sample.json", payload, "application/json")},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()
