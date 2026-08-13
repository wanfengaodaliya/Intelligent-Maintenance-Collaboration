from __future__ import annotations

import pytest

from edge_status_reporter.config import EdgeStatusReporterConfig, StatusTargetConfig


def test_default_config_enables_both_targets_with_project_ports() -> None:
    config = EdgeStatusReporterConfig.from_env(default_model_version="edge_bearing_mock", environ={})
    assert config.enabled is True
    assert config.interval_seconds == 1.0
    assert config.scheduler.url == "http://127.0.0.1:18011/scheduler/edge-nodes/status"
    assert config.cloud.url == "http://127.0.0.1:18021/cloud/edge-status"
    assert config.network.url.endswith("/edge_01__to__scheduler__http")
    assert config.resource.mode == "system"


def test_disabled_config_ignores_other_reporter_values() -> None:
    config = EdgeStatusReporterConfig.from_env(
        default_model_version="edge_bearing_mock",
        environ={
            "EDGE_STATUS_REPORTER_ENABLED": "false",
            "EDGE_STATUS_INTERVAL_SECONDS": "invalid",
            "EDGE_STATUS_SCHEDULER_URL": "invalid",
        },
    )
    assert config.enabled is False
    assert config.scheduler.enabled is False
    assert config.cloud.enabled is False


def test_process_mode_requires_explicit_quotas() -> None:
    with pytest.raises(ValueError, match="LOGICAL_CPU_COUNT"):
        EdgeStatusReporterConfig.from_env(default_model_version="edge_bearing_mock", environ={"EDGE_STATUS_RESOURCE_MODE": "process"})


def test_environment_overrides_model_targets_and_process_quotas() -> None:
    config = EdgeStatusReporterConfig.from_env(
        default_model_version="old",
        environ={
            "EDGE_STATUS_MODEL_VERSION": "bearing-v2",
            "EDGE_STATUS_SCHEDULER_URL": "http://scheduler-proxy:9101/status",
            "EDGE_STATUS_CLOUD_URL": "http://cloud-proxy:9102/status",
            "EDGE_STATUS_RESOURCE_MODE": "process",
            "EDGE_STATUS_PROCESS_LOGICAL_CPU_COUNT": "4",
            "EDGE_STATUS_PROCESS_MEMORY_LIMIT_MB": "8192",
            "EDGE_STATUS_GPU_AVAILABLE_OVERRIDE": "true",
            "EDGE_STATUS_NPU_AVAILABLE_OVERRIDE": "0",
        },
    )
    assert config.model_version == "bearing-v2"
    assert config.scheduler.url == "http://scheduler-proxy:9101/status"
    assert config.cloud.url == "http://cloud-proxy:9102/status"
    assert config.resource.logical_cpu_count == 4
    assert config.resource.memory_limit_mb == 8192.0
    assert config.accelerator.gpu_available_override is True
    assert config.accelerator.npu_available_override is False


def test_target_config_rejects_non_integer_retry_count() -> None:
    with pytest.raises(ValueError, match="retry_count"):
        StatusTargetConfig(
            "scheduler",
            True,
            "http://127.0.0.1:8003/status",
            0.5,
            1.5,
        )


def test_disabled_target_ignores_its_url_timeout_and_retry_values() -> None:
    config = EdgeStatusReporterConfig.from_env(
        default_model_version="model",
        environ={
            "EDGE_STATUS_SCHEDULER_ENABLED": "false",
            "EDGE_STATUS_SCHEDULER_URL": "not-a-url",
            "EDGE_STATUS_SCHEDULER_TIMEOUT_SECONDS": "not-a-number",
            "EDGE_STATUS_SCHEDULER_RETRY_COUNT": "not-an-integer",
        },
    )

    assert config.enabled is True
    assert config.scheduler.enabled is False
    assert config.cloud.enabled is True


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:not-a-port/status",
        "http://127.0.0.1:70000/status",
    ],
)
def test_target_config_rejects_invalid_url_ports(url: str) -> None:
    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        StatusTargetConfig("scheduler", True, url, 0.5, 0)
