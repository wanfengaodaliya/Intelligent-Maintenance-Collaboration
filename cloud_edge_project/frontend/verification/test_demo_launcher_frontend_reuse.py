from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "start_frontend_demo.ps1"


def test_launcher_reuses_the_existing_project_frontend() -> None:
    source = SCRIPT.read_text(encoding="utf-8-sig")

    assert "function Test-FrontendGateway" in source
    assert "$ReuseFrontend = Test-FrontendGateway" in source
    assert "Reusing the existing frontend on port 8088" in source
    assert "if (-not $ReuseFrontend)" in source
