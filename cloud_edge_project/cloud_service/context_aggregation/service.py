from __future__ import annotations

from pathlib import Path

from .assembler import assemble
from .contracts import AggregationError, LoadedPacket
from .loader import ContextWindowLoader, source_fingerprint
from .preprocessor import preprocess
from .repository import ContextAggregationRepository
from .window_store import WindowStore


class ContextAggregationService:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.loader = ContextWindowLoader(self.database_path)
        self.repository = ContextAggregationRepository(self.database_path)
        self.store = WindowStore(self.database_path)

    def aggregate(self, review_id: str, *, config_version: str = "cloud-preprocess-v1") -> dict:
        review, request, packets = self.loader.load(review_id)
        fingerprint = source_fingerprint(review_id, request["request_status"], packets)
        result = self.repository.create_or_get(review_id, fingerprint, config_version, request["request_status"])
        if result["aggregation_status"] != "queued" or not self.repository.acquire_lease(result["aggregation_id"]):
            return self._public(result)
        try:
            raw = assemble(packets)
            relative_dir = f"{review_id}/{fingerprint}/{config_version}"
            raw_path, raw_sha = self.store.write(f"{relative_dir}/raw.npz", raw)
            processed = preprocess(raw, config_version)
            processed_path, processed_sha = self.store.write(f"{relative_dir}/preprocessed.npz", processed)
            metadata = self._metadata(result["aggregation_id"], review_id, request["request_status"], config_version, packets, raw, raw_path, raw_sha, processed_path, processed_sha)
            return self._public(self.repository.mark_succeeded(result["aggregation_id"], metadata))
        except AggregationError as error:
            self.repository.mark_failed(result["aggregation_id"], error.code, error.detail, retryable=False)
            raise
        except Exception as error:
            self.repository.mark_failed(result["aggregation_id"], "AGGREGATION_INTERNAL_ERROR", "unexpected aggregation failure", retryable=True)
            raise error

    @staticmethod
    def _metadata(aggregation_id: str, review_id: str, context_status: str, config_version: str, packets: list[LoadedPacket], raw, raw_path: str, raw_sha: str, processed_path: str, processed_sha: str) -> dict:
        manifest = [{"device_id": item.packet["device_id"], "task_id": item.packet["task_id"], "bearing_id": item.packet["bearing_id"], "sender_id": item.packet["sender_id"], "relative_position": item.relative_position, "packet_id": item.packet["packet_id"], "sequence_number": item.packet["sequence_number"], "end_generate_timestamp_ns": item.packet["end_generate_timestamp_ns"], "payload_sha256": item.index["payload_sha256"], "storage_path": item.index["storage_path"]} for item in packets]
        boundaries = [{"relative_position": int(position), "start_index": int(start), "end_index_exclusive": int(start + 3_200)} for position, start in zip(raw.relative_positions, raw.packet_start_samples)]
        return {
            "relative_positions": raw.relative_positions.tolist(), "manifest": manifest, "boundaries": boundaries,
            "raw_path": raw_path, "raw_sha": raw_sha, "processed_path": processed_path, "processed_sha": processed_sha,
            "sample_counts": {name: int(len(values)) for name, values in raw.channels.items()},
            "event": {"event_type": "preprocessed_window_ready", "aggregation_id": aggregation_id, "review_id": review_id, "context_status": context_status, "preprocessing_config_version": config_version, "preprocessed_window_path": processed_path, "packet_count": len(packets), "sample_count_per_channel": int(len(raw.channels["vibration"]))},
        }

    @staticmethod
    def _public(result: dict) -> dict:
        result = dict(result)
        if result.get("relative_positions_json"):
            import json
            result["relative_positions"] = json.loads(result["relative_positions_json"])
        return result
