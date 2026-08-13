# Runtime bearing classifier

`random_forest.joblib` is the real packet-level binary classifier loaded by the
edge runtime. Its integrity and feature order are fixed by `model_manifest.json`,
`feature_schema.json`, `label_mapping.json`, and `SHA256SUMS`.

## Edge integration

The normal runtime path is in-process: `EdgeModelPipeline` invokes
`RandomForestDiagnosticModel`, which loads this `joblib` artifact and calls
`RandomForestClassifier.predict_proba`. It is not served by a separate HTTP
model process. Raw packet ingestion uses `/edge/packets`; `/edge/rf/infer` is
the synchronous diagnostic endpoint for a complete `edge-model-input/1.1`
`PerceptionResult`.

The model consumes the 27 `float64` features in the exact order recorded in
`feature_schema.json` (`bearing-rf-features/1.0`). No scaler or other runtime
normalization artifact is used. The classifier emits `normal` or `fault`; the
public RF endpoint maps `fault` to `abnormal`, takes confidence from the
largest `predict_proba` value, and maps risk to `low` (`normal`) or `high`
(`fault`).

`/health` and `/edge/rf/infer` expose the runtime feature extractor version
(`edge-perception-v1`), feature schema version, and model input schema version
for traceability. The V0.1 `/edge/infer` task contract remains the legacy
four-sensor interface; it does not contain enough information to manufacture
the RF's 27 features.

This model is intentionally marked `evaluation_only`: bearing-isolated
cross-validation did not meet the recorded deployment gate. It is committed to
replace the deterministic mock in the integration path, not as evidence of
production accuracy.

The Paderborn source dataset and generated feature tables are not part of this
directory and must not be committed to Git.
