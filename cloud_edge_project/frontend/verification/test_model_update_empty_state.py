from pathlib import Path


INDEX_HTML = Path(__file__).resolve().parents[1] / "index.html"


def test_empty_model_update_panel_explains_latest_analysis_state() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")

    assert 'Api.get("cloud/global-analysis/recent?limit=1")' in source
    assert "设备故障不等于模型失准" in source
    assert "最新全局分析未发现模型偏差" in source
