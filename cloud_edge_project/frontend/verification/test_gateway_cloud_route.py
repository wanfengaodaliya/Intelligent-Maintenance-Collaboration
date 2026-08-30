from __future__ import annotations

import sys
from pathlib import Path

FRONTEND_ROOT = Path(__file__).resolve().parents[1]
# server.py uses import-local modules (dashboard_state, mqtt_payload), so its
# sibling directory must be importable as an absolute path.
sys.path.insert(0, str(FRONTEND_ROOT))
from frontend.server import BACKENDS  # noqa: E402

INDEX_HTML = FRONTEND_ROOT / "index.html"


def _gateway_target(api_path: str) -> str | None:
    """Replicate the frontend gateway's `/api/{backend}/rest...` mapping exactly.

    This mirrors ``GatewayHandler._handle_proxy``: the first path segment after
    ``/api/`` selects the backend, and every remaining segment is concatenated
    verbatim onto the backend base URL. It is intentionally a thin copy so the
    test catches regressions in how the frontend must address Cloud's own
    ``/cloud`` route prefix.
    """
    parts = api_path.split("?", 1)
    segments = parts[0].split("/")  # ['', 'api', backend, rest...]
    if len(segments) < 3 or segments[1] != "api" or segments[2] not in BACKENDS:
        return None
    base = BACKENDS[segments[2]]
    target = base + "/" + "/".join(segments[3:])
    if len(parts) > 1 and parts[1]:
        target += "?" + parts[1]
    return target


def test_model_update_call_uses_double_layer_cloud_prefix() -> None:
    """The Cloud model-update UI call must keep the second ``cloud`` segment.

    Cloud serves model-update under ``/cloud/model-update/recent``. The gateway
    strips the leading ``/api/cloud/`` so the frontend must encode the whole
    Cloud-relative path, i.e. ``/api/cloud/cloud/model-update/recent``.
    """
    script = INDEX_HTML.read_text(encoding="utf-8")
    assert 'Api.get("cloud/cloud/model-update/recent?limit=20")' in script
    assert not script.startswith('Api.get("cloud/model-update/recent')


def test_double_layer_path_reaches_cloud_model_update_recent() -> None:
    target = _gateway_target("/api/cloud/cloud/model-update/recent?limit=20")
    assert target == "http://127.0.0.1:8004/cloud/model-update/recent?limit=20"


def test_single_layer_cloud_path_would_404():  # noqa: ANN201 - documents the original bug
    target = _gateway_target("/api/cloud/model-update/recent?limit=20")
    # Cloud does not serve model-update at the gateway-stripped path; this is
    # exactly the 404 the frontend used to hit before the fix.
    assert target == "http://127.0.0.1:8004/model-update/recent?limit=20"


def test_all_cloud_ui_calls_carry_cloud_prefix() -> None:
    """Every `cloud` proxied call in the frontend pages must use the double layer."""
    import re

    for html in ("analysis.html", "arbitration.html"):
        script = (FRONTEND_ROOT / html).read_text(encoding="utf-8")
        tokens = re.findall(r'Api\.get\("cloud/([^"]*)"', script)
        assert tokens, f"expected at least one cloud call in {html}"
        assert all(token.startswith("cloud/") for token in tokens), tokens