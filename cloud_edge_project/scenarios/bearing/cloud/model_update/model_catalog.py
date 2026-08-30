"""Model lifecycle metadata owned by the bearing scenario."""

from core.model_lifecycle import ModelCatalog, ModelDescriptor


BEARING_MODEL_CATALOG = ModelCatalog(
    scenario_id="bearing",
    default_model_id="distilled_h5",
    models={
        "distilled_h5": ModelDescriptor(
            model_id="distilled_h5",
            family="edge",
            default_version="distilled_h5_kd_fold3_a9f20442",
            description="edge distilled H5 three-branch realtime model",
        ),
        "moment_light_adapt": ModelDescriptor(
            model_id="moment_light_adapt",
            family="cloud",
            default_version="moment-scl05-final",
            description="cloud MOMENT light-adapt review model",
        ),
    },
)
