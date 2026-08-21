# Dual Edge Local Startup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start and register both local edge nodes while moving the optional LLM service to port 8005.

**Architecture:** `start_project.ps1` remains the only launch entry point. It starts one Scheduler, one Cloud service, two independently configured Edge processes, and optionally one LLM process. The scheduler receives both registered nodes through one JSON environment variable; each Edge receives its own node identity, proxies, MQTT identity, and writable state paths.

**Tech Stack:** PowerShell 5+, Conda, Python/Uvicorn, Docker Compose, FastAPI health endpoints.

## Global Constraints

- Preserve `edge_01` ports/topics/proxies: `8001`, `18011`, `18021`, `18042`, `edge/edge_01/input`.
- Preserve `edge_02` ports/topics/proxies: `8002`, `18051`, `18053`, `18052`, `edge/edge_02/input`.
- Move LLM from `8002` to `8005`; no process may use `8002` except `edge_02`.
- Keep H5 model assets shared read-only; separate all Edge writable state under `cloud_edge_project/data/edge_01` and `cloud_edge_project/data/edge_02`.
- Temporary verification code must be deleted and must not be committed.

---

### Task 1: Prove the launch contract before editing

**Files:**
- Create then delete: `tests/temporary_test_dual_edge_startup.ps1`
- Test: `start_project.ps1`

**Interfaces:**
- Consumes: raw `start_project.ps1` text.
- Produces: a failing exit code until the script contains two node registrations, the second Edge command, and LLM port `8005`.

- [ ] **Step 1: Write the failing test**

```powershell
$script = Get-Content "$PSScriptRoot/../start_project.ps1" -Raw
@('edge_02','http://127.0.0.1:18052','edge/edge_02/input','--port 8002','--port 8005') |
    ForEach-Object { if ($script -notlike "*$_*") { throw "Missing $_" } }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `powershell -ExecutionPolicy Bypass -File tests/temporary_test_dual_edge_startup.ps1`

Expected: failure reporting missing `edge_02` or `--port 8005`.

### Task 2: Add the second edge and change the LLM port

**Files:**
- Modify: `start_project.ps1`

**Interfaces:**
- Consumes: network simulator proxy contract in `cloud_edge_project/edge_service/compose.network-sim.yml`.
- Produces: two Edge Uvicorn processes and a Scheduler configuration with `edge_01` and `edge_02`.

- [ ] **Step 1: Expand Scheduler registration**

```powershell
$schedulerNodesJson = '{"edge_01":{"control_url":"http://127.0.0.1:18042","target_topic":"edge/edge_01/input"},"edge_02":{"control_url":"http://127.0.0.1:18052","target_topic":"edge/edge_02/input"}}'
```

- [ ] **Step 2: Start edge_02 with isolated writable state**

```powershell
$edge02Data = Join-Path $CloudEdge "data\\edge_02"
$edge02Cmd = "Set-Location '$CloudEdge'; `$env:EDGE_NODE_ID='edge_02'; `$env:EDGE_MQTT_CLIENT_ID='edge_02-runtime'; `$env:EDGE_MQTT_INPUT_TOPIC='edge/edge_02/input'; `$env:SCHEDULER_SERVICE_BASE_URL='http://127.0.0.1:18051'; `$env:CLOUD_SERVICE_BASE_URL='http://127.0.0.1:18053'; `$env:EDGE_V12_DATABASE_PATH='$edge02Data\\edge_v12.db'; conda activate moment; python edge_service/run_edge_service.py --host 127.0.0.1 --port 8002"
```

- [ ] **Step 3: Move LLM and both Edge LLM clients to port 8005**

```powershell
$llmCmd = "Set-Location '$LLM_DIR'; .\\llama-server.exe --model .\\models\\qwen2.5-0.5b-instruct-q3_k_m.gguf --host 127.0.0.1 --port 8005 --ctx-size 2048 --n-gpu-layers 99"
```

- [ ] **Step 4: Add health checks for edge_02 and LLM 8005**

```powershell
Check-Svc "Edge(8002)" "http://127.0.0.1:8002/health" { param($r) $r.status -eq "ok" -and $r.node_id -eq "edge_02" -and $r.mqtt_connected -eq $true }
Check-Svc "LLM(8005)" "http://127.0.0.1:8005/v1/models" { param($r) $r.data.Count -gt 0 }
```

- [ ] **Step 5: Run verification, remove the temporary test, and commit**

Run: `powershell -ExecutionPolicy Bypass -File tests/temporary_test_dual_edge_startup.ps1`; then remove that test; then run the PowerShell parser and `git diff --check`.

Expected: the contract test and parser succeed; the repository has no temporary test file.
