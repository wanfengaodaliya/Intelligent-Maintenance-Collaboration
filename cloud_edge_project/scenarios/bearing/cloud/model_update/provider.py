"""Bearing model-update assembly around the existing cloud services."""

from __future__ import annotations

import os
from pathlib import Path

from cloud_service.config import CloudSettings
from cloud_service.model_update.dataset_repository import (
    LabelConfirmationRepository,
    PacketSourceRepository,
)
from cloud_service.model_update.label_confirmation import (
    CloudReferenceProvider,
    LabelConfirmationResolver,
)
from cloud_service.model_update.model_types import ActiveModelVersionStore
from cloud_service.model_update.service import ModelUpdateService
from cloud_service.model_update.training import (
    CloudMomentTrainer,
    EdgeH5Trainer,
    OfflineTrainingRunner,
    RawTrainingSampleLoader,
)
from cloud_service.service import activate_moment_candidate, activate_moment_version
from core.model_lifecycle import ModelCatalog
from scenarios.bearing.cloud.model_update.config import load_label_mapping
from scenarios.bearing.cloud.model_update.dataset_label_provider import (
    DatasetLabelProvider,
)
from scenarios.bearing.cloud.model_update.human_review_provider import (
    HumanReviewProvider,
)
from scenarios.bearing.cloud.model_update.model_catalog import BEARING_MODEL_CATALOG
from scenarios.bearing.cloud.model_update.training_data_source import (
    BearingTrainingDataSource,
)


PROJECT_ROOT = Path(__file__).resolve().parents[4]


class BearingModelUpdateProvider:
    scenario_id = "bearing"

    def model_catalog(self) -> ModelCatalog:
        return BEARING_MODEL_CATALOG

    def build_service(self, settings: CloudSettings) -> ModelUpdateService:
        source_database = os.getenv("PACKET_SOURCE_DATABASE_PATH")
        label_mapping_path = os.getenv("PADERBORN_LABEL_MAPPING_PATH")
        data_root = os.getenv("MODEL_UPDATE_DATA_ROOT")
        raw_data_root = Path(
            os.getenv("PADERBORN_DATA_ROOT", str(PROJECT_ROOT / "data" / "paderborn"))
        )
        database_path = settings.database_path
        source_path = Path(source_database) if source_database else None
        source_repository = PacketSourceRepository(source_path or database_path)
        label_repository = LabelConfirmationRepository(database_path)
        label_mapping = load_label_mapping(
            Path(label_mapping_path) if label_mapping_path else None
        )
        return ModelUpdateService(
            database_path,
            data_root=Path(data_root) if data_root else None,
            packet_source_database_path=source_path,
            training_data_source=BearingTrainingDataSource(
                database_path,
                source_repository,
            ),
            label_provider=LabelConfirmationResolver(
                [
                    DatasetLabelProvider(source_repository, label_mapping),
                    HumanReviewProvider(label_repository),
                    CloudReferenceProvider(),
                ]
            ),
            trainer=self._build_offline_trainer(
                settings,
                source_repository,
                raw_data_root,
            ),
            model_catalog=self.model_catalog(),
        )

    def _build_offline_trainer(
        self,
        settings: CloudSettings,
        source_repository: PacketSourceRepository,
        raw_data_root: Path,
    ) -> OfflineTrainingRunner:
        loader = RawTrainingSampleLoader(source_repository, raw_data_root)
        edge_model_root = Path(
            os.getenv("EDGE_MODEL_ROOT", str(PROJECT_ROOT / "edge_service" / "models"))
        )
        edge_version = (
            ActiveModelVersionStore(settings.database_path).get("distilled_h5")
            or self.model_catalog().require("distilled_h5").default_version
        )
        edge_version_dir = edge_model_root / "distilled_h5" / edge_version
        return OfflineTrainingRunner(
            edge_trainer=EdgeH5Trainer(
                loader=loader,
                baseline_checkpoint=edge_version_dir / "best_model.pt",
                baseline_dir=edge_version_dir,
            ),
            cloud_trainer=CloudMomentTrainer(
                loader=loader,
                baseline_checkpoint=settings.moment_checkpoint_path,
                pretrained_path=settings.moment_pretrained_path,
                model_module_path=settings.moment_deployment_dir / "moment_model.py",
                condition_norm_path=settings.moment_condition_norm_path,
            ),
        )

    def activate_candidate(
        self,
        settings: CloudSettings,
        artifact: Path,
        version: str,
    ) -> object:
        return activate_moment_candidate(settings, artifact, version)

    def activate_version(self, settings: CloudSettings, version: str) -> object:
        return activate_moment_version(settings, version)
