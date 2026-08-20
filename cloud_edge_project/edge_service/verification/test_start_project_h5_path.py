from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
START_SCRIPT = PROJECT_ROOT / "start_project.ps1"


def test_start_script_resolves_h5_checkpoint_from_active_version():
    script = START_SCRIPT.read_text(encoding="utf-8")

    assert "active_version.json" in script
    assert "ConvertFrom-Json" in script
    assert "Join-Path $h5Root $activeVersion" in script
    assert "Join-Path $CloudEdge \"edge_service\\models\\distilled_h5\\best_model.pt\"" not in script
