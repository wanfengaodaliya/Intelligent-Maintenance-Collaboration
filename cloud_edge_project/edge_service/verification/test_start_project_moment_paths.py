from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
START_SCRIPT = PROJECT_ROOT / "start_project.ps1"


def test_start_script_checks_every_required_moment_asset():
    script = START_SCRIPT.read_text(encoding="utf-8")

    required_defaults = (
        "model_assets\\moment\\releases\\moment-scl05-final\\best_model.pt",
        "model_assets\\moment\\releases\\moment-scl05-final\\condition_norm.json",
        "model_assets\\moment\\releases\\moment-scl05-final",
        "model_assets\\moment\\pretrained\\MOMENT-1-small",
    )
    required_derived_files = (
        '(Join-Path $momentDeployment "moment_model.py")',
        '(Join-Path $momentPretrained "config.json")',
        '(Join-Path $momentPretrained "model.safetensors")',
    )

    assert all(path in script for path in required_defaults)
    assert all(expression in script for expression in required_derived_files)
    assert "$missingMomentFiles" in script
