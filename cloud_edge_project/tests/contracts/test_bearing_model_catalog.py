from cloud_service.model_update.model_types import (
    MODEL_TYPE_SPECS,
    VALID_MODEL_TYPES,
    ModelTypeSpec,
)
from compatibility.bearing_v12.legacy_exports import BEARING_MODEL_CATALOG
from scenarios.bearing.cloud.model_update.provider import BearingModelUpdateProvider


def test_bearing_catalog_preserves_legacy_model_metadata() -> None:
    assert tuple(BEARING_MODEL_CATALOG.models) == (
        "distilled_h5",
        "moment_light_adapt",
    )
    assert BEARING_MODEL_CATALOG.default_model_id == "distilled_h5"
    assert VALID_MODEL_TYPES == tuple(BEARING_MODEL_CATALOG.models)
    for model_id, descriptor in BEARING_MODEL_CATALOG.models.items():
        assert MODEL_TYPE_SPECS[model_id] == ModelTypeSpec(
            family=descriptor.family,
            default_version=descriptor.default_version,
            description=descriptor.description,
        )


def test_bearing_provider_owns_the_bearing_model_catalog() -> None:
    assert BearingModelUpdateProvider().model_catalog() is BEARING_MODEL_CATALOG


def test_legacy_model_type_spec_remains_constructible() -> None:
    assert ModelTypeSpec("edge", "v1", "test model").default_version == "v1"
