from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
START_SCRIPT = PROJECT_ROOT / "start_project.ps1"


def test_start_script_stops_when_native_commands_or_health_checks_fail():
    script = START_SCRIPT.read_text(encoding="utf-8")

    assert "$LASTEXITCODE -ne 0" in script
    assert "$allHealthy = $true" in script
    assert "if (-not $allHealthy)" in script
    assert "Write-Host \"`n========== Done ==========\"" in script
