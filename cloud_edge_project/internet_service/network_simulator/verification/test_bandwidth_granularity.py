"""AUD-10: 1~7 Kbps 带宽不得被静默抬高为 8 Kbps。

Toxiproxy 带宽 toxic 以整数 KB/s 生效，最小可表示粒度为 1 KB/s = 8 Kbps。
配置阶段直接拒绝低于 8 Kbps 的正数带宽，转换函数同样显式失败。
"""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest
from pydantic import ValidationError

from controller.config_loader import FloatRange, IntegerRange, StateConfig
from domain.models import MIN_APPLICABLE_BANDWIDTH_KBPS
from plugins.toxiproxy.client import kbps_to_kbytes_per_second


NETWORK_ROOT = Path(__file__).resolve().parents[1]


def _state_config(bandwidth_min: int) -> StateConfig:
    return StateConfig(
        latency_ms=IntegerRange(min=8, max=25),
        jitter_ms=IntegerRange(min=1, max=5),
        bandwidth_kbps=IntegerRange(min=bandwidth_min, max=2500),
        packet_loss_percent=FloatRange(min=1.0, max=5.0),
        disconnect_mode="none",
    )


@pytest.mark.parametrize("bandwidth_min", [0, 1, 4, 7])
def test_state_config_rejects_bandwidth_below_8kbps(bandwidth_min: int) -> None:
    with pytest.raises(ValidationError, match="at least 8 Kbps"):
        _state_config(bandwidth_min)


def test_state_config_accepts_bandwidth_at_8kbps() -> None:
    config = _state_config(MIN_APPLICABLE_BANDWIDTH_KBPS)
    assert config.bandwidth_kbps is not None
    assert config.bandwidth_kbps.min == MIN_APPLICABLE_BANDWIDTH_KBPS


@pytest.mark.parametrize("bandwidth_kbps", [1, 4, 7])
def test_kbps_conversion_rejects_values_below_8kbps(bandwidth_kbps: int) -> None:
    with pytest.raises(ValueError, match="cannot be represented"):
        kbps_to_kbytes_per_second(bandwidth_kbps)


def test_kbps_conversion_maps_representable_values_exactly() -> None:
    assert kbps_to_kbytes_per_second(8) == 1
    assert kbps_to_kbytes_per_second(12) == 2  # 1.5 KB/s 半向上取整
    assert kbps_to_kbytes_per_second(16) == 2
    assert kbps_to_kbytes_per_second(40000) == 5000


def test_cli_check_config_rejects_sub_8kbps_config(tmp_path: Path) -> None:
    from controller.main import cli

    config_dir = tmp_path / "config"
    shutil.copytree(NETWORK_ROOT / "config", config_dir)
    states_file = config_dir / "network_states.yaml"
    states_file.write_text(
        states_file.read_text(encoding="utf-8").replace(
            "bandwidth_kbps: {min: 500, max: 2500}",
            "bandwidth_kbps: {min: 4, max: 2500}",
        ),
        encoding="utf-8",
    )

    assert cli(["--config-dir", str(config_dir), "--check-config"]) == 1


def test_cli_check_config_accepts_shipped_config() -> None:
    from controller.main import cli

    assert cli(["--config-dir", str(NETWORK_ROOT / "config"), "--check-config"]) == 0
