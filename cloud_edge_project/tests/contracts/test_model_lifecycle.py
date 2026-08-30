from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from core.model_lifecycle import ModelCatalog, ModelDescriptor


def test_model_catalog_is_scenario_neutral_validated_and_immutable() -> None:
    source_compatibility = {"feature_pipeline": "inspection-v1"}
    descriptor = ModelDescriptor(
        model_id="vision-small",
        family="edge",
        default_version="vision-small-v1",
        description="reference inspection model",
        compatibility=source_compatibility,
    )
    source_models = {descriptor.model_id: descriptor}
    catalog = ModelCatalog(
        scenario_id="inspection",
        default_model_id="vision-small",
        models=source_models,
    )

    source_compatibility["feature_pipeline"] = "changed"
    source_models.clear()

    assert catalog.require("vision-small") is descriptor
    assert catalog.require("vision-small").compatibility == {
        "feature_pipeline": "inspection-v1"
    }
    with pytest.raises(TypeError):
        catalog.models["new"] = descriptor  # type: ignore[index]
    with pytest.raises(TypeError):
        descriptor.compatibility["new"] = "value"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        catalog.default_model_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "catalog",
    [
        lambda: ModelCatalog("", "model", {"model": _descriptor("model")}),
        lambda: ModelCatalog("inspection", "missing", {"model": _descriptor("model")}),
        lambda: ModelCatalog(
            "inspection",
            "model",
            {"wrong-key": _descriptor("model")},
        ),
    ],
)
def test_model_catalog_rejects_invalid_identity(catalog) -> None:
    with pytest.raises(ValueError):
        catalog()


def test_model_catalog_preserves_legacy_unknown_type_error() -> None:
    catalog = ModelCatalog(
        "inspection",
        "model",
        {"model": _descriptor("model")},
    )

    with pytest.raises(ValueError, match="UNSUPPORTED_MODEL_TYPE=unknown"):
        catalog.require("unknown")


def _descriptor(model_id: str) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        family="cloud",
        default_version="v1",
        description="test model",
    )
