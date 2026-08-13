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

### Task 2: Serve complete PerceptionResult input through the real RF and adapt its public label

**Files:**
- Modify: `cloud_edge_project/edge_service/app.py`
- Modify: `cloud_edge_project/edge_service/src/edge_diagnosis/random_forest_model.py` only if a small public inference adapter is required
- Test: `cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py`

**Interfaces:**
- Consumes: exact `edge-model-input/1.1` input and `RandomForestDiagnosticModel.run(PacketInferenceTask)`.
- Produces: `/edge/rf/infer` output with public `normal|abnormal` label, confidence, risk level, model version, and `need_cloud=true` while evaluation-only.

- [ ] **Step 1: Add a failing HTTP test using a real PerceptionResult**

Assert `POST /edge/rf/infer` returns `model_name == "bearing-rf-a2-evaluation-v1"` and maps RF `fault` to public `abnormal`.

- [ ] **Step 2: Run the test as RED**

Run: `python -m pytest cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py -q`

Expected: failure because `/edge/rf/infer` does not exist.

- [ ] **Step 3: Implement the smallest shared RF-to-public adapter**

Build a packet task from validated model input, invoke the RF model, map `fault` to `abnormal`, retain `normal`, copy confidence/risk level/model version, and require cloud review because the artifact is evaluation-only. Keep the four-sensor V0.1 `/edge/infer` contract unchanged.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py -q`

Expected: all tests pass.

### Task 3: Expose feature-version provenance through RF diagnostics

**Files:**
- Modify: `cloud_edge_project/edge_service/app.py`
- Modify: `cloud_edge_project/edge_service/src/edge_diagnosis/random_forest_model.py`
- Test: `cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py`

**Interfaces:**
- Consumes: runtime `PerceptionConfig.feature_extractor_version`, RF feature schema version, and model input schema version.
- Produces: `/health` and `/edge/rf/infer` fields for `feature_extractor_version`, `feature_schema_version`, and `model_input_schema_version` without changing the frozen packet-routing output contract.

- [ ] **Step 1: Add a failing health provenance test**

Assert `/health` includes `edge-perception-v1`, `bearing-rf-features/1.0`, and `edge-model-input/1.1`.

- [ ] **Step 2: Run the test as RED**

Run: `python -m pytest cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py -q`

Expected: failure because the health response currently has no feature-version fields.

- [ ] **Step 3: Read immutable schema versions from the artifact and expose them at diagnostics boundaries**

Load schema versions with the RF artifact, then expose them from `/health` and `/edge/rf/infer`.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py -q`

Expected: all tests pass.

### Task 4: Declare the HTTP test dependency

**Files:**
- Modify: `cloud_edge_project/requirements-dev.txt`
- Test: `cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py`

**Interfaces:**
- Consumes: FastAPI's `TestClient`.
- Produces: a reproducible development environment for existing and new HTTP regression tests.

- [ ] **Step 1: Run the RF HTTP test as RED**

Run: `python -m pytest cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py -q`

Expected: collection fails because `TestClient` has no `httpx` implementation.

- [ ] **Step 2: Declare the supported TestClient transport dependency**

Add `httpx==0.28.1` to `requirements-dev.txt`, matching the existing repository development dependency convention.

- [ ] **Step 3: Verify GREEN**

Run: `python -m pytest cloud_edge_project/edge_service/verification/test_random_forest_diagnosis.py -q`

Expected: all RF verification tests pass with the declared development dependency.

### Task 5: Run full regression, commit, integrate, and push

**Files:**
- Verify all modified files and relevant project tests.

- [ ] **Step 1: Run formatting and full-project verification**

Run: `git diff --check` and `python -m pytest cloud_edge_project -q`.

- [ ] **Step 2: Commit the focused change**

Run: `git add <focused files>` then `git commit -m "fix: complete edge random forest integration"`.

- [ ] **Step 3: Merge into local `main`, re-run the full verification, and push `main`**

Run the same full verification after merge, then `git push origin main`.
