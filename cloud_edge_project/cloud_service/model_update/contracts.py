"""Shared constants for cloud update-task state transitions."""

MIN_VALIDATION_SAMPLE_COUNT = 10
MIN_AGREEMENT_IMPROVEMENT = 0.05

CREATABLE_UPDATE_TYPES = {"rule", "model"}
VALIDATION_STATES = {"created", "waiting_validation_data"}
CONFIRMATION_STATES = {"waiting_confirmation"}
DOWNLOAD_ERROR = "UPDATE_FILE_CHANGED"
