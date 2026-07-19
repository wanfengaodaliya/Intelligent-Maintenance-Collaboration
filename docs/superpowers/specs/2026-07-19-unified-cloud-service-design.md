# Unified Cloud Service Design

## Goal

Maintain one `cloud_service` codebase that runs without a GPU in local development and uses the real Qwen model on AutoDL. The scheduler keeps one request contract and changes only the cloud-service URL between environments.

## Scope

This change upgrades the existing `cloud_edge_project/cloud_service` implementation. It does not create a second independent server application, change the scheduler's routing policy, modify the edge model, upload model weights, or add model fine-tuning.

## Architecture

The FastAPI application continues to expose `POST /cloud/infer` and `GET /health`. A service layer selects exactly one inference backend from the `CLOUD_BACKEND` environment variable:

- `mock`, the default, produces deterministic local results without vLLM or a GPU.
- `vllm` sends an OpenAI-compatible chat-completions request to the vLLM service and converts its JSON response into the existing project `CloudResult` contract.

The same source tree is developed locally, committed to GitHub, and pulled onto AutoDL. Runtime configuration, not different source code, selects the backend.

```text
Local scheduler -> local /cloud/infer -> mock backend

Team scheduler -> AutoDL /cloud/infer -> vLLM backend
                                      -> 127.0.0.1:6006
                                      -> Qwen3-14B-AWQ
```

## Files and Responsibilities

```text
cloud_edge_project/
├── cloud_service/
│   ├── app.py              FastAPI routes and HTTP error mapping
│   ├── model.py            Stable inference facade used by app.py
│   ├── service.py          Backend selection and CloudResult assembly
│   ├── mock_backend.py     Deterministic local inference
│   ├── vllm_backend.py     HTTP client and vLLM response parsing
│   └── prompt.py           Cloud-review system prompt and input construction
├── scripts/
│   ├── start_vllm.sh       AutoDL vLLM and Qwen startup
│   └── start_cloud_service.sh  AutoDL FastAPI startup with vLLM selected
├── configs/
│   └── local.yaml          Existing local service defaults
└── tests/
    └── test_cloud_service.py
```

`model.py` remains as a compatibility facade so imports from the existing `app.py` and other project modules do not break. Backend-specific behavior moves into focused modules.

## Public Interface

The request remains the existing `CloudRequest` shape from `docs/api.md`:

```json
{
  "packet": {
    "packet_id": "batch_000001",
    "device_id": "K001",
    "sensor_id": "sensor_K001",
    "sequence_number": 1,
    "start_timestamp_ns": 1781920800000000000,
    "end_timestamp_ns": 1781920800050000000,
    "duration_ms": 50,
    "data": {
      "data_type": "bearing_timeseries",
      "vibration_sample_rate_hz": 16000,
      "vibration_sample_count": 800,
      "vibration": [],
      "current": 1.34,
      "temperature": 45.8,
      "speed": 899.7,
      "load": 0.7
    }
  },
  "edge_result": {
    "label": "abnormal",
    "confidence": 0.72,
    "risk_level": "medium"
  }
}
```

Formal validation continues to use `common.schemas.validate_cloud_request`; production requests contain 800 numeric vibration samples as required by the existing contract.

Both backends return the existing `CloudResult` shape:

```json
{
  "packet_id": "batch_000001",
  "device_id": "K001",
  "cloud_node_id": "cloud_1",
  "model_name": "qwen-cloud",
  "label": "abnormal",
  "confidence": 0.93,
  "risk_level": "high",
  "cloud_latency_ms": 852.4,
  "decision": {
    "action": "send_alert",
    "description": "The cloud review confirms an abnormal bearing state."
  }
}
```

Allowed model-produced values are restricted to the contract:

- `label`: `normal` or `abnormal`
- `risk_level`: `low`, `medium`, or `high`
- `decision.action`: `none`, `record_only`, `send_alert`, or `stop_machine_check`
- `confidence`: numeric value from 0 through 1

## Prompt and Model Input

The system prompt defines the model as the cloud reviewer in an edge-cloud intelligent maintenance system. It requires a single JSON object with `label`, `confidence`, `risk_level`, `action`, and `description`, forbids invented measurements, and instructs the model to use conservative decisions when information is insufficient.

The user message is serialized with `json.dumps(..., ensure_ascii=False)`. It includes packet identity, scalar sensor measurements, edge-model output, and compact vibration statistics derived from the 800 samples. The raw 800-value waveform is not embedded in the language-model prompt because it consumes context without giving a text model a reliable signal-processing representation.

The vibration summary contains count, minimum, maximum, mean, root-mean-square value, and peak absolute value. The original request still undergoes full contract validation before the summary is produced.

## Configuration

Runtime settings use environment variables with safe local defaults:

| Variable | Default | Meaning |
|---|---|---|
| `CLOUD_BACKEND` | `mock` | Selected backend: `mock` or `vllm` |
| `VLLM_URL` | `http://127.0.0.1:6006/v1/chat/completions` | Internal vLLM endpoint |
| `VLLM_MODEL_NAME` | `qwen-cloud` | Name configured by `--served-model-name` |
| `VLLM_API_KEY` | empty | Optional bearer token; never committed |
| `VLLM_TIMEOUT_SECONDS` | `120` | Request timeout in seconds |

An unsupported `CLOUD_BACKEND` value fails clearly instead of silently falling back to mock.

The scheduler uses a separate environment variable, `CLOUD_SERVICE_URL`, to select the service instance:

- Local development: `http://127.0.0.1:8004`
- Real integration: the AutoDL public mapping for port 6008

The scheduler does not inspect GPU availability and does not choose the inference backend.

## Startup and Deployment

`start_vllm.sh` activates the `cloud_llm` conda environment and starts Qwen3-14B-AWQ through vLLM on port 6006.

`start_cloud_service.sh` activates the same environment, exports `CLOUD_BACKEND=vllm`, enters the checked-out project directory, and starts the FastAPI service on port 6008. Other vLLM settings rely on the code defaults unless the operator overrides them explicitly.

Startup order on AutoDL is:

1. Run `start_vllm.sh` and wait for the vLLM health endpoint to respond.
2. Run `start_cloud_service.sh`.
3. Test `GET /health` and `POST /cloud/infer` through port 6008.

Model weights remain at `/root/autodl-tmp/models/Qwen3-14B-AWQ` and are never committed. AutoDL pulls the application source from GitHub instead of maintaining a separate handwritten copy.

## Error Handling

Existing request-contract errors remain HTTP 400 with the shared error response. Additional failures map as follows:

- Unsupported backend configuration: HTTP 500, `MODEL_INFER_FAILED`
- vLLM connection failure or timeout: HTTP 503, `CLOUD_UNAVAILABLE`
- Non-success vLLM HTTP response: HTTP 503, `CLOUD_UNAVAILABLE`
- Empty, malformed, fenced, or contract-invalid model JSON: HTTP 502, `MODEL_INFER_FAILED`

Errors preserve `packet_id` whenever it can be extracted. Secrets and full model responses are not included in client-facing error messages.

`GET /health` reports the selected backend. In mock mode it reports ready immediately. In vLLM mode it checks the configured vLLM models endpoint with a short timeout and reports unavailable when the model service cannot be reached.

## Testing

Tests run locally without a GPU. They cover:

1. Default backend selection is `mock`.
2. Mock inference preserves packet and device IDs and produces a valid `CloudResult`.
3. `vllm` selection sends the expected model name, system prompt, compact sensor data, and timeout.
4. Valid model JSON is converted into the existing `CloudResult` structure.
5. Markdown-fenced JSON is rejected rather than accepted ambiguously.
6. Invalid labels, risk levels, confidence values, actions, empty responses, timeouts, and connection errors map to the defined errors.
7. An unsupported backend value fails without invoking mock.
8. FastAPI health and inference routes expose the expected status codes.

The existing demo remains runnable in default mock mode. No test requires AutoDL, vLLM, model weights, or network access.

## Success Criteria

- One committed `cloud_service` codebase runs locally and on AutoDL.
- Local startup with no backend variable uses mock and requires no GPU.
- AutoDL startup sets `CLOUD_BACKEND=vllm` and calls the local vLLM process.
- Both modes accept the same `CloudRequest` and return the same `CloudResult` contract.
- The scheduler changes only `CLOUD_SERVICE_URL` between local and real integration.
- Automated tests pass without GPU or network access.
- Model weights, credentials, `local_word`, and runtime logs remain outside Git tracking.
