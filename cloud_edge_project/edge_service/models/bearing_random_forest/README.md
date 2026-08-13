# Runtime bearing classifier

`random_forest.joblib` is the real packet-level binary classifier loaded by the
edge runtime. Its integrity and feature order are fixed by `model_manifest.json`,
`feature_schema.json`, `label_mapping.json`, and `SHA256SUMS`.

This model is intentionally marked `evaluation_only`: bearing-isolated
cross-validation did not meet the recorded deployment gate. It is committed to
replace the deterministic mock in the integration path, not as evidence of
production accuracy.

The Paderborn source dataset and generated feature tables are not part of this
directory and must not be committed to Git.
