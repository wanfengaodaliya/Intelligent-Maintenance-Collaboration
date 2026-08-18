from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import pickle
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import RandomForestClassifier

from .dataset import (
    ARTIFICIAL_SPLIT,
    CHALLENGE_SPLIT,
    DEVELOPMENT_SPLIT,
    LOCKED_TEST_SPLIT,
    DatasetManifest,
    load_dataset_manifest,
)
from .evaluate import (
    LABELS,
    evaluate_predictions,
    select_winner,
    to_binary_label,
)
from .features import FEATURE_COLUMNS


@dataclass(frozen=True)
class FoldSplit:
    train_bearing_ids: set[str]
    validation_bearing_ids: set[str]
    artificial_bearing_ids: set[str]


class LockedTestAlreadyConsumed(RuntimeError):
    pass


def build_fold_split(
    manifest: DatasetManifest,
    validation_fold: int,
    *,
    include_artificial: bool,
) -> FoldSplit:
    if validation_fold not in {1, 2, 3, 4}:
        raise ValueError("validation_fold 必须是 1..4")
    validation = {
        item.bearing_id
        for item in manifest.bearings
        if item.split == DEVELOPMENT_SPLIT and item.validation_fold == validation_fold
    }
    real_training = {
        item.bearing_id
        for item in manifest.bearings
        if item.split == DEVELOPMENT_SPLIT and item.validation_fold != validation_fold
    }
    artificial = (
        {
            item.bearing_id
            for item in manifest.bearings
            if item.split == ARTIFICIAL_SPLIT
        }
        if include_artificial
        else set()
    )
    return FoldSplit(real_training | artificial, validation, artificial)


def balance_training_rows(
    frame: pd.DataFrame,
    *,
    random_state: int,
) -> tuple[pd.DataFrame, np.ndarray]:
    if frame.empty:
        raise ValueError("训练数据为空")
    counts = frame.groupby("source_bearing_code").size()
    rows_per_bearing = int(counts.min())
    balanced = pd.concat(
        [
            group.sample(n=rows_per_bearing, random_state=random_state)
            for _, group in frame.groupby("source_bearing_code", sort=True)
        ]
    ).sort_index()
    weights = np.full(len(balanced), 1.0 / rows_per_bearing, dtype=np.float64)
    return balanced, weights


def _feature_columns(feature_set: str) -> tuple[str, ...]:
    if feature_set == "vibration_6":
        return FEATURE_COLUMNS[:6]
    if feature_set == "full_27":
        return FEATURE_COLUMNS
    raise ValueError(f"未知特征集: {feature_set}")


def _new_model(config: dict) -> RandomForestClassifier:
    parameters = dict(config["random_forest"])
    parameters["random_state"] = int(config["random_seed"])
    if not 1 <= int(parameters["n_jobs"]) <= 6:
        raise ValueError("随机森林 n_jobs 必须位于 1..6")
    return RandomForestClassifier(**parameters)


def _aligned_probabilities(
    model: RandomForestClassifier,
    frame: pd.DataFrame,
    feature_columns: tuple[str, ...],
) -> np.ndarray:
    raw = model.predict_proba(frame.loc[:, feature_columns])
    aligned = np.zeros((len(frame), len(LABELS)), dtype=np.float64)
    class_positions = {label: index for index, label in enumerate(model.classes_)}
    for target_index, label in enumerate(LABELS):
        if label in class_positions:
            aligned[:, target_index] = raw[:, class_positions[label]]
    return aligned


def _probability_report(y_true: list[str], probabilities: np.ndarray) -> dict:
    one_hot = np.zeros_like(probabilities)
    for row, label in enumerate(y_true):
        one_hot[row, LABELS.index(label)] = 1.0
    true_probabilities = probabilities[np.arange(len(y_true)), np.argmax(one_hot, axis=1)]
    multiclass_log_loss = -float(
        np.mean(np.log(np.clip(true_probabilities, np.finfo(np.float64).eps, 1.0)))
    )
    return {
        "multiclass_log_loss": multiclass_log_loss,
        "multiclass_brier_score": float(np.mean(np.sum((probabilities - one_hot) ** 2, axis=1))),
        "probability_note": "随机森林原始概率，未进行校准",
    }


def run_cross_validation(
    frame: pd.DataFrame,
    manifest: DatasetManifest,
    config: dict,
) -> dict:
    usable = frame[frame["perception_quality_status"] == "good"].copy()
    usable["label"] = usable["label"].map(to_binary_label)
    experiment_reports: list[dict] = []
    for experiment in config["experiments"]:
        feature_columns = _feature_columns(experiment["feature_set"])
        fold_reports: list[dict] = []
        all_true: list[str] = []
        all_predicted: list[str] = []
        all_bearings: list[str] = []
        model_sizes: list[int] = []

        for fold in range(1, 5):
            split = build_fold_split(
                manifest,
                fold,
                include_artificial=bool(experiment["include_artificial"]),
            )
            training = usable[
                usable["source_bearing_code"].isin(split.train_bearing_ids)
            ]
            validation = usable[
                usable["source_bearing_code"].isin(split.validation_bearing_ids)
            ]
            balanced, sample_weight = balance_training_rows(
                training,
                random_state=int(config["random_seed"]) + fold,
            )
            model = _new_model(config)
            fit_started = time.perf_counter()
            model.fit(
                balanced.loc[:, feature_columns],
                balanced["label"],
                sample_weight=sample_weight,
            )
            fit_seconds = time.perf_counter() - fit_started
            inference_started = time.perf_counter()
            predicted = model.predict(validation.loc[:, feature_columns]).tolist()
            probabilities = _aligned_probabilities(model, validation, feature_columns)
            inference_seconds = time.perf_counter() - inference_started
            y_true = validation["label"].tolist()
            bearing_ids = validation["source_bearing_code"].tolist()
            metrics = evaluate_predictions(y_true, predicted, bearing_ids)
            model_size = len(pickle.dumps(model, protocol=5))
            model_sizes.append(model_size)
            fold_reports.append(
                {
                    "fold": fold,
                    "train_bearing_ids": sorted(split.train_bearing_ids),
                    "validation_bearing_ids": sorted(split.validation_bearing_ids),
                    "training_rows": len(balanced),
                    "validation_rows": len(validation),
                    "fit_seconds": fit_seconds,
                    "inference_milliseconds_per_window": (
                        inference_seconds * 1000.0 / len(validation)
                    ),
                    "model_bytes": model_size,
                    **metrics,
                    **_probability_report(y_true, probabilities),
                }
            )
            all_true.extend(y_true)
            all_predicted.extend(predicted)
            all_bearings.extend(bearing_ids)

        aggregate = evaluate_predictions(all_true, all_predicted, all_bearings)
        experiment_reports.append(
            {
                "experiment": experiment["name"],
                "include_artificial": bool(experiment["include_artificial"]),
                "feature_set": experiment["feature_set"],
                "feature_columns": list(feature_columns),
                "feature_count": len(feature_columns),
                "model_bytes": max(model_sizes),
                "folds": fold_reports,
                **aggregate,
            }
        )

    winner = select_winner(
        experiment_reports,
        macro_f1_tie_tolerance=float(
            config["selection"]["macro_f1_tie_tolerance"]
        ),
    )
    cv_gate_passed = (
        winner["window_macro_f1"] >= 0.90
        and all(value >= 0.85 for value in winner["class_recall"].values())
    )
    return {
        "schema_version": "bearing-rf-cross-validation/2.0",
        "task": "binary_fault_detection",
        "random_seed": int(config["random_seed"]),
        "experiments": experiment_reports,
        "winner": winner,
        "cv_gate_passed": cv_gate_passed,
        "gates": {"window_macro_f1_minimum": 0.90, "each_class_recall_minimum": 0.85},
    }


def fit_final_model(
    frame: pd.DataFrame,
    manifest: DatasetManifest,
    winner: dict,
    config: dict,
) -> dict:
    training_ids = {
        item.bearing_id for item in manifest.bearings if item.split == DEVELOPMENT_SPLIT
    }
    if winner["include_artificial"]:
        training_ids.update(
            item.bearing_id
            for item in manifest.bearings
            if item.split == ARTIFICIAL_SPLIT
        )
    training = frame[
        frame["source_bearing_code"].isin(training_ids)
        & (frame["perception_quality_status"] == "good")
    ].copy()
    training["label"] = training["label"].map(to_binary_label)
    balanced, sample_weight = balance_training_rows(
        training,
        random_state=int(config["random_seed"]),
    )
    feature_columns = tuple(winner["feature_columns"])
    model = _new_model(config)
    model.fit(
        balanced.loc[:, feature_columns],
        balanced["label"],
        sample_weight=sample_weight,
    )
    return {
        "artifact_schema_version": "bearing-rf-model/2.0",
        "task": "binary_fault_detection",
        "experiment": winner["experiment"],
        "feature_columns": list(feature_columns),
        "labels": list(LABELS),
        "random_seed": int(config["random_seed"]),
        "random_forest_parameters": model.get_params(),
        "training_bearing_ids": sorted(training_ids),
        "training_rows": len(balanced),
        "estimator": model,
    }


def predict_frame(artifact: dict, frame: pd.DataFrame) -> np.ndarray:
    feature_columns = tuple(artifact["feature_columns"])
    missing = [column for column in feature_columns if column not in frame.columns]
    if missing:
        raise ValueError(f"预测表缺少特征: {missing}")
    return artifact["estimator"].predict(frame.loc[:, feature_columns])


def read_feature_splits(path: Path | str, splits: set[str]) -> pd.DataFrame:
    if not splits:
        raise ValueError("至少选择一个数据 split")
    frame = pd.read_parquet(
        Path(path),
        engine="pyarrow",
        filters=[("split", "in", sorted(splits))],
    )
    actual = set(frame["split"])
    if not actual <= splits:
        raise ValueError(f"Parquet 过滤失败，读入了未请求分组: {sorted(actual - splits)}")
    return frame


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _plot_confusion_matrix(report: dict, path: Path, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(path.parent.parent / ".matplotlib"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    matrix = np.asarray(report["confusion_matrix"], dtype=int)
    figure, axis = plt.subplots(figsize=(6.4, 5.2))
    image = axis.imshow(matrix, cmap="Blues")
    figure.colorbar(image, ax=axis)
    axis.set_xticks(range(len(LABELS)), LABELS, rotation=20, ha="right")
    axis.set_yticks(range(len(LABELS)), LABELS)
    axis.set_xlabel("Predicted")
    axis.set_ylabel("True")
    axis.set_title(title)
    threshold = matrix.max() / 2.0 if matrix.size else 0
    for row in range(matrix.shape[0]):
        for column in range(matrix.shape[1]):
            axis.text(
                column,
                row,
                str(matrix[row, column]),
                ha="center",
                va="center",
                color="white" if matrix[row, column] > threshold else "black",
            )
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _plot_feature_importance(artifact: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(path.parent.parent / ".matplotlib"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names = artifact["feature_columns"]
    values = artifact["estimator"].feature_importances_
    order = np.argsort(values)
    figure, axis = plt.subplots(figsize=(8.5, max(4.5, len(names) * 0.28)))
    axis.barh(np.asarray(names)[order], np.asarray(values)[order])
    axis.set_xlabel("Random forest feature importance")
    axis.set_title(f"{artifact['experiment']} feature importance")
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def run_cv_to_disk(
    features_path: Path | str,
    dataset_root: Path | str,
    model_dir: Path | str,
    config_path: Path | str,
) -> dict:
    features = Path(features_path).resolve()
    dataset = Path(dataset_root).resolve()
    destination = Path(model_dir).resolve()
    configuration = Path(config_path).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    config = _read_json(configuration)
    manifest = load_dataset_manifest(dataset)
    frame = read_feature_splits(features, {DEVELOPMENT_SPLIT, ARTIFICIAL_SPLIT})
    report = run_cross_validation(frame, manifest, config)
    report.update(
        {
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "features_sha256": _sha256(features),
            "dataset_manifest_sha256": _sha256(dataset / "dataset_split.csv"),
            "cv_folds_sha256": _sha256(dataset / "cv_folds.csv"),
            "experiments_config_sha256": _sha256(configuration),
        }
    )
    report_path = destination / "cross_validation_report.json"
    _write_json(report_path, report)
    winner = report["winner"]
    _plot_confusion_matrix(
        winner,
        destination / "plots" / "cv_winner_confusion_matrix.png",
        f"CV winner {winner['experiment']}",
    )
    _write_json(
        destination / "frozen_selection.json",
        {
            "schema_version": "bearing-rf-frozen-selection/2.0",
            "task": "binary_fault_detection",
            "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
            "cv_gate_passed": report["cv_gate_passed"],
            "experiment": winner["experiment"],
            "include_artificial": winner["include_artificial"],
            "feature_set": winner["feature_set"],
            "feature_columns": winner["feature_columns"],
            "random_forest": config["random_forest"],
            "random_seed": config["random_seed"],
            "cross_validation_report_sha256": _sha256(report_path),
            "experiments_config_sha256": _sha256(configuration),
        },
    )
    return report


def finalize_to_disk(
    features_path: Path | str,
    dataset_root: Path | str,
    model_dir: Path | str,
    config_path: Path | str,
) -> dict:
    features = Path(features_path).resolve()
    dataset = Path(dataset_root).resolve()
    destination = Path(model_dir).resolve()
    configuration = Path(config_path).resolve()
    report_path = destination / "cross_validation_report.json"
    frozen_path = destination / "frozen_selection.json"
    report = _read_json(report_path)
    frozen = _read_json(frozen_path)
    if report.get("cv_gate_passed") is not True or frozen.get("cv_gate_passed") is not True:
        raise RuntimeError("交叉验证未通过门槛，禁止生成最终模型")
    if frozen["cross_validation_report_sha256"] != _sha256(report_path):
        raise RuntimeError("交叉验证报告在冻结后发生变化")
    if frozen["experiments_config_sha256"] != _sha256(configuration):
        raise RuntimeError("实验配置在冻结后发生变化")

    config = _read_json(configuration)
    manifest = load_dataset_manifest(dataset)
    requested_splits = {DEVELOPMENT_SPLIT}
    if report["winner"]["include_artificial"]:
        requested_splits.add(ARTIFICIAL_SPLIT)
    frame = read_feature_splits(features, requested_splits)
    artifact = fit_final_model(frame, manifest, report["winner"], config)
    model_path = destination / "random_forest.joblib"
    temporary_model = model_path.with_suffix(".joblib.tmp")
    joblib.dump(artifact, temporary_model, compress=3)
    loaded = joblib.load(temporary_model)
    probe = frame.head(min(len(frame), 100))
    if predict_frame(artifact, probe).tolist() != predict_frame(loaded, probe).tolist():
        raise RuntimeError("模型保存后重新加载的预测不一致")
    temporary_model.replace(model_path)

    schema_dir = Path(__file__).resolve().parent / "schema"
    shutil.copy2(schema_dir / "feature_schema.json", destination / "feature_schema.json")
    shutil.copy2(schema_dir / "label_mapping.json", destination / "label_mapping.json")
    _plot_feature_importance(
        artifact,
        destination / "plots" / "feature_importance.png",
    )
    _write_json(
        destination / "model_manifest.json",
        {
            "schema_version": "bearing-rf-model-manifest/2.0",
            "task": "binary_fault_detection",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "experiment": artifact["experiment"],
            "training_bearing_ids": artifact["training_bearing_ids"],
            "training_rows": artifact["training_rows"],
            "feature_columns": artifact["feature_columns"],
            "random_seed": artifact["random_seed"],
            "random_forest_parameters": artifact["random_forest_parameters"],
            "model_sha256": _sha256(model_path),
            "feature_schema_sha256": _sha256(schema_dir / "feature_schema.json"),
            "label_mapping_sha256": _sha256(schema_dir / "label_mapping.json"),
            "features_sha256": _sha256(features),
            "dataset_manifest_sha256": _sha256(dataset / "dataset_split.csv"),
            "cv_folds_sha256": _sha256(dataset / "cv_folds.csv"),
            "frozen_selection_sha256": _sha256(frozen_path),
            "runtime": {
                "python": platform.python_version(),
                "executable": sys.executable,
                "numpy": np.__version__,
                "pandas": pd.__version__,
                "scikit_learn": sklearn.__version__,
                "joblib": joblib.__version__,
            },
        },
    )
    return artifact


def run_locked_test(
    artifact: dict,
    locked_frame: pd.DataFrame,
    model_dir: Path | str,
) -> dict:
    expected_bearings = {"K006", "KA30", "KI17"}
    usable = locked_frame[
        locked_frame["perception_quality_status"] == "good"
    ].copy()
    actual_bearings = set(usable["source_bearing_code"])
    if actual_bearings != expected_bearings:
        raise ValueError(
            f"锁定测试必须且只能包含 {sorted(expected_bearings)}，实际为 {sorted(actual_bearings)}"
        )

    destination = Path(model_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    marker_path = destination / "locked_test_consumption.json"
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        with marker_path.open("x", encoding="utf-8") as marker:
            json.dump(
                {"status": "started", "started_at_utc": started_at},
                marker,
                ensure_ascii=False,
                indent=2,
            )
            marker.write("\n")
    except FileExistsError as exc:
        raise LockedTestAlreadyConsumed(
            f"锁定测试已被消费，禁止重复运行: {marker_path}"
        ) from exc

    predicted = predict_frame(artifact, usable).tolist()
    report = evaluate_predictions(
        usable["label"].map(to_binary_label).tolist(),
        predicted,
        usable["source_bearing_code"].tolist(),
    )
    report.update(
        {
            "schema_version": "bearing-rf-locked-test/2.0",
            "task": "binary_fault_detection",
            "experiment": artifact["experiment"],
            "evaluated_bearing_ids": sorted(expected_bearings),
            "evaluated_windows": len(usable),
            "locked_test_gate_passed": (
                report["bearing_majority_accuracy"] == 1.0
                and report["window_macro_f1"] >= 0.85
            ),
            "generalization_note": (
                "这是数据集内锁定测试，不代表工业现场外部泛化已经得到证明"
            ),
        }
    )
    report_path = destination / "final_test_report.json"
    _write_json(report_path, report)
    _plot_confusion_matrix(
        report,
        destination / "plots" / "final_test_confusion_matrix.png",
        "Locked final test",
    )
    _write_json(
        marker_path,
        {
            "status": "complete",
            "started_at_utc": started_at,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "report_path": str(report_path),
        },
    )
    return report


def run_challenge_test(
    artifact: dict,
    challenge_frame: pd.DataFrame,
    model_dir: Path | str,
) -> dict:
    expected_bearings = {"KB23", "KB24", "KB27"}
    usable = challenge_frame[
        challenge_frame["perception_quality_status"] == "good"
    ].copy()
    actual_bearings = set(usable["source_bearing_code"])
    if actual_bearings != expected_bearings:
        raise ValueError(
            f"挑战集必须且只能包含 {sorted(expected_bearings)}，实际为 {sorted(actual_bearings)}"
        )
    predicted = predict_frame(artifact, usable).tolist()
    report = evaluate_predictions(
        usable["label"].map(to_binary_label).tolist(),
        predicted,
        usable["source_bearing_code"].tolist(),
    )
    report.update(
        {
            "schema_version": "bearing-rf-mixed-damage-challenge/2.0",
            "task": "binary_fault_detection",
            "experiment": artifact["experiment"],
            "evaluated_bearing_ids": sorted(expected_bearings),
            "evaluated_windows": len(usable),
            "mixed_damage_challenge": True,
            "metric_note": "混合损伤挑战结果不并入纯三分类锁定测试指标",
        }
    )
    destination = Path(model_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    _write_json(destination / "challenge_report.json", report)
    _plot_confusion_matrix(
        report,
        destination / "plots" / "challenge_confusion_matrix.png",
        "Mixed-damage challenge",
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="轴承三分类随机森林训练与评价")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("cv", "finalize", "locked-test", "challenge"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--features", required=True, type=Path)
        subparser.add_argument("--model-dir", required=True, type=Path)
        if command in {"cv", "finalize"}:
            subparser.add_argument("--dataset-root", required=True, type=Path)
            subparser.add_argument("--experiments", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "cv":
        report = run_cv_to_disk(
            args.features,
            args.dataset_root,
            args.model_dir,
            args.experiments,
        )
        print(
            json.dumps(
                {
                    "cv_gate_passed": report["cv_gate_passed"],
                    "winner": report["winner"]["experiment"],
                    "window_macro_f1": report["winner"]["window_macro_f1"],
                    "class_recall": report["winner"]["class_recall"],
                },
                ensure_ascii=False,
            )
        )
        return 0 if report["cv_gate_passed"] else 1
    if args.command == "finalize":
        artifact = finalize_to_disk(
            args.features,
            args.dataset_root,
            args.model_dir,
            args.experiments,
        )
        print(
            json.dumps(
                {
                    "experiment": artifact["experiment"],
                    "training_rows": artifact["training_rows"],
                    "model": str(Path(args.model_dir).resolve() / "random_forest.joblib"),
                },
                ensure_ascii=False,
            )
        )
        return 0

    artifact = joblib.load(Path(args.model_dir) / "random_forest.joblib")
    if args.command == "locked-test":
        frame = read_feature_splits(args.features, {LOCKED_TEST_SPLIT})
        report = run_locked_test(artifact, frame, args.model_dir)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["locked_test_gate_passed"] else 1
    frame = read_feature_splits(args.features, {CHALLENGE_SPLIT})
    report = run_challenge_test(artifact, frame, args.model_dir)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
