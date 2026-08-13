# Local Qwen Cloud Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the user's local Qwen3.5-2B model as an OpenAI-compatible vLLM service and make the cloud inference APIs use it when `CLOUD_BACKEND=vllm`.

**Architecture:** Keep model weights and vLLM lifecycle under `/home/jason/cloud_local_service`; the maintenance repository only consumes the stable OpenAI-compatible HTTP interface. `mock` retains deterministic tests, while `vllm` returns validated structured model output and does not silently fall back to rule results.

**Tech Stack:** Python, FastAPI, requests, vLLM, conda `local_vllm`, pytest.

## Global Constraints

- Model weights remain outside Git at `/home/jason/aigc_project/Qwen3.5-2B/Qwen/Qwen3___5-2B`.
- Bind vLLM to `127.0.0.1:6006`; do not expose the model service publicly.
- Preserve the existing `/cloud/infer` response contracts for both V0.1 and bearing packet requests.
- `mock` remains the default backend; `vllm` failures surface as `CloudServiceError` HTTP errors.

---

### Task 1: Local model-service lifecycle

**Files:**
- Create: `/home/jason/cloud_local_service/scripts/start_vllm.sh`
- Create: `/home/jason/cloud_local_service/scripts/smoke_test.py`
- Create: `/home/jason/cloud_local_service/.env.example`

**Interfaces:**
- Produces: `GET /v1/models` and `POST /v1/chat/completions` at `http://127.0.0.1:6006`.
- Consumes: `MODEL_PATH`, `MODEL_NAME`, `VLLM_PORT` environment variables.

- [ ] **Step 1: Create a smoke test that requires model discovery and a JSON chat completion.**
- [ ] **Step 2: Start the service with `conda run -n local_vllm vllm serve "$MODEL_PATH" --host 127.0.0.1 --port "$VLLM_PORT" --served-model-name "$MODEL_NAME"`.**
- [ ] **Step 3: Run the smoke test against the real service and retain its output as runtime evidence.**

### Task 2: Bearing packet vLLM routing

**Files:**
- Modify: `cloud_edge_project/cloud_service/service.py`
- Test: `cloud_edge_project/cloud_service/tests/test_packet_review.py`

**Interfaces:**
- Consumes: `CloudSettings.backend`, `CloudSettings.vllm_*`, and `infer_vllm(perception_result, settings)`.
- Produces: Existing `cloud_packet_result`, with `cloud_model_version` set to the served model name for a vLLM result.

- [ ] **Step 1: Write a failing test that sets backend `vllm`, stubs only the external HTTP boundary, and asserts a validated vLLM result is persisted in the unchanged packet-review envelope.**
- [ ] **Step 2: Run that test and verify it fails because `infer_cloud` still selects `RuleBasedDiagnosisModel`.**
- [ ] **Step 3: Implement the smallest backend branch: injected diagnosis models win; `mock` uses rules; `vllm` calls `infer_vllm`; invalid backend raises `CloudServiceError`.**
- [ ] **Step 4: Run packet-review tests and commit the independently verified behavior.**

### Task 3: V0.1 cloud-request vLLM routing

**Files:**
- Modify: `cloud_edge_project/cloud_service/prompt.py`
- Modify: `cloud_edge_project/cloud_service/vllm_backend.py`
- Modify: `cloud_edge_project/cloud_service/app.py`
- Test: `cloud_edge_project/tests/test_v01_inference.py`

**Interfaces:**
- Produces: the existing V0.1 result fields `task_id`, `node_id`, `model_name`, `label`, `confidence`, `risk_level`, `cloud_latency_ms`, and `decision`.
- Maps `record_only` to `ignore` and escalation recommendations to `send_alert`.

- [ ] **Step 1: Write a failing V0.1 test proving `CLOUD_BACKEND=vllm` calls the model adapter and returns the served model name rather than `cloud_full_model`.**
- [ ] **Step 2: Run it and verify the current fixed rule implementation fails the new assertion.**
- [ ] **Step 3: Add a compact V0.1 JSON prompt and a validated vLLM adapter; make `infer_cloud_v01` select it only for backend `vllm`.**
- [ ] **Step 4: Run V0.1 tests plus cloud-service tests and commit the verified routing.**

### Task 4: Local end-to-end verification and handoff

**Files:**
- Create: `cloud_edge_project/docs/local-qwen-cloud.md`
- Test: existing cloud-service and project suites.

- [ ] **Step 1: Configure the cloud service for the local endpoint and run a real `/cloud/infer` request against the loaded model.**
- [ ] **Step 2: Verify the response is valid JSON, contains the served model name, and has valid contract fields.**
- [ ] **Step 3: Run `python -m pytest cloud_edge_project -q` and `git diff --check`.**
- [ ] **Step 4: Commit on the feature branch, merge into local `main`, run final tests, then push `main` to `origin`.**
