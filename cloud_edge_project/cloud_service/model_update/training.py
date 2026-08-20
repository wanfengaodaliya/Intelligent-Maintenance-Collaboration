"""Real offline trainers for the edge H5 and cloud MOMENT model families.

The bearing scenario retrains two model families offline from supervised
samples accumulated at runtime:

- ``distilled_h5``: the edge three-branch H5 model (CNN + physical features +
  operating condition), fine-tuned from the active baseline checkpoint.
- ``moment_light_adapt``: the cloud MOMENT light-adapt review model, fine-tuned
  from the deployed baseline checkpoint.

Both trainers consume a frozen ``DatasetManifest`` (train / validation / test
sample ids plus frozen labels) and write a version directory whose contents
satisfy ``CandidateRegistry`` and the edge pull bundle contract
(``best_model.pt`` + ``checkpoint_sha256.txt`` + normalization JSONs +
``manifest.json``).

The module is self-contained on purpose: the cloud trainer must run without
importing the ``experiments`` package, mirroring how the edge runtime already
keeps its own copy of the H5 architecture and feature transform.
"""

from __future__ import annotations

import csv
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np
from scipy.io import loadmat
from scipy.signal import resample_poly

from cloud_service.model_update.dataset_repository import PacketSourceRepository

LABEL_NAMES = ("healthy", "outer_ring_damage", "inner_ring_damage")
LABEL_TO_ID = {
    "healthy": 0,
    "normal": 0,
    "outer_ring_damage": 1,
    "inner_ring_damage": 2,
}

SAMPLES_PER_WINDOW = 3200
STUDENT_SAMPLES = 800
PHYSICAL_DIM = 19
CONDITION_DIM = 13

DEFAULT_EDGE_CONFIG = {
    "seed": 42,
    "max_epochs": 15,
    "patience": 5,
    "batch_size": 256,
    "learning_rate": 0.0001,
    "weight_decay": 0.0001,
}

DEFAULT_CLOUD_CONFIG = {
    "seed": 42,
    "max_epochs": 5,
    "patience": 3,
    "batch_size": 32,
    "learning_rate": 0.0005,
    "weight_decay": 0.0001,
}


# ============================================================
# H5 three-branch architecture (frozen copy of the edge runtime)
# ============================================================

_physical_fusion_model_class: type | None = None


def _build_physical_fusion_model() -> type:
    """Build the nn.Module subclass on first use (torch may be absent at import)."""

    import torch.nn as nn

    class PhysicalFusionModel(nn.Module):
        """CNN, physical-feature, and operating-condition fusion network.

        Mirrors ``edge_service/src/edge_diagnosis/h5_network.py`` so the
        trained checkpoint is loadable by the deployed edge runtime.
        """

        def __init__(
            self,
            num_classes: int = 3,
            phys_dim: int = 19,
            cond_dim: int = 13,
            cond_scale: float = 0.25,
            cond_dropout: float = 0.5,
            use_h4_cnn: bool = False,
        ) -> None:
            super().__init__()
            self.cond_scale = cond_scale
            self.cond_dropout = cond_dropout
            self.use_h4_cnn = use_h4_cnn
            if use_h4_cnn:
                self.cnn = nn.Sequential(
                    nn.Conv1d(1, 16, kernel_size=15, stride=4, padding=7),
                    nn.BatchNorm1d(16), nn.GELU(),
                    nn.Conv1d(16, 32, kernel_size=7, stride=4, padding=3),
                    nn.BatchNorm1d(32), nn.GELU(),
                    nn.Conv1d(32, 64, kernel_size=5, stride=2, padding=2),
                    nn.BatchNorm1d(64), nn.GELU(), nn.AdaptiveAvgPool1d(1),
                )
                self.vibration_dim = 64
            else:
                self.cnn = nn.Sequential(
                    nn.Conv1d(1, 32, kernel_size=3, padding=1),
                    nn.BatchNorm1d(32), nn.ReLU(), nn.MaxPool1d(2, 2),
                    nn.Conv1d(32, 64, kernel_size=3, padding=1),
                    nn.BatchNorm1d(64), nn.ReLU(), nn.MaxPool1d(2, 2),
                    nn.Conv1d(64, 128, kernel_size=3, padding=1),
                    nn.BatchNorm1d(128), nn.ReLU(), nn.AdaptiveAvgPool1d(1),
                )
                self.vibration_dim = 128
            self.phys_encoder = nn.Sequential(
                nn.Linear(phys_dim, 32), nn.LayerNorm(32), nn.GELU(), nn.Dropout(0.1)
            )
            self.phys_dim = 32
            self.condition_encoder = nn.Sequential(
                nn.Linear(cond_dim, 16), nn.LayerNorm(16), nn.GELU()
            )
            self.condition_dim = 16
            self.classifier = nn.Sequential(
                nn.Linear(self.vibration_dim + self.phys_dim + self.condition_dim, 64),
                nn.GELU(), nn.Dropout(0.2), nn.Linear(64, num_classes),
            )

        def forward(self, vibration, physical_features, condition):
            import torch

            vib_feat = self.cnn(vibration.unsqueeze(1)).squeeze(-1)
            phys_feat = self.phys_encoder(physical_features)
            cond_feat = self.condition_encoder(condition) * self.cond_scale
            if self.training and self.cond_dropout > 0:
                mask = torch.rand(cond_feat.size(0), 1, device=cond_feat.device) > self.cond_dropout
                cond_feat = cond_feat * mask
            fused = torch.cat([vib_feat, phys_feat, cond_feat], dim=1)
            return self.classifier(fused), fused

    return PhysicalFusionModel


def PhysicalFusionModel(*args: Any, **kwargs: Any) -> Any:
    """Factory for the lazily built H5 fusion network."""
    global _physical_fusion_model_class
    if _physical_fusion_model_class is None:
        _physical_fusion_model_class = _build_physical_fusion_model()
    return _physical_fusion_model_class(*args, **kwargs)


# ============================================================
# Feature transforms (frozen copies of the edge runtime)
# ============================================================

def compute_physical_features(x: np.ndarray, sample_rate: int = 64_000) -> np.ndarray:
    """Compute the training-time 19D physical feature vector for one window."""
    from scipy import stats as scipy_stats
    from scipy.signal import hilbert
    from scipy.signal.windows import hann

    eps = 1e-10
    x = np.asarray(x, dtype=np.float64)
    rms = np.sqrt(np.mean(x ** 2))
    std = np.std(x)
    max_abs = float(np.max(np.abs(x)))
    mean_abs = float(np.mean(np.abs(x)))
    centered = x - np.mean(x)
    spectrum = np.abs(np.fft.rfft(centered * hann(len(centered))))
    frequencies = np.fft.rfftfreq(len(centered), d=1.0 / sample_rate)
    power = spectrum ** 2
    total_power = np.sum(power)
    if total_power > eps:
        centroid = float(np.sum(frequencies * power) / total_power)
        spread = float(np.sqrt(np.sum(((frequencies - centroid) ** 2) * power) / total_power))
    else:
        centroid = spread = 0.0
    normalized_power = power / (total_power + eps)
    low = (frequencies >= 0) & (frequencies < 4_000)
    middle = (frequencies >= 4_000) & (frequencies < 12_000)
    high = (frequencies >= 12_000) & (frequencies <= 32_000)
    envelope = np.abs(hilbert(centered))
    envelope_spectrum = np.abs(np.fft.rfft(envelope - np.mean(envelope)))
    envelope_frequencies = np.fft.rfftfreq(len(envelope), d=1.0 / sample_rate)
    features = np.array(
        [
            rms, std, float(np.ptp(x)), float(scipy_stats.kurtosis(x, fisher=True)),
            float(scipy_stats.skew(x)), max_abs / (rms + eps),
            max_abs / (mean_abs + eps), rms / (mean_abs + eps), centroid, spread,
            float(-np.sum(normalized_power * np.log(normalized_power + eps))),
            float(frequencies[int(np.argmax(spectrum))]),
            float(np.sum(power[low]) / (total_power + eps)),
            float(np.sum(power[middle]) / (total_power + eps)),
            float(np.sum(power[high]) / (total_power + eps)),
            float(np.sqrt(np.mean(envelope ** 2))),
            float(scipy_stats.kurtosis(envelope, fisher=True)), float(np.max(envelope)),
            float(envelope_frequencies[int(np.argmax(envelope_spectrum))]),
        ],
        dtype=np.float32,
    )
    return np.nan_to_num(features, nan=0.0, posinf=0.0, neginf=0.0)


def _load_vibration(mat_path: Path) -> np.ndarray:
    mat = loadmat(str(mat_path), simplify_cells=True)
    data = mat.get(Path(mat_path).stem)
    if data is None or "Y" not in data:
        raise ValueError("MAT missing Y channels")
    channels = data["Y"]
    if isinstance(channels, dict):
        channels = [channels]
    for channel in channels:
        if str(channel.get("Name")) == "vibration_1":
            return np.asarray(channel.get("Data"), dtype=np.float32).reshape(-1)
    raise ValueError("MAT missing vibration_1")


def _load_condition_channels(mat_path: Path) -> dict[str, np.ndarray]:
    mat = loadmat(str(mat_path), simplify_cells=True)
    data = mat.get(Path(mat_path).stem)
    if data is None:
        raise ValueError("cannot read MAT")
    channels = data["Y"]
    if isinstance(channels, dict):
        channels = [channels]
    out: dict[str, np.ndarray] = {}
    for channel in channels:
        name = str(channel.get("Name"))
        if name in ("speed", "torque", "force", "temp_2_bearing_module"):
            out[name] = np.asarray(channel.get("Data"), dtype=np.float64).reshape(-1)
    missing = {"speed", "torque", "force", "temp_2_bearing_module"} - set(out)
    if missing:
        raise ValueError(f"MAT missing condition channels: {sorted(missing)}")
    return out


def _window_condition(
    cond_channels: Mapping[str, np.ndarray], vib_len: int, window_index: int
) -> np.ndarray:
    values: list[float] = []
    for name in ("speed", "torque", "force"):
        arr = cond_channels[name]
        ratio = arr.size / vib_len
        start = int(np.round(window_index * SAMPLES_PER_WINDOW * ratio))
        end = int(np.round((window_index + 1) * SAMPLES_PER_WINDOW * ratio))
        segment = arr[start:end]
        if segment.size == 0:
            values.extend([0.0, 0.0, 0.0, 0.0])
        else:
            values.extend(
                [
                    float(segment.mean()),
                    float(segment.std()),
                    float(segment.min()),
                    float(segment.max()),
                ]
            )
    values.append(float(cond_channels["temp_2_bearing_module"].mean()))
    return np.asarray(values, dtype=np.float32)


# ============================================================
# Raw sample loading
# ============================================================

class RawTrainingSampleLoader:
    """Load raw vibration windows and condition vectors for manifest samples.

    Samples are resolved through ``PacketSourceRepository`` to their Paderborn
    source file; samples without a resolvable source file are skipped so the
    trainer only consumes windows it can actually reconstruct.
    """

    def __init__(
        self, source_repository: PacketSourceRepository, data_root: Path | str
    ) -> None:
        self.source_repository = source_repository
        self.data_root = Path(data_root)

    def load(
        self, sample_ids: list[str], labels: Mapping[str, int]
    ) -> dict[str, dict[str, Any]]:
        loaded: dict[str, dict[str, Any]] = {}
        for sample_id in sample_ids:
            label = labels.get(sample_id)
            if label is None:
                continue
            window = self._load_window(sample_id)
            if window is None:
                continue
            loaded[sample_id] = {
                "vibration_64k": window["vibration_64k"],
                "vibration_16k": resample_poly(
                    window["vibration_64k"], 1, 4
                ).astype(np.float32),
                "physical": compute_physical_features(window["vibration_64k"]),
                "condition": window["condition_13d"],
                "label": int(label),
            }
        return loaded

    def _load_window(self, sample_id: str) -> dict[str, np.ndarray] | None:
        source = self.source_repository.get_by_packet_id(sample_id)
        if source is None:
            return None
        mat_path = (
            self.data_root / str(source["source_bearing_code"]) / source["source_file"]
        )
        if not mat_path.is_file():
            return None
        try:
            vibration = _load_vibration(mat_path)
            cond_channels = _load_condition_channels(mat_path)
        except (ValueError, KeyError, TypeError):
            return None
        window_index = int(source["window_index"])
        start = window_index * SAMPLES_PER_WINDOW
        if start + SAMPLES_PER_WINDOW > vibration.size:
            return None
        return {
            "vibration_64k": vibration[start : start + SAMPLES_PER_WINDOW].astype(
                np.float32
            ),
            "condition_13d": _window_condition(
                cond_channels, vibration.size, window_index
            ),
        }


# ============================================================
# Shared training helpers
# ============================================================

def _class_labels(dataset_manifest: dict[str, Any]) -> dict[str, int]:
    labels: dict[str, int] = {}
    for sample_id, info in (dataset_manifest.get("sample_labels") or {}).items():
        if not isinstance(info, dict):
            continue
        label = LABEL_TO_ID.get(info.get("confirmed_label"))
        if label is not None:
            labels[sample_id] = label
    return labels


def _stack_arrays(raw: Mapping[str, dict[str, Any]]) -> dict[str, np.ndarray]:
    sample_ids = sorted(raw)
    return {
        "vibration_64k": np.stack([raw[sid]["vibration_64k"] for sid in sample_ids]),
        "vibration_16k": np.stack([raw[sid]["vibration_16k"] for sid in sample_ids]),
        "physical": np.stack([raw[sid]["physical"] for sid in sample_ids]),
        "condition": np.stack([raw[sid]["condition"] for sid in sample_ids]),
        "labels": np.asarray([raw[sid]["label"] for sid in sample_ids], dtype=np.int64),
        "sample_ids": sample_ids,
    }


def _normalize_arrays(arrays: Mapping[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    physical = arrays["physical"]
    condition = arrays["condition"]
    phys_mean = physical.mean(axis=0).astype(np.float32)
    phys_std = physical.std(axis=0).astype(np.float32)
    phys_std = np.where(phys_std < 1e-8, 1.0, phys_std)
    cond_mean = condition.mean(axis=0).astype(np.float32)
    cond_std = condition.std(axis=0).astype(np.float32)
    cond_std = np.where(cond_std < 1e-8, 1.0, cond_std)
    return phys_mean, phys_std, cond_mean, cond_std


def _load_norm(path: Path | None) -> tuple[np.ndarray, np.ndarray] | None:
    if path is None or not Path(path).is_file():
        return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        mean = np.asarray(value["mean"], dtype=np.float32)
        std = np.asarray(value["std"], dtype=np.float32)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if mean.shape != std.shape or mean.size == 0 or np.any(std <= 0):
        return None
    return mean, std


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _bundle_entries(output_dir: Path) -> list[dict[str, str]]:
    return [
        {"rel_path": path.name, "sha256": _sha256(path)}
        for path in sorted(output_dir.iterdir())
        if path.is_file()
    ]


def _candidate_version(model_type: str, update_id: str) -> str:
    return f"{model_type}_kd_{update_id[-8:]}"


# ============================================================
# Edge H5 trainer
# ============================================================

class EdgeH5Trainer:
    """Fine-tune the edge three-branch H5 model from its baseline checkpoint."""

    def __init__(
        self,
        *,
        loader: RawTrainingSampleLoader,
        baseline_checkpoint: Path | str,
        baseline_dir: Path | str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.loader = loader
        self.baseline_checkpoint = Path(baseline_checkpoint)
        self.baseline_dir = Path(baseline_dir) if baseline_dir else None
        self.config = {**DEFAULT_EDGE_CONFIG, **(config or {})}

    def train(
        self, dataset_manifest: dict[str, Any], output_dir: Path
    ) -> dict[str, Any]:
        import torch
        import torch.nn as nn
        import torch.nn.functional as F
        from sklearn.metrics import f1_score
        from torch.utils.data import DataLoader, TensorDataset

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        labels = _class_labels(dataset_manifest)
        train_raw = self.loader.load(dataset_manifest["train_sample_ids"], labels)
        val_raw = self.loader.load(dataset_manifest["validation_sample_ids"], labels)
        if not train_raw:
            raise ValueError("TRAINING_RAW_SAMPLES_NOT_FOUND")
        if not val_raw:
            raise ValueError("VALIDATION_RAW_SAMPLES_NOT_FOUND")
        train_arrays = _stack_arrays(train_raw)
        val_arrays = _stack_arrays(val_raw)

        phys_mean, phys_std, cond_mean, cond_std = _normalize_arrays(train_arrays)
        baseline_phys = _load_norm(
            self.baseline_dir / "physical_feature_normalization.json"
            if self.baseline_dir
            else None
        )
        if baseline_phys is not None and baseline_phys[0].size == PHYSICAL_DIM:
            phys_mean, phys_std = baseline_phys
        baseline_cond = _load_norm(
            self.baseline_dir / "condition_norm.json" if self.baseline_dir else None
        )
        if baseline_cond is not None and baseline_cond[0].size == CONDITION_DIM:
            cond_mean, cond_std = baseline_cond

        train_arrays["physical"] = _normalize_features(
            train_arrays["physical"], phys_mean, phys_std
        )
        train_arrays["condition"] = _normalize_features(
            train_arrays["condition"], cond_mean, cond_std
        )
        val_arrays["physical"] = _normalize_features(
            val_arrays["physical"], phys_mean, phys_std
        )
        val_arrays["condition"] = _normalize_features(
            val_arrays["condition"], cond_mean, cond_std
        )

        model = PhysicalFusionModel(
            num_classes=3,
            phys_dim=PHYSICAL_DIM,
            cond_dim=CONDITION_DIM,
            cond_scale=0.25,
            cond_dropout=0.5,
            use_h4_cnn=False,
        )
        _load_h5_checkpoint(model, self.baseline_checkpoint)

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model.to(device)
        train_loader = DataLoader(
            TensorDataset(
                torch.from_numpy(train_arrays["vibration_16k"]),
                torch.from_numpy(train_arrays["physical"]),
                torch.from_numpy(train_arrays["condition"]),
                torch.from_numpy(train_arrays["labels"]),
            ),
            batch_size=self.config["batch_size"],
            shuffle=True,
        )
        val_loader = DataLoader(
            TensorDataset(
                torch.from_numpy(val_arrays["vibration_16k"]),
                torch.from_numpy(val_arrays["physical"]),
                torch.from_numpy(val_arrays["condition"]),
                torch.from_numpy(val_arrays["labels"]),
            ),
            batch_size=512,
            shuffle=False,
        )
        optimizer = torch.optim.AdamW(
            model.parameters(),
            lr=self.config["learning_rate"],
            weight_decay=self.config["weight_decay"],
        )

        best_f1 = 0.0
        best_state = None
        best_epoch = 0
        no_improve = 0
        history: list[dict[str, Any]] = []
        for epoch in range(1, self.config["max_epochs"] + 1):
            model.train()
            total_loss = 0.0
            total = 0
            for vibration, physical, condition, target in train_loader:
                vibration = vibration.to(device)
                physical = physical.to(device)
                condition = condition.to(device)
                target = target.to(device)
                optimizer.zero_grad()
                logits, _ = model(vibration, physical, condition)
                loss = F.cross_entropy(logits, target)
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                total_loss += float(loss.item()) * target.size(0)
                total += target.size(0)
            val_metrics = _evaluate_h5(model, val_loader, device, f1_score)
            val_f1 = val_metrics["macro_f1"]
            history.append(
                {
                    "epoch": epoch,
                    "train_loss": round(total_loss / total, 6) if total else 0.0,
                    "val_macro_f1": round(val_f1, 6),
                    "val_accuracy": round(val_metrics["accuracy"], 6),
                }
            )
            if val_f1 > best_f1:
                best_f1 = val_f1
                best_epoch = epoch
                best_state = {
                    key: value.cpu().clone() for key, value in model.state_dict().items()
                }
                no_improve = 0
            else:
                no_improve += 1
            if no_improve >= self.config["patience"]:
                break
        if best_state is not None:
            model.load_state_dict(best_state)
        model.to("cpu")

        return _write_edge_bundle(
            model=model,
            output_dir=output_dir,
            manifest=dataset_manifest,
            candidate_version=_candidate_version(
                "distilled_h5", dataset_manifest["update_id"]
            ),
            best_epoch=best_epoch,
            best_f1=best_f1,
            history=history,
            phys_mean=phys_mean,
            phys_std=phys_std,
            cond_mean=cond_mean,
            cond_std=cond_std,
            config=self.config,
            train_sample_count=len(train_raw),
            val_sample_count=len(val_raw),
            baseline_checkpoint=self.baseline_checkpoint,
        )


def _load_h5_checkpoint(model: Any, checkpoint_path: Path) -> None:
    import torch

    checkpoint = torch.load(
        str(checkpoint_path), map_location="cpu", weights_only=False
    )
    state = checkpoint["model_state_dict"]
    h5_state = {
        key[3:]: value for key, value in state.items() if key.startswith("h5.")
    }
    if not h5_state:
        h5_state = {
            key: value
            for key, value in state.items()
            if not key.startswith("feature_projection.")
        }
    model.load_state_dict(h5_state)


def _normalize_features(
    features: np.ndarray, mean: np.ndarray, std: np.ndarray
) -> np.ndarray:
    return ((features - mean) / std).astype(np.float32)


def _evaluate_h5(model: Any, loader: Any, device: Any, f1_score: Callable) -> dict[str, Any]:
    import torch
    import torch.nn.functional as F

    model.eval()
    all_preds: list[np.ndarray] = []
    all_labels: list[np.ndarray] = []
    with torch.no_grad():
        for vibration, physical, condition, target in loader:
            vibration = vibration.to(device)
            physical = physical.to(device)
            condition = condition.to(device)
            logits, _ = model(vibration, physical, condition)
            preds = F.softmax(logits, dim=1).argmax(dim=1).cpu().numpy()
            all_preds.append(preds)
            all_labels.append(target.numpy())
    preds = np.concatenate(all_preds)
    labels = np.concatenate(all_labels)
    return {
        "macro_f1": float(
            f1_score(labels, preds, average="macro", zero_division=0)
        ),
        "accuracy": float(np.mean(preds == labels)),
    }


def _write_edge_bundle(
    *,
    model: Any,
    output_dir: Path,
    manifest: dict[str, Any],
    candidate_version: str,
    best_epoch: int,
    best_f1: float,
    history: list[dict[str, Any]],
    phys_mean: np.ndarray,
    phys_std: np.ndarray,
    cond_mean: np.ndarray,
    cond_std: np.ndarray,
    config: dict[str, Any],
    train_sample_count: int,
    val_sample_count: int,
    baseline_checkpoint: Path,
) -> dict[str, Any]:
    import torch

    checkpoint_path = output_dir / "best_model.pt"
    state = {f"h5.{key}": value for key, value in model.state_dict().items()}
    torch.save(
        {
            "model_state_dict": state,
            "epoch": best_epoch,
            "best_val_macro_f1": best_f1,
            "architecture": "PhysicalFusionModel",
            "config": config,
        },
        str(checkpoint_path),
    )
    (output_dir / "checkpoint_sha256.txt").write_text(
        _sha256(checkpoint_path) + "\n", encoding="utf-8"
    )
    _write_json(
        output_dir / "physical_feature_normalization.json",
        {"mean": phys_mean.tolist(), "std": phys_std.tolist()},
    )
    _write_json(
        output_dir / "condition_norm.json",
        {"mean": cond_mean.tolist(), "std": cond_std.tolist()},
    )
    _write_json(
        output_dir / "manifest.json",
        {
            "version": candidate_version,
            "model_type": "distilled_h5",
            "model_family": "edge",
            "feature_pipeline_version": manifest["feature_pipeline_version"],
            "training_dataset_id": manifest["dataset_id"],
        },
    )
    _write_csv(
        output_dir / "training_history.csv",
        ["epoch", "train_loss", "val_macro_f1", "val_accuracy"],
        [
            [entry["epoch"], entry["train_loss"], entry["val_macro_f1"], entry["val_accuracy"]]
            for entry in history
        ],
    )
    _write_json(
        output_dir / "model_metadata.json",
        {
            "model_name": "Distilled_H5_KD",
            "architecture": "PhysicalFusionModel",
            "best_epoch": best_epoch,
            "best_val_macro_f1": best_f1,
            "train_sample_count": train_sample_count,
            "val_sample_count": val_sample_count,
            "training_config": config,
            "baseline_checkpoint": str(baseline_checkpoint),
        },
    )
    return {
        "candidate_version": candidate_version,
        "artifact_path": str(output_dir),
        "artifact_sha256": _sha256(checkpoint_path),
        "artifact_bundle": _bundle_entries(output_dir),
        "model_type": "distilled_h5",
        "feature_pipeline_version": manifest["feature_pipeline_version"],
        "input_feature_schema": manifest["input_feature_schema"],
        "training_dataset_id": manifest["dataset_id"],
        "training_config": config,
        "training_metrics": {
            "best_val_macro_f1": best_f1,
            "best_epoch": best_epoch,
            "epochs": len(history),
            "history": history,
            "train_sample_count": train_sample_count,
            "val_sample_count": val_sample_count,
        },
    }


# ============================================================
# Cloud MOMENT light-adapt trainer
# ============================================================

class CloudMomentTrainer:
    """Fine-tune the cloud MOMENT light-adapt review model from its baseline."""

    def __init__(
        self,
        *,
        loader: RawTrainingSampleLoader,
        baseline_checkpoint: Path | str,
        pretrained_path: Path | str,
        model_module_path: Path | str,
        condition_norm_path: Path | str | None = None,
        model_factory: Callable[..., Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.loader = loader
        self.baseline_checkpoint = Path(baseline_checkpoint)
        self.pretrained_path = Path(pretrained_path)
        self.model_module_path = Path(model_module_path)
        self.condition_norm_path = (
            Path(condition_norm_path) if condition_norm_path else None
        )
        self.model_factory = model_factory
        self.config = {**DEFAULT_CLOUD_CONFIG, **(config or {})}

    def train(
        self, dataset_manifest: dict[str, Any], output_dir: Path
    ) -> dict[str, Any]:
        import torch
        import torch.nn as nn
        import torch.nn.functional as F
        from sklearn.metrics import f1_score
        from torch.utils.data import DataLoader, TensorDataset

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        labels = _class_labels(dataset_manifest)
        train_raw = self.loader.load(dataset_manifest["train_sample_ids"], labels)
        val_raw = self.loader.load(dataset_manifest["validation_sample_ids"], labels)
        if not train_raw:
            raise ValueError("TRAINING_RAW_SAMPLES_NOT_FOUND")
        if not val_raw:
            raise ValueError("VALIDATION_RAW_SAMPLES_NOT_FOUND")
        train_arrays = _stack_arrays(train_raw)
        val_arrays = _stack_arrays(val_raw)

        cond_mean, cond_std = self._condition_normalization(train_arrays)
        train_arrays["condition"] = _normalize_features(
            train_arrays["condition"], cond_mean, cond_std
        )
        val_arrays["condition"] = _normalize_features(
            val_arrays["condition"], cond_mean, cond_std
        )

        model = self._build_model()
        checkpoint = torch.load(
            str(self.baseline_checkpoint), map_location="cpu", weights_only=False
        )
        model.load_state_dict(checkpoint["model_state_dict"])

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model.to(device)
        train_loader = DataLoader(
            TensorDataset(
                torch.from_numpy(train_arrays["vibration_64k"]),
                torch.from_numpy(train_arrays["condition"]),
                torch.from_numpy(train_arrays["labels"]),
            ),
            batch_size=self.config["batch_size"],
            shuffle=True,
        )
        val_loader = DataLoader(
            TensorDataset(
                torch.from_numpy(val_arrays["vibration_64k"]),
                torch.from_numpy(val_arrays["condition"]),
                torch.from_numpy(val_arrays["labels"]),
            ),
            batch_size=32,
            shuffle=False,
        )
        optimizer = torch.optim.AdamW(
            model.parameters(),
            lr=self.config["learning_rate"],
            weight_decay=self.config["weight_decay"],
        )

        best_f1 = 0.0
        best_state = None
        best_epoch = 0
        no_improve = 0
        history: list[dict[str, Any]] = []
        for epoch in range(1, self.config["max_epochs"] + 1):
            model.train()
            total_loss = 0.0
            total = 0
            for vibration, condition, target in train_loader:
                vibration = vibration.to(device)
                condition = condition.to(device)
                target = target.to(device)
                optimizer.zero_grad()
                logits = model(vibration, condition)
                loss = F.cross_entropy(logits, target)
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                total_loss += float(loss.item()) * target.size(0)
                total += target.size(0)
            val_metrics = _evaluate_moment(model, val_loader, device, f1_score)
            val_f1 = val_metrics["macro_f1"]
            history.append(
                {
                    "epoch": epoch,
                    "train_loss": round(total_loss / total, 6) if total else 0.0,
                    "val_macro_f1": round(val_f1, 6),
                    "val_accuracy": round(val_metrics["accuracy"], 6),
                }
            )
            if val_f1 > best_f1:
                best_f1 = val_f1
                best_epoch = epoch
                best_state = {
                    key: value.cpu().clone() for key, value in model.state_dict().items()
                }
                no_improve = 0
            else:
                no_improve += 1
            if no_improve >= self.config["patience"]:
                break
        if best_state is not None:
            model.load_state_dict(best_state)
        model.to("cpu")

        return _write_cloud_bundle(
            model=model,
            output_dir=output_dir,
            manifest=dataset_manifest,
            candidate_version=_candidate_version(
                "moment_light_adapt", dataset_manifest["update_id"]
            ),
            best_epoch=best_epoch,
            best_f1=best_f1,
            history=history,
            cond_mean=cond_mean,
            cond_std=cond_std,
            config=self.config,
            train_sample_count=len(train_raw),
            val_sample_count=len(val_raw),
        )

    def _condition_normalization(
        self, train_arrays: Mapping[str, np.ndarray]
    ) -> tuple[np.ndarray, np.ndarray]:
        baseline = _load_norm(self.condition_norm_path)
        if baseline is not None and baseline[0].size == CONDITION_DIM:
            return baseline
        _, _, cond_mean, cond_std = _normalize_arrays(train_arrays)
        return cond_mean, cond_std

    def _build_model(self) -> Any:
        if self.model_factory is not None:
            return self.model_factory()
        import importlib.util
        import sys

        self._install_training_adapter(sys)
        spec = importlib.util.spec_from_file_location(
            "_offline_moment_model", str(self.model_module_path)
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load MOMENT model definition: {self.model_module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module.build_model(
            str(self.pretrained_path), num_classes=3, condition_dropout=0.0
        )

    @staticmethod
    def _install_training_adapter(sys_module: Any) -> None:
        module_name = "experiments.diagnosis_models.moment.adapter"
        if module_name in sys_module.modules:
            return
        import types

        from cloud_service.moment_backbone import load_moment_backbone

        adapter = types.ModuleType(module_name)
        adapter.load_moment_backbone = load_moment_backbone
        sys_module.modules[module_name] = adapter


def _evaluate_moment(model: Any, loader: Any, device: Any, f1_score: Callable) -> dict[str, Any]:
    import torch
    import torch.nn.functional as F

    model.eval()
    all_preds: list[np.ndarray] = []
    all_labels: list[np.ndarray] = []
    with torch.no_grad():
        for vibration, condition, target in loader:
            vibration = vibration.to(device)
            condition = condition.to(device)
            logits = model(vibration, condition)
            preds = F.softmax(logits, dim=1).argmax(dim=1).cpu().numpy()
            all_preds.append(preds)
            all_labels.append(target.numpy())
    preds = np.concatenate(all_preds)
    labels = np.concatenate(all_labels)
    return {
        "macro_f1": float(
            f1_score(labels, preds, average="macro", zero_division=0)
        ),
        "accuracy": float(np.mean(preds == labels)),
    }


def _write_cloud_bundle(
    *,
    model: Any,
    output_dir: Path,
    manifest: dict[str, Any],
    candidate_version: str,
    best_epoch: int,
    best_f1: float,
    history: list[dict[str, Any]],
    cond_mean: np.ndarray,
    cond_std: np.ndarray,
    config: dict[str, Any],
    train_sample_count: int,
    val_sample_count: int,
) -> dict[str, Any]:
    import torch

    checkpoint_path = output_dir / "best_model.pt"
    torch.save(
        {
            "model_state_dict": model.state_dict(),
            "epoch": best_epoch,
            "best_val_macro_f1": best_f1,
            "architecture": "MomentLightAdaptModel",
            "config": config,
        },
        str(checkpoint_path),
    )
    (output_dir / "checkpoint_sha256.txt").write_text(
        _sha256(checkpoint_path) + "\n", encoding="utf-8"
    )
    _write_json(
        output_dir / "condition_norm.json",
        {"mean": cond_mean.tolist(), "std": cond_std.tolist()},
    )
    _write_json(
        output_dir / "manifest.json",
        {
            "version": candidate_version,
            "model_type": "moment_light_adapt",
            "model_family": "cloud",
            "feature_pipeline_version": manifest["feature_pipeline_version"],
            "training_dataset_id": manifest["dataset_id"],
        },
    )
    _write_csv(
        output_dir / "training_history.csv",
        ["epoch", "train_loss", "val_macro_f1", "val_accuracy"],
        [
            [entry["epoch"], entry["train_loss"], entry["val_macro_f1"], entry["val_accuracy"]]
            for entry in history
        ],
    )
    _write_json(
        output_dir / "model_metadata.json",
        {
            "model_name": "MomentLightAdapt",
            "architecture": "MomentLightAdaptModel",
            "best_epoch": best_epoch,
            "best_val_macro_f1": best_f1,
            "train_sample_count": train_sample_count,
            "val_sample_count": val_sample_count,
            "training_config": config,
        },
    )
    return {
        "candidate_version": candidate_version,
        "artifact_path": str(output_dir),
        "artifact_sha256": _sha256(checkpoint_path),
        "artifact_bundle": _bundle_entries(output_dir),
        "model_type": "moment_light_adapt",
        "feature_pipeline_version": manifest["feature_pipeline_version"],
        "input_feature_schema": manifest["input_feature_schema"],
        "training_dataset_id": manifest["dataset_id"],
        "training_config": config,
        "training_metrics": {
            "best_val_macro_f1": best_f1,
            "best_epoch": best_epoch,
            "epochs": len(history),
            "history": history,
            "train_sample_count": train_sample_count,
            "val_sample_count": val_sample_count,
        },
    }


# ============================================================
# Orchestration
# ============================================================

class OfflineTrainingRunner:
    """Select and run the trainer matching a persisted training plan."""

    def __init__(
        self,
        *,
        edge_trainer: EdgeH5Trainer,
        cloud_trainer: CloudMomentTrainer,
    ) -> None:
        self.edge_trainer = edge_trainer
        self.cloud_trainer = cloud_trainer

    def run(
        self, plan: dict[str, Any], dataset_manifest: dict[str, Any]
    ) -> dict[str, Any]:
        model_type = plan.get("model_type")
        output_dir = plan.get("output_dir")
        if not isinstance(output_dir, str) or not output_dir:
            raise ValueError("TRAINER_PLAN_OUTPUT_DIR_MISSING")
        if model_type == "distilled_h5":
            trainer: Any = self.edge_trainer
        elif model_type == "moment_light_adapt":
            trainer = self.cloud_trainer
        else:
            raise ValueError(f"UNSUPPORTED_MODEL_TYPE={model_type}")
        return trainer.train(dataset_manifest, Path(output_dir))
