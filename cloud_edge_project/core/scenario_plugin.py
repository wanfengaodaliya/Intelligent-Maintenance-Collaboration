"""Small, optional capability protocols for scenario plugins."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from core.scenario_contracts import (
    ScenarioDecision,
    ScenarioDiagnosis,
)
from core.consistency_engine import ConsistencyPolicy
from core.arbitration_contracts import ScenarioArbitrationAdapter as ArbitrationPolicy


INPUT_ADAPTER = "input_adapter"
EDGE_INFERENCE = "edge_inference"
CLOUD_DIAGNOSIS = "cloud_diagnosis"
DECISION_POLICY = "decision_policy"
CONSISTENCY_POLICY = "consistency_policy"
ARBITRATION_POLICY = "arbitration_policy"
GLOBAL_ANALYSIS = "global_analysis"
MODEL_PROVIDER = "model_provider"
MODEL_UPDATE = "model_update"
STORAGE_PROVIDER = "storage_provider"


@dataclass(frozen=True)
class ScenarioManifest:
    scenario_id: str
    version: str
    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, str) or not self.scenario_id.strip():
            raise ValueError("scenario_id must not be empty")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("version must not be empty")
        if not self.capabilities or any(
            not isinstance(item, str) or not item.strip()
            for item in self.capabilities
        ):
            raise ValueError("capabilities must contain non-empty values")


@dataclass(frozen=True)
class CapabilityBinding:
    """A declared capability and its optional executable provider."""

    capability: str
    provider: object | None = None
    implementation_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capability, str) or not self.capability.strip():
            raise ValueError("capability must not be empty")
        if self.provider is None and not self.implementation_ref:
            raise ValueError("an unresolved capability needs an implementation_ref")

    @property
    def resolved(self) -> bool:
        return self.provider is not None


@dataclass(frozen=True)
class EdgeInferenceRuntimeRequest:
    model_root: Path
    bundled_model_root: Path
    pinned_model_version: str | None
    observation_window_ms: int
    lifecycle_enabled: bool

    def __post_init__(self) -> None:
        if self.observation_window_ms <= 0:
            raise ValueError("observation_window_ms must be positive")


@dataclass(frozen=True)
class EdgeInferenceMetadata:
    backend_id: str
    default_model_version: str
    feature_extractor_version: str
    deployment_status: str


class EdgeReadiness(Protocol):
    ok: bool
    model_version: str | None
    version_mismatch: bool
    detail: str


@runtime_checkable
class EdgeModelRuntimeClient(Protocol):
    cfg: object

    def readiness(self) -> EdgeReadiness: ...

    def infer_task(self, task: object, **kwargs: Any) -> object: ...

    def activate_version(self, target_version: str) -> Mapping[str, str]: ...


@dataclass(frozen=True)
class EdgeInferenceRuntime:
    pipeline_backend: str
    model_client: EdgeModelRuntimeClient
    evidence_builder: Callable[[dict[str, Any]], dict[str, Any]]


class InputAdapterProvider(Protocol):
    scenario_id: str

    def build_adapter(
        self,
        state_dir: Path,
        source_mapping_store: object | None = None,
    ) -> object: ...


class EdgeInferenceProvider(Protocol):
    @property
    def metadata(self) -> EdgeInferenceMetadata: ...

    def build_runtime(
        self,
        request: EdgeInferenceRuntimeRequest,
    ) -> EdgeInferenceRuntime: ...

    def infer_compatible(self, payload: Any) -> dict[str, Any]: ...


class CloudDiagnosisProvider(Protocol):
    scenario_id: str

    def build_handler(self, database_path: Path) -> "CloudScenarioHandler": ...


class CloudScenarioHandler(Protocol):
    def infer(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def arbitrate_device_conflict(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    def get_device_arbitration(
        self,
        conflict_id: str,
    ) -> dict[str, Any] | None: ...


class DecisionPolicy(Protocol):
    def decide(self, diagnosis: ScenarioDiagnosis) -> ScenarioDecision: ...


class GlobalAnalysisProvider(Protocol):
    scenario_id: str

    def build_analyzers(self) -> Mapping[str, Callable[..., Any]]: ...


class ModelProvider(Protocol):
    def model_metadata(self) -> Mapping[str, Any]: ...


class ModelUpdateRuntime(Protocol):
    def create(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def validate(self, update_id: str, test_results: list[Any]) -> dict[str, Any]: ...
    def prepare_data(self, update_id: str, **kwargs: Any) -> dict[str, Any]: ...
    def register_training_result(self, update_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def start_training(self, update_id: str) -> dict[str, Any]: ...
    def run_training(self, update_id: str) -> dict[str, Any]: ...
    def list_pending_distribution(self, **kwargs: Any) -> dict[str, Any]: ...
    def get(self, update_id: str) -> dict[str, Any]: ...
    def generate_suggestion(self, update_id: str) -> dict[str, Any]: ...
    def approve(self, update_id: str, **kwargs: Any) -> dict[str, Any]: ...
    def reject(self, update_id: str, **kwargs: Any) -> dict[str, Any]: ...
    def handoff_distribution(self, update_id: str, **kwargs: Any) -> dict[str, Any]: ...
    def list_pending_human_confirmation(self, update_id: str) -> dict[str, Any]: ...
    def record_distribution_result(self, update_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def record_rollback_result(self, update_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def post_validate(self, update_id: str, analysis_id: str) -> dict[str, Any]: ...
    def request_rollback(self, update_id: str, **kwargs: Any) -> dict[str, Any]: ...
    def execute_rollback(self, update_id: str, **kwargs: Any) -> dict[str, Any]: ...
    def get_download_artifact(self, update_id: str) -> dict[str, Any]: ...


class ModelUpdateProvider(Protocol):
    scenario_id: str

    def build_service(self, settings: object) -> ModelUpdateRuntime: ...

    def activate_candidate(
        self,
        settings: object,
        artifact: Path,
        version: str,
    ) -> object: ...

    def activate_version(self, settings: object, version: str) -> object: ...


@runtime_checkable
class StorageRegistrar(Protocol):
    def execute_schema(self, script: str) -> None: ...


@runtime_checkable
class StorageProvider(Protocol):
    scenario_id: str

    def initialize(self, registrar: StorageRegistrar) -> None: ...


class ScenarioPlugin(Protocol):
    @property
    def manifest(self) -> ScenarioManifest: ...

    @property
    def capabilities(self) -> Mapping[str, CapabilityBinding]: ...

    def validate_configuration(self) -> None: ...
