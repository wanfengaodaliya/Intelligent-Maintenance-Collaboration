# Edge RF Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the committed random-forest model load reliably, serve both packet and synchronous inference paths, and publish traceable public results.

**Architecture:** Keep `RandomForestDiagnosticModel` in the edge Python process. Preserve its `normal`/`fault` internal result contract, then add a single explicit adapter for public V0.1 responses that maps `fault` to `abnormal`. Bind the runtime perception and RF schema versions into completed packet output without changing the model algorithm or its `evaluation_only` status.

**Tech Stack:** Python 3.12, FastAPI, scikit-learn/joblib, pytest.

## Global Constraints

- Keep the model algorithm as `RandomForestClassifier`; do not retrain or change the `evaluation_only` deployment state.
- Use the committed 27-feature `bearing-rf-features/1.0` order exactly.
- The public V0.1 label remains `normal|abnormal`; RF internal output remains `normal|fault`.
- Every behavioral change follows RED → GREEN and is covered by an automated test.

---

### Task 1: Make the committed RF artifact loadable on Windows and Linux

**Files:**
- Modify: `.gitattributes`
- Modify: `cloud_edge_project/edge_service/models/bearing_random_forest/model_manifest.json`
- Modify: `cloud_edge_project/edge_service/models/bearing_random_forest/SHA256SUMS`
- Test: `cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py`

**Interfaces:**
- Consumes: model manifest `label_mapping_sha256` and runtime `RandomForestDiagnosticModel` SHA-256 validation.
- Produces: an artifact package whose committed model verification test loads successfully after checkout.

- [ ] **Step 1: Run the existing artifact-load test as RED**

Run: `python -m pytest cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py -q`

Expected: failure with `label mapping checksum mismatch`.

- [ ] **Step 2: Pin model-artifact text files to a fixed Git checkout representation and align their checksums**

Add Git attributes for the model package JSON/checksum files and set the manifest and `SHA256SUMS` `label_mapping.json` hash to the checked-in artifact byte digest.

- [ ] **Step 3: Verify GREEN**

Run: `python -m pytest cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py -q`

Expected: all tests pass.

### Task 2: Route synchronous edge inference through the real RF and adapt its public label

**Files:**
- Modify: `cloud_edge_project/edge_service/app.py`
- Modify: `cloud_edge_project/edge_service/src/edge_diagnosis/random_forest_model.py` only if a small public inference adapter is required
- Test: `cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py`

**Interfaces:**
- Consumes: V0.1 `/edge/infer` payload and `RandomForestDiagnosticModel.run(PacketInferenceTask)`.
- Produces: V0.1-compatible `label`, `confidence`, `risk_level`, `need_cloud`, and RF model version.

- [ ] **Step 1: Add a failing HTTP test using a real V0.1 request**

Assert `POST /edge/infer` returns `model_version == "bearing-rf-a2-evaluation-v1"` and maps RF `fault` to public `abnormal`.

- [ ] **Step 2: Run the test as RED**

Run: `python -m pytest cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py -q`

Expected: failure because `/edge/infer` still calls the legacy synchronous model.

- [ ] **Step 3: Implement the smallest shared RF-to-public adapter**

Build a packet task from a validated V0.1 payload, invoke the RF model, map `fault` to `abnormal`, retain `normal`, copy confidence/risk level/model version, and derive `need_cloud` from the existing confidence policy.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py cloud_edge_project/edge_service/tests/unit/test_task_http.py -q`

Expected: all tests pass.

### Task 3: Carry feature-version provenance alongside RF packet results

**Files:**
- Modify: `cloud_edge_project/edge_service/src/edge_model/contracts.py`
- Modify: `cloud_edge_project/edge_service/src/edge_model/pipeline.py`
- Modify: `cloud_edge_project/edge_service/src/edge_runtime/coordinator.py`
- Test: `cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py`

**Interfaces:**
- Consumes: runtime `PerceptionConfig.feature_extractor_version`, RF feature schema version, and completed `PacketResult`.
- Produces: packet output with `feature_extractor_version`, `feature_schema_version`, and `model_input_schema_version` while preserving the existing RF fields.

- [ ] **Step 1: Add a failing local-pipeline provenance test**

Assert a completed local RF packet result includes `edge-perception-v1`, `bearing-rf-features/1.0`, and `edge-model-input/1.1`.

- [ ] **Step 2: Run the test as RED**

Run: `python -m pytest cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py -q`

Expected: failure because packet output currently has no feature-version fields.

- [ ] **Step 3: Add immutable provenance fields at the `EdgeResult` boundary**

Load schema versions with the RF artifact, place them on `EdgeResult`, and preserve them through `PacketResult` and task-completion persistence.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py -q`

Expected: all tests pass.

### Task 4: Preserve HTTP-mode regression coverage and remove optional performance imports from collection

**Files:**
- Modify: `cloud_edge_project/edge_service/tests/unit/test_edge_model.py`
- Modify: `cloud_edge_project/edge_service/tests/performance/functional_test.py`
- Test: `cloud_edge_project/edge_service/tests/unit/test_edge_model.py`

**Interfaces:**
- Consumes: explicit `EdgeModelConfig(diagnostic_backend="http")` test harness configuration.
- Produces: HTTP queue/circuit-breaker tests independent of the RF local default, and a manually invoked performance script that does not break pytest collection without PyTorch.

- [ ] **Step 1: Run existing edge tests as RED**

Run: `python -m pytest cloud_edge_project/edge_service -q`

Expected: collection fails on optional `torch`; excluding it reveals HTTP tests use the changed local default.

- [ ] **Step 2: Make test intent explicit and defer optional imports**

Set the legacy fake-client harness to `diagnostic_backend="http"`; move PyTorch/Transformers imports into the performance script entrypoint and declare it non-test code.

- [ ] **Step 3: Verify GREEN**

Run: `python -m pytest cloud_edge_project/edge_service -q`

Expected: all edge-service tests pass without installing PyTorch.

### Task 5: Run full regression, commit, integrate, and push

**Files:**
- Verify all modified files and relevant project tests.

- [ ] **Step 1: Run formatting and full-project verification**

Run: `git diff --check` and `python -m pytest cloud_edge_project -q`.

- [ ] **Step 2: Commit the focused change**

Run: `git add <focused files>` then `git commit -m "fix: complete edge random forest integration"`.

- [ ] **Step 3: Merge into local `main`, re-run the full verification, and push `main`**

Run the same full verification after merge, then `git push origin main`.
