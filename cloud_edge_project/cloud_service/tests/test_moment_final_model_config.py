from cloud_service.config import load_cloud_settings
from cloud_service.moment_light_adapt import MODEL_VERSION


def test_default_moment_artifacts_and_public_version_use_scl05_final_model(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CLOUD_MOMENT_CHECKPOINT_PATH", raising=False)
    monkeypatch.delenv("CLOUD_MOMENT_CONDITION_NORM_PATH", raising=False)

    settings = load_cloud_settings()

    assert "SCL05/fold_3/best_model.pt" in settings.moment_checkpoint_path.as_posix()
    assert "SCL05/fold_3/condition_norm.json" in settings.moment_condition_norm_path.as_posix()
    assert MODEL_VERSION == "moment-scl05-final"


def test_moment_artifact_environment_overrides_remain_available(monkeypatch, tmp_path) -> None:
    checkpoint = tmp_path / "checkpoint.pt"
    condition_norm = tmp_path / "condition_norm.json"
    monkeypatch.setenv("CLOUD_MOMENT_CHECKPOINT_PATH", str(checkpoint))
    monkeypatch.setenv("CLOUD_MOMENT_CONDITION_NORM_PATH", str(condition_norm))

    settings = load_cloud_settings()

    assert settings.moment_checkpoint_path == checkpoint
    assert settings.moment_condition_norm_path == condition_norm
