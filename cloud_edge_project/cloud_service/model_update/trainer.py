"""Trainer boundary and per-model-type registry for offline retraining.

The bearing scenario retrains two model families offline: the edge distilled
H5 model and the cloud MOMENT light-adapt review model. Concrete trainers
implement one of the protocols below and are selected by model_type through
``resolve_trainer``. Training itself stays offline; the lifecycle only records
the plan and hands the dataset manifest to the trainer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from cloud_service.model_update.model_types import MODEL_TYPE_SPECS


class EdgeModelTrainer(Protocol):
    def train(
        self, dataset_manifest: dict[str, Any], output_dir: Path
    ) -> dict[str, Any]: ...


class CloudModelTrainer(Protocol):
    def train(
        self, dataset_manifest: dict[str, Any], output_dir: Path
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class TrainerSpec:
    trainer_id: str
    family: str
    output_subdir: str
    description: str


TRAINER_REGISTRY: dict[str, TrainerSpec] = {
    "distilled_h5": TrainerSpec(
        trainer_id="distilled_h5_kd_trainer",
        family="edge",
        output_subdir="distilled_h5",
        description="edge distilled H5 three-branch knowledge-distillation trainer",
    ),
    "moment_light_adapt": TrainerSpec(
        trainer_id="moment_light_adapt_trainer",
        family="cloud",
        output_subdir="moment_light_adapt",
        description="cloud MOMENT light-adapt review trainer",
    ),
}


def resolve_trainer(model_type: str) -> TrainerSpec:
    spec = TRAINER_REGISTRY.get(model_type)
    if spec is None:
        raise ValueError(f"UNSUPPORTED_MODEL_TYPE={model_type}")
    return spec


def build_training_plan(
    *,
    update_id: str,
    model_type: str,
    dataset_id: str,
    training_root: Path,
) -> dict[str, Any]:
    """Build the persisted plan that an offline trainer will consume.

    The output directory is deterministic per update so a retry of the same
    update reuses the same location.
    """

    spec = resolve_trainer(model_type)
    output_dir = training_root / spec.output_subdir / update_id
    return {
        "update_id": update_id,
        "model_type": model_type,
        "model_family": spec.family,
        "trainer_id": spec.trainer_id,
        "dataset_id": dataset_id,
        "output_dir": str(output_dir),
    }
