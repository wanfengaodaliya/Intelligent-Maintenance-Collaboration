from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
START_SCRIPT = PROJECT_ROOT / "start_project.ps1"


def test_start_script_checks_every_required_moment_asset():
    script = START_SCRIPT.read_text(encoding="utf-8")

    required_paths = (
        "model_assets\\moment\\releases\\moment-scl05-final\\best_model.pt",
        "model_assets\\moment\\releases\\moment-scl05-final\\condition_norm.json",
        "model_assets\\moment\\releases\\moment-scl05-final\\moment_model.py",
        "model_assets\\moment\\pretrained\\MOMENT-1-small\\config.json",
        "model_assets\\moment\\pretrained\\MOMENT-1-small\\model.safetensors",
    )
    assert all(path in script for path in required_paths)
