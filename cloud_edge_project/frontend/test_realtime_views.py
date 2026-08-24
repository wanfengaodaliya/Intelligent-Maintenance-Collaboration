from pathlib import Path


FRONTEND = Path(__file__).resolve().parent


def _page(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def test_dashboard_does_not_restore_old_streams_into_live_counts() -> None:
    page = _page("index.html")

    assert "localStorage" not in page
    assert "本次页面会话" in page
    assert 'k.id === "packets" ? "本次页面会话 · "' in page
    assert "setKpiVal(k.id, stats.packets);" in page
    assert "setKpiVal(k.id, stats.suggestions);" in page


def test_arbitration_page_lists_decisions_and_arbitrations() -> None:
    page = _page("arbitration.html")

    assert "device-decision-results/recent" in page
    assert "device-arbitration/recent" in page
    assert "暂无冲突仲裁记录" in page
    assert 'id="decisionList" style="max-height:560px;overflow:auto;"' in page
    assert 'id="arbitrationList" style="max-height:560px;overflow:auto;"' in page


def test_analysis_page_lists_recent_saved_results() -> None:
    page = _page("analysis.html")

    assert "global-analysis/recent" in page
    assert 'id="recentList" style="max-height:480px;overflow:auto;"' in page


def test_topology_uses_backend_state_timestamp() -> None:
    page = _page("topology.html")

    assert "state_since_ns" in page
    assert "applied_state_since_ns" in page
    assert "current_state" in page
    assert "刷新时间" in page
    assert "items.slice(0, 12)" not in page
    assert "toFixed(2)" in page
    assert "l.applied_parameters || l.desired_parameters" not in page
    assert "l.applied_state_since_ns ?? l.state_since_ns" in page
