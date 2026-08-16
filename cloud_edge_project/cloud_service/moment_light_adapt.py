"""MOMENT LIGHT_ADAPT runner for V1.2 cloud bearing review."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from cloud_service.config import CloudSettings
from cloud_service.moment_backbone import load_moment_backbone


LABEL_NAMES = ("healthy", "outer_ring_damage", "inner_ring_damage")
MODEL_VERSION = "moment-light-adapt-fold3"


@dataclass(frozen=True)
class MomentPrediction:
    label: str
    confidence: float
    probabilities: dict[str, float]
    model_version: str


class MomentReviewPolicy:
    """Map model labels to the existing V1.2 bearing-result vocabulary."""

    _DECISIONS = {
        "healthy": ("normal", "low", "continue_operation"),
        "outer_ring_damage": ("warning", "medium", "scheduled_inspection"),
        "inner_ring_damage": ("warning", "medium", "scheduled_inspection"),
    }

    def decide(self, label: str) -> tuple[str, str, str]:
        return self._DECISIONS[label]


def build_condition_vector(operating_context: Mapping[str, Any]) -> np.ndarray:
    """Build the LIGHT_ADAPT 13D condition vector in its training order."""

    values: list[float] = []
    for field in (
        "shaft_speed_rpm",
        "load_torque_nm",
        "bearing_radial_load_n",
    ):
        statistics = operating_context[field]
        values.extend(
            float(statistics[name])
            for name in ("mean", "standard_deviation", "minimum", "maximum")
        )
    values.append(float(operating_context["bearing_module_temperature_c"]))
    return np.asarray(values, dtype=np.float32)


def deployment_workspace_root(pretrained_path: Path) -> Path:
    """Locate the deployment root that owns the ``experiments`` package."""

    for candidate in (pretrained_path, *pretrained_path.parents):
        if (candidate / "experiments").is_dir():
            return candidate
    return Path(__file__).resolve().parents[2]


class MomentLightAdaptRunner:
    """Load fold_3 once and infer directly from a raw 50 ms vibration window."""

    def __init__(self, settings: CloudSettings):
        self.settings = settings
        self._torch: Any | None = None
        self._device: Any | None = None
        self._model: Any | None = None
        self._condition_mean: np.ndarray | None = None
        self._condition_std: np.ndarray | None = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def model_version(self) -> str:
        return MODEL_VERSION

    def load(self) -> None:
        if self._model is not None:
            return

        import torch

        self._torch = torch
        self._device = self._resolve_device(torch)
        build_model = self._load_model_builder()
        model = build_model(
            str(self.settings.moment_pretrained_path),
            num_classes=3,
            condition_dropout=0.0,
        )
        checkpoint = torch.load(
            self.settings.moment_checkpoint_path,
            map_location=self._device,
        )
        model.load_state_dict(checkpoint["model_state_dict"])
        self._model = model.to(self._device).eval()
        condition_norm = json.loads(
            self.settings.moment_condition_norm_path.read_text(encoding="utf-8")
        )
        self._condition_mean = np.asarray(condition_norm["mean"], dtype=np.float32)
        self._condition_std = np.asarray(condition_norm["std"], dtype=np.float32)

    def predict(
        self,
        vibration: Mapping[str, Any],
        operating_context: Mapping[str, Any],
    ) -> MomentPrediction:
        if self._model is None or self._torch is None or self._device is None:
            raise RuntimeError("MOMENT LIGHT_ADAPT runner is not loaded")

        raw = np.asarray(vibration["values"], dtype=np.float32).reshape(1, -1)
        condition = build_condition_vector(operating_context).reshape(1, -1)
        normalized_condition = (condition - self._condition_mean) / self._condition_std
        raw_tensor = self._torch.from_numpy(raw).to(self._device)
        condition_tensor = self._torch.from_numpy(normalized_condition).to(self._device)
        with self._torch.no_grad():
            logits = self._model(raw_tensor, condition_tensor)
            probabilities = self._torch.softmax(logits, dim=1)[0].detach().cpu().numpy()
        index = int(probabilities.argmax())
        probability_map = {
            label: float(probabilities[position])
            for position, label in enumerate(LABEL_NAMES)
        }
        return MomentPrediction(
            label=LABEL_NAMES[index],
            confidence=float(probabilities[index]),
            probabilities=probability_map,
            model_version=MODEL_VERSION,
        )

    def _load_model_builder(self) -> Any:
        workspace_root = deployment_workspace_root(self.settings.moment_pretrained_path)
        for path in (workspace_root, self.settings.moment_deployment_dir):
            path_text = str(path)
            if path_text not in sys.path:
                sys.path.insert(0, path_text)
        self._install_training_adapter()
        module_path = self.settings.moment_deployment_dir / "moment_model.py"
        spec = importlib.util.spec_from_file_location(
            "_cloud_moment_light_adapt_model",
            module_path,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load MOMENT model definition: {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module.build_model

    @staticmethod
    def _install_training_adapter() -> None:
        """Provide the adapter import expected by the saved training model code."""

        module_name = "experiments.diagnosis_models.moment.adapter"
        if module_name not in sys.modules:
            adapter = types.ModuleType(module_name)
            adapter.load_moment_backbone = load_moment_backbone
            sys.modules[module_name] = adapter

    def _resolve_device(self, torch: Any) -> Any:
        if self.settings.moment_device == "auto":
            return torch.device("cuda" if torch.cuda.is_available() else "cpu")
        return torch.device(self.settings.moment_device)
