"""Deterministic group-isolated DatasetManifest construction."""

from __future__ import annotations

import hashlib
import time
from collections import Counter, defaultdict
from typing import Any
from uuid import uuid4

from cloud_service.model_update.contracts import ModelUpdateConfig
from cloud_service.model_update.label_confirmation import LabelConfirmationProvider


FORBIDDEN_FEATURE_KEYS = {
    "cloud_recomputed_features",
    "cloud_enhanced_features",
    "advanced_features",
}


class DatasetBuilder:
    def __init__(self, config: ModelUpdateConfig):
        self.config = config

    def build(
        self,
        *,
        update: dict[str, Any],
        samples: list[dict[str, Any]],
        label_provider: LabelConfirmationProvider,
        feature_pipeline_version: str,
    ) -> dict[str, Any]:
        if not isinstance(feature_pipeline_version, str) or not feature_pipeline_version:
            raise ValueError("FEATURE_PIPELINE_VERSION_REQUIRED")
        eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
        seen: set[str] = set()
        for sample in samples:
            sample_id = sample.get("sample_id")
            if not isinstance(sample_id, str) or not sample_id or sample_id in seen:
                continue
            features = sample.get("features")
            if not isinstance(features, dict) or not features:
                continue
            if FORBIDDEN_FEATURE_KEYS & set(features):
                continue
            confirmation = label_provider.confirm(sample)
            if confirmation is None:
                continue
            _identifiers(sample)
            seen.add(sample_id)
            eligible.append((sample, confirmation))
        if not eligible:
            raise ValueError("SUPERVISED_SAMPLES_NOT_FOUND")
        feature_schemas = [
            _feature_schema(sample["features"]) for sample, _ in eligible
        ]
        input_feature_schema = feature_schemas[0]
        if any(schema != input_feature_schema for schema in feature_schemas[1:]):
            raise ValueError("INCONSISTENT_EDGE_FEATURE_SCHEMA")

        sample_group_keys = _isolation_group_keys(
            [sample for sample, _ in eligible]
        )
        groups: dict[str, list[str]] = defaultdict(list)
        for sample, _ in eligible:
            key = sample_group_keys[sample["sample_id"]]
            groups[key].append(sample["sample_id"])
        if len(groups) < 3:
            raise ValueError("INSUFFICIENT_ISOLATED_GROUPS")

        focus_ids = [
            sample["sample_id"]
            for sample, _ in eligible
            if sample.get("is_cloud_reviewed") is True
        ]
        if len(focus_ids) < self.config.min_focus_sample_count:
            raise ValueError("INSUFFICIENT_FOCUS_SAMPLES")
        focus_groups = {sample_group_keys[sample_id] for sample_id in focus_ids}
        ordered_groups = sorted(groups, key=_stable_group_order)
        train_groups, validation_groups, test_groups = self._partition_groups(ordered_groups)
        if not focus_groups.intersection(test_groups):
            focus_group = next(
                group_id
                for group_id in train_groups + validation_groups
                if group_id in focus_groups
            )
            replacement = test_groups[0]
            owner = train_groups if focus_group in train_groups else validation_groups
            owner[owner.index(focus_group)] = replacement
            test_groups[0] = focus_group
        partitions = {
            "train_sample_ids": _sample_ids(train_groups, groups),
            "validation_sample_ids": _sample_ids(validation_groups, groups),
            "test_sample_ids": _sample_ids(test_groups, groups),
        }
        if any(not values for values in partitions.values()):
            raise ValueError("EMPTY_DATASET_PARTITION")

        label_counts = Counter(
            confirmation["label_source"] for _, confirmation in eligible
        )
        labels = {}
        for sample, confirmation in eligible:
            frozen = {
                "confirmed_label": confirmation["confirmed_label"],
                "label_source": confirmation["label_source"],
            }
            if confirmation.get("confirmed_risk_level") in {
                "normal", "warning", "abnormal"
            }:
                frozen["confirmed_risk_level"] = confirmation[
                    "confirmed_risk_level"
                ]
            labels[sample["sample_id"]] = frozen
        return {
            "dataset_id": f"training_dataset_{uuid4().hex}",
            "update_id": update["update_id"],
            "baseline_version": update["baseline_version"],
            "feature_pipeline_version": feature_pipeline_version,
            "input_feature_schema": input_feature_schema,
            **partitions,
            "focus_sample_ids": focus_ids,
            "problem_type": update["problem_type"],
            "problem_context": update.get("problem_context", {}),
            "label_source_summary": dict(sorted(label_counts.items())),
            "sample_labels": labels,
            "sample_group_keys": sample_group_keys,
            "created_at_ns": time.time_ns(),
        }

    def _partition_groups(
        self, ordered_groups: list[str]
    ) -> tuple[list[str], list[str], list[str]]:
        count = len(ordered_groups)
        train_count = min(max(1, int(count * self.config.train_ratio)), count - 2)
        validation_count = min(
            max(1, int(count * self.config.validation_ratio)),
            count - train_count - 1,
        )
        return (
            ordered_groups[:train_count],
            ordered_groups[train_count : train_count + validation_count],
            ordered_groups[train_count + validation_count :],
        )


def _identifiers(sample: dict[str, Any]) -> list[str]:
    identifiers = []
    for key in ("source_file", "run_id", "task_id"):
        value = sample.get(key)
        if isinstance(value, str) and value:
            identifiers.append(f"{key}:{value}")
    if not identifiers:
        raise ValueError("SAMPLE_GROUP_REQUIRED")
    return identifiers


def _isolation_group_keys(samples: list[dict[str, Any]]) -> dict[str, str]:
    """Build connected groups so sharing any leakage identifier keeps samples together."""

    parents = list(range(len(samples)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    identifier_owner: dict[str, int] = {}
    identifiers_by_sample: list[list[str]] = []
    for index, sample in enumerate(samples):
        identifiers = _identifiers(sample)
        identifiers_by_sample.append(identifiers)
        for identifier in identifiers:
            owner = identifier_owner.setdefault(identifier, index)
            union(index, owner)

    component_identifiers: dict[int, set[str]] = defaultdict(set)
    for index, identifiers in enumerate(identifiers_by_sample):
        component_identifiers[find(index)].update(identifiers)
    component_keys = {
        root: "isolation:"
        + hashlib.sha256("|".join(sorted(identifiers)).encode("utf-8")).hexdigest()
        for root, identifiers in component_identifiers.items()
    }
    return {
        sample["sample_id"]: component_keys[find(index)]
        for index, sample in enumerate(samples)
    }


def _stable_group_order(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sample_ids(group_ids: list[str], groups: dict[str, list[str]]) -> list[str]:
    return [sample_id for group_id in group_ids for sample_id in sorted(groups[group_id])]


def _feature_schema(features: dict[str, Any], prefix: str = "") -> dict[str, str]:
    schema: dict[str, str] = {}
    for key in sorted(features):
        value = features[key]
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            schema.update(_feature_schema(value, path))
        elif isinstance(value, bool):
            schema[path] = "boolean"
        elif isinstance(value, (int, float)) or value is None:
            schema[path] = "number"
        elif isinstance(value, str):
            schema[path] = "string"
        else:
            raise ValueError("UNSUPPORTED_EDGE_FEATURE_TYPE")
    return schema
