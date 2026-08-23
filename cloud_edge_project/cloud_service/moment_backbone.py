"""Compatibility loader for the MOMENT-1-small backbone used in training."""

from __future__ import annotations

import warnings
from typing import Any


def load_moment_backbone(
    pretrained_path: str,
    num_classes: int,
    *,
    pipeline_class: Any | None = None,
) -> Any:
    """Create the same initialized MOMENT classification pipeline as training."""

    if pipeline_class is None:
        from momentfm import MOMENTPipeline

        pipeline_class = MOMENTPipeline
    model = pipeline_class.from_pretrained(
        pretrained_path,
        model_kwargs={
            "task_name": "classification",
            "n_channels": 1,
            "num_class": num_classes,
        },
    )
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Only reconstruction head is pre-trained.*",
            category=UserWarning,
        )
        model.init()
    return model
