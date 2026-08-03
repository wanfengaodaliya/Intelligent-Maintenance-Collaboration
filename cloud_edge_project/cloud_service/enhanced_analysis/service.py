"""Idempotent orchestration entry point for enhanced analysis."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .bearing_matcher import match_bearing_frequencies
from .config import AnalysisConfig, DEFAULT_ANALYSIS_CONFIG
from .contracts import (
    EnhancedAnalysisError,
    EnhancedAnalysisResult,
    limitation,
)
from .diagnosis_model import DiagnosisFeatures, DiagnosisModel, RuleBasedDiagnosisAdapter
from .envelope import analyze_envelope_spectrum
from .evidence_builder import build_enhanced_analysis_result
from .history_baseline import analyze_history
from .loader import EnhancedAnalysisLoader
from .preprocessing import WindowPreprocessor
from .repositories import EnhancedAnalysisRepository
from .spectrum import analyze_spectrum
from .time_domain import analyze_time_domain
from .time_frequency import analyze_time_frequency


LOGGER = logging.getLogger(__name__)


class EnhancedAnalysisService:
    def __init__(
        self,
        database_path: Path,
        *,
        config: AnalysisConfig = DEFAULT_ANALYSIS_CONFIG,
        model: DiagnosisModel | None = None,
        repository: EnhancedAnalysisRepository | None = None,
        loader: EnhancedAnalysisLoader | None = None,
        clock=time.time_ns,
    ):
        self.database_path = Path(database_path)
        self.config = config
        self.model = model or RuleBasedDiagnosisAdapter()
        self.repository = repository or EnhancedAnalysisRepository(self.database_path)
        self.loader = loader or EnhancedAnalysisLoader(self.database_path)
        self.clock = clock
        self.preprocessor = WindowPreprocessor(config)

    def analyze(self, review_id: str) -> EnhancedAnalysisResult:
        if not review_id or not isinstance(review_id, str):
            raise EnhancedAnalysisError("REVIEW_NOT_FOUND", "review_id is required", retryable=False)
        if not self.repository.review_exists(review_id):
            raise EnhancedAnalysisError("REVIEW_NOT_FOUND", "review does not exist", retryable=False)

        aggregation = self.repository.find_aggregation(review_id)
        if aggregation is None:
            raise EnhancedAnalysisError(
                "AGGREGATION_RESULT_NOT_FOUND", "succeeded aggregation result does not exist", retryable=True
            )
        if (
            aggregation["aggregation_status"] != "succeeded"
            or aggregation["context_status"] not in {"complete", "partial_context"}
        ):
            raise EnhancedAnalysisError(
                "ANALYSIS_NOT_ELIGIBLE", "aggregation context is not eligible for enhanced analysis",
                retryable=False,
            )

        existing = self.repository.get_succeeded(review_id)
        if existing:
            return self.repository.result_from_row(existing)
        row, owns_run = self.repository.start(review_id, aggregation, self.config)
        if not owns_run:
            if row["status"] == "succeeded":
                return self.repository.result_from_row(row)
            raise EnhancedAnalysisError(
                "ANALYSIS_IN_PROGRESS", "enhanced analysis is already running", retryable=True
            )

        started_at = self.clock()
        try:
            window, context = self.loader.load(aggregation, self.config)
            prepared = self.preprocessor.prepare(window)
            time_domain_evidence = analyze_time_domain(
                prepared,
                context.start_timestamp_ns,
                context.packet_start_samples,
                context.sample_rate_hz,
            )
            spectrum_evidence = analyze_spectrum(
                prepared.x2["vibration"], context.sample_rate_hz, self.config
            )
            envelope_evidence = analyze_envelope_spectrum(
                prepared.x2["vibration"],
                context.sample_rate_hz,
                context.bearing,
                self.config,
            )
            time_frequency_evidence = analyze_time_frequency(
                prepared.x0["vibration"], context.sample_rate_hz, self.config
            )
            quality_good = not any(
                item["severity"] != "info" for item in prepared.limitations
            )
            bearing_evidence = match_bearing_frequencies(
                envelope_peaks=envelope_evidence["peaks"],
                spectrum_peaks=spectrum_evidence["peaks"],
                frequency_resolution_hz=context.frequency_resolution_hz,
                speed_rpm=context.speed_rpm,
                bearing=context.bearing,
                context_status=context.context_status,
                config=self.config,
                quality_good=quality_good,
            )
            history_evidence = analyze_history(
                edge_rows=self.repository.edge_history(
                    context.sender_id, started_at, self.config.history_lookback_days
                ),
                enhanced_rows=self.repository.enhanced_history(review_id, context.sender_id),
                current_speed_rpm=context.speed_rpm,
                current_radial_load_n=context.radial_load_n,
                current_time_domain=time_domain_evidence,
                config=self.config,
            )
            model_evidence, model_limitation = self._predict_model(
                time_domain_evidence,
                spectrum_evidence,
                time_frequency_evidence,
                history_evidence,
                bearing_evidence,
                context,
            )
            result = build_enhanced_analysis_result(
                context=context,
                config=self.config,
                time_domain_evidence=time_domain_evidence,
                spectrum_evidence=spectrum_evidence,
                envelope_evidence=envelope_evidence,
                time_frequency_evidence=time_frequency_evidence,
                bearing_evidence=bearing_evidence,
                history_evidence=history_evidence,
                model_evidence=model_evidence,
                extra_limitations=prepared.limitations + model_limitation,
                created_at_ns=started_at,
            )
            self.repository.complete(result)
            LOGGER.info(
                "enhanced_analysis succeeded review_id=%s attempt_count=%s stage=succeeded",
                review_id,
                row["attempt_count"],
            )
            return result
        except EnhancedAnalysisError as error:
            self.repository.fail(review_id, error.code, error.detail, retryable=error.retryable)
            raise
        except Exception as error:
            self.repository.fail(
                review_id,
                "ENHANCED_ANALYSIS_INTERNAL_ERROR",
                "unexpected enhanced-analysis failure",
                retryable=True,
            )
            raise

    def _predict_model(
        self,
        time_domain_evidence,
        spectrum_evidence,
        time_frequency_evidence,
        history_evidence,
        bearing_evidence,
        context,
    ):
        features = DiagnosisFeatures(
            numeric_features={
                "vibration_rms": _optional(time_domain_evidence["vibration"].get("rms")),
                "vibration_kurtosis": _optional(time_domain_evidence["vibration"].get("kurtosis")),
                "vibration_crest_factor": _optional(time_domain_evidence["vibration"].get("crest_factor")),
                "phase_current_1_rms_a": _optional(time_domain_evidence["phase_current_1_A"].get("rms")),
                "phase_current_2_rms_a": _optional(time_domain_evidence["phase_current_2_A"].get("rms")),
                "nonstationarity_score": time_frequency_evidence["nonstationarity_score"],
                "history_deviation": _optional(history_evidence.get("baseline_deviation_score")) or 0.0,
            },
            operating_conditions={
                "speed_rpm": context.speed_rpm,
                "radial_load_n": context.radial_load_n,
            },
            quality_codes=[item["code"] for item in context.limitations],
            bearing_match_scores={
                item["hypothesis"]: item["score"] for item in bearing_evidence["evidence"]
            },
        )
        try:
            return self.model.predict(features), []
        except Exception as error:
            LOGGER.warning("diagnosis model unavailable: %s", error)
            return (
                {
                    "status": "unavailable",
                    "label": None,
                    "label_probabilities": {},
                    "probability": None,
                    "uncertainty": None,
                    "model_version": getattr(self.model, "model_version", None),
                    "feature_contributions": [],
                },
                [limitation("diagnosis_model_unavailable", "diagnosis model is unavailable")],
            )


def _optional(value):
    return 0.0 if value is None else float(value)
