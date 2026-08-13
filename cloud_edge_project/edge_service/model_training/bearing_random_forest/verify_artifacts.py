from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import joblib

from .audit import audit_run


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sha256sums(path: Path, artifacts: dict[str, Path]) -> dict[str, str]:
    hashes = {name: _sha256(artifact) for name, artifact in sorted(artifacts.items())}
    path.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in hashes.items()),
        encoding="utf-8",
    )
    return hashes


def verify_sha256sums(path: Path, artifacts: dict[str, Path]) -> tuple[str, ...]:
    expected: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        expected[name] = digest
    errors = []
    for name, artifact in sorted(artifacts.items()):
        if name not in expected:
            errors.append(f"SHA-256 清单缺少: {name}")
        elif not artifact.is_file():
            errors.append(f"哈希目标不存在: {name}")
        elif _sha256(artifact) != expected[name]:
            errors.append(f"SHA-256 不匹配: {name}")
    for name in sorted(set(expected) - set(artifacts)):
        errors.append(f"SHA-256 清单含未知目标: {name}")
    return tuple(errors)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def verify_delivery(run_dir: Path | str, model_dir: Path | str) -> dict:
    run_root = Path(run_dir).resolve()
    model_root = Path(model_dir).resolve()
    required_run = {
        "perception_records.jsonl",
        "features.parquet",
        "extraction_errors.csv",
        "extraction_manifest.json",
    }
    required_model = {
        "random_forest.joblib",
        "feature_schema.json",
        "label_mapping.json",
        "model_manifest.json",
        "cross_validation_report.json",
        "frozen_selection.json",
        "final_test_report.json",
        "challenge_report.json",
        "locked_test_consumption.json",
        "plots/cv_winner_confusion_matrix.png",
        "plots/feature_importance.png",
        "plots/final_test_confusion_matrix.png",
        "plots/challenge_confusion_matrix.png",
    }
    missing = [
        f"extraction/{name}" for name in sorted(required_run) if not (run_root / name).is_file()
    ] + [
        f"model/{name}" for name in sorted(required_model) if not (model_root / name).is_file()
    ]
    checks: dict[str, dict] = {
        "required_artifacts": {"passed": not missing, "missing": missing}
    }
    if missing:
        report = {
            "schema_version": "bearing-rf-acceptance/1.0",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "deployable": False,
            "checks": checks,
        }
        model_root.mkdir(parents=True, exist_ok=True)
        _write_json(model_root / "acceptance_report.json", report)
        return report

    extraction_audit = audit_run(run_root)
    cv_report = _read_json(model_root / "cross_validation_report.json")
    final_report = _read_json(model_root / "final_test_report.json")
    challenge_report = _read_json(model_root / "challenge_report.json")
    model_manifest = _read_json(model_root / "model_manifest.json")
    feature_schema = _read_json(model_root / "feature_schema.json")
    artifact = joblib.load(model_root / "random_forest.joblib")

    schema_columns = [item["name"] for item in feature_schema["features"]]
    checks.update(
        {
            "extraction_audit": {
                "passed": extraction_audit.passed,
                "evidence": asdict(extraction_audit),
            },
            "cross_validation_gate": {
                "passed": cv_report.get("cv_gate_passed") is True,
                "macro_f1": cv_report["winner"]["window_macro_f1"],
                "class_recall": cv_report["winner"]["class_recall"],
            },
            "locked_test_gate": {
                "passed": final_report.get("locked_test_gate_passed") is True,
                "macro_f1": final_report["window_macro_f1"],
                "bearing_majority_accuracy": final_report["bearing_majority_accuracy"],
                "bearing_predictions": final_report["bearing_predictions"],
            },
            "challenge_is_separate": {
                "passed": challenge_report.get("mixed_damage_challenge") is True,
                "bearing_ids": challenge_report.get("evaluated_bearing_ids"),
            },
            "model_load_and_schema": {
                "passed": (
                    artifact.get("feature_columns") == schema_columns
                    and model_manifest.get("model_sha256")
                    == _sha256(model_root / "random_forest.joblib")
                ),
                "feature_count": len(schema_columns),
                "experiment": artifact.get("experiment"),
            },
        }
    )

    tests_dir = Path(__file__).resolve().parent / "tests"
    environment = os.environ.copy()
    runtime_root = run_root.parent / "runtime"
    environment.update(
        {
            "TEMP": str(runtime_root / "temp"),
            "TMP": str(runtime_root / "temp"),
            "PYTHONPYCACHEPREFIX": str(runtime_root / "pycache"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "MPLCONFIGDIR": str(runtime_root / "matplotlib"),
        }
    )
    test_result = subprocess.run(
        [sys.executable, "-m", "pytest", str(tests_dir), "-q"],
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    checks["automated_tests"] = {
        "passed": test_result.returncode == 0,
        "command": f"{sys.executable} -m pytest {tests_dir} -q",
        "stdout": test_result.stdout,
        "stderr": test_result.stderr,
    }

    artifacts = {
        **{f"extraction/{name}": run_root / name for name in required_run},
        **{f"model/{name}": model_root / name for name in required_model},
    }
    sums_path = model_root / "SHA256SUMS"
    write_sha256sums(sums_path, artifacts)
    hash_errors = verify_sha256sums(sums_path, artifacts)
    checks["sha256"] = {"passed": not hash_errors, "errors": list(hash_errors)}

    deployable = all(check["passed"] for check in checks.values())
    report = {
        "schema_version": "bearing-rf-acceptance/1.0",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "deployable": deployable,
        "checks": checks,
    }
    _write_json(model_root / "acceptance_report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="复核随机森林训练制品与部署门槛")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--model-dir", required=True, type=Path)
    args = parser.parse_args()
    report = verify_delivery(args.run_dir, args.model_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["deployable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
