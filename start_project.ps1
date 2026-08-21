param(
    [string]$ProjectRoot,
    [switch]$SkipLLM
)

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$CloudEdge = Join-Path $ProjectRoot "cloud_edge_project"
$NetSim = Join-Path $CloudEdge "internet_service\network_simulator"
$LLM_DIR = "D:\develop\llama.cpp"

if ([string]::IsNullOrWhiteSpace($env:EDGE_CONTROL_SHARED_SECRET) -or
    [System.Text.Encoding]::UTF8.GetByteCount($env:EDGE_CONTROL_SHARED_SECRET) -lt 32) {
    throw "EDGE_CONTROL_SHARED_SECRET must be set to at least 32 bytes before startup"
}

Write-Host "=== Project Root: $ProjectRoot ==="

# Pre-checks
Write-Host "[Check] Docker Desktop ..."
$dockerInfo = docker info 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Docker not running, start Docker Desktop first"
    exit 1
}
Write-Host "  Docker OK"

Write-Host "[Check] Conda moment env ..."
$condaEnvs = conda env list 2>&1
if ($LASTEXITCODE -ne 0) { Write-Host "  Could not query Conda environments"; exit 1 }
if (-not ($condaEnvs -match "^\s*moment\s")) { Write-Host "  moment env missing, create it first"; exit 1 }
Write-Host "  moment OK"

Write-Host "[Check] MOMENT model ..."
$requiredMomentFiles = @(
    (Join-Path $CloudEdge "model_assets\moment\releases\moment-scl05-final\best_model.pt"),
    (Join-Path $CloudEdge "model_assets\moment\releases\moment-scl05-final\condition_norm.json"),
    (Join-Path $CloudEdge "model_assets\moment\releases\moment-scl05-final\moment_model.py"),
    (Join-Path $CloudEdge "model_assets\moment\pretrained\MOMENT-1-small\config.json"),
    (Join-Path $CloudEdge "model_assets\moment\pretrained\MOMENT-1-small\model.safetensors")
)
$missingMomentFiles = @($requiredMomentFiles | Where-Object { -not (Test-Path -LiteralPath $_) })
if ($missingMomentFiles.Count -gt 0) {
    Write-Host "  MOMENT assets missing, download them first:"
    $missingMomentFiles | ForEach-Object { Write-Host "    - $_" }
    exit 1
}
Write-Host "  MOMENT OK"

Write-Host "[Check] H5 model ..."
$h5Root = Join-Path $CloudEdge "edge_service\models\distilled_h5"
$edge01ModelState = Join-Path $h5Root "edge_01_model_update_state.json"
$edge02ModelState = Join-Path $h5Root "edge_02_model_update_state.json"
$activePointer = Join-Path $h5Root "active_version.json"
if (-not (Test-Path $activePointer)) {
    Write-Host "  H5 active_version.json missing, restore the model package first"
    exit 1
}
try {
    $activeVersion = ((Get-Content $activePointer -Raw | ConvertFrom-Json).version).ToString().Trim()
} catch {
    Write-Host "  H5 active_version.json is invalid"
    exit 1
}
if ([string]::IsNullOrWhiteSpace($activeVersion) -or $activeVersion -notmatch '^[A-Za-z0-9._-]+$') {
    Write-Host "  H5 active version is invalid"
    exit 1
}
$h5VersionDir = Join-Path $h5Root $activeVersion
$p = Join-Path $h5VersionDir "best_model.pt"
if (-not (Test-Path $p)) {
    Write-Host "  H5 checkpoint missing for active version: $activeVersion"
    exit 1
}
Write-Host "  H5 OK"

if (-not $SkipLLM) {
    $lb = Join-Path $LLM_DIR "llama-server.exe"
    $lm = Join-Path $LLM_DIR "models\qwen2.5-0.5b-instruct-q3_k_m.gguf"
    $cm = Join-Path $LLM_DIR "models\qwen2.5-3b-instruct-q4_k_m.gguf"
    if (-not (Test-Path $lb) -or -not (Test-Path $lm) -or -not (Test-Path $cm)) {
        Write-Host "  LLM not fully deployed (need llama-server.exe + 0.5B + 3B models), use -SkipLLM to skip"
        exit 1
    }
    Write-Host "  LLM OK"
}

# Window 1: Docker Network Simulator
Write-Host "[Win 1] Starting Docker network simulator ..."
Push-Location $NetSim
try {
    if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
    docker compose --env-file .env up -d --build
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Network simulator failed to start"
        exit 1
    }
} finally {
    Pop-Location
}
Start-Sleep -Seconds 15

# 每个 Edge 都有自己的运行状态，避免两个进程争用同一个 SQLite 文件或缓存目录。
$edge01Data = Join-Path $CloudEdge "data\edge_01"
$edge02Data = Join-Path $CloudEdge "data\edge_02"
New-Item -ItemType Directory -Force -Path $edge01Data, $edge02Data | Out-Null

# Window 2: Scheduler
Write-Host "[Win 2] Starting Scheduler ..."
$schedulerNodesJson = '{"edge_01":{"control_url":"http://127.0.0.1:18042","target_topic":"edge/edge_01/input"},"edge_02":{"control_url":"http://127.0.0.1:18052","target_topic":"edge/edge_02/input"}}'
$env:SCHEDULER_EDGE_NODES_JSON = $schedulerNodesJson
$schCmd = "Set-Location '$CloudEdge'; conda activate moment; python -m uvicorn scheduler.api:app --host 127.0.0.1 --port 8003"
Start-Process powershell -ArgumentList "-NoExit","-Command",$schCmd

# Window 3: Cloud
Write-Host "[Win 3] Starting Cloud service ..."
$cloudCmd = "Set-Location '$CloudEdge'; `$env:CLOUD_BACKEND='moment_light_adapt'; `$env:CLOUD_MOMENT_DEVICE='auto'; `$env:SCHEDULER_SERVICE_BASE_URL='http://127.0.0.1:18045'; conda activate moment; python -m uvicorn cloud_service.app:app --host 127.0.0.1 --port 8004"
Start-Process powershell -ArgumentList "-NoExit","-Command",$cloudCmd

# Window 4: Edge 01
Write-Host "[Win 4] Starting Edge service edge_01 ..."
$edge01Cmd = "Set-Location '$CloudEdge'; `$env:EDGE_NODE_ID='edge_01'; `$env:EDGE_MQTT_CLIENT_ID='edge_01-runtime'; `$env:EDGE_MQTT_INPUT_TOPIC='edge/edge_01/input'; `$env:SCHEDULER_SERVICE_BASE_URL='http://127.0.0.1:18011'; `$env:CLOUD_SERVICE_BASE_URL='http://127.0.0.1:18021'; `$env:EDGE_SUGGESTION_LLM_BASE_URL='http://127.0.0.1:8005'; `$env:EDGE_V12_DATABASE_PATH='$edge01Data\edge_v12.db'; `$env:EDGE_PACKET_ROUTE_ERROR_LOG='$edge01Data\edge_packet_route_errors.jsonl'; `$env:EDGE_CLOUD_REVIEW_CACHE_DIR='$edge01Data\cloud_review'; `$env:EDGE_RAW_SAMPLE_DIRECTORY='$edge01Data\raw_analysis_samples'; `$env:EDGE_MODEL_UPDATE_STATE_PATH='$edge01ModelState'; `$env:EDGE_NETWORK_LINK_ID='edge_01__to__scheduler__http'; conda activate moment; python edge_service/run_edge_service.py --host 127.0.0.1 --port 8001"
Start-Process powershell -ArgumentList "-NoExit","-Command",$edge01Cmd

# Window 5: Edge 02
Write-Host "[Win 5] Starting Edge service edge_02 ..."
$edge02Cmd = "Set-Location '$CloudEdge'; `$env:EDGE_NODE_ID='edge_02'; `$env:EDGE_MQTT_CLIENT_ID='edge_02-runtime'; `$env:EDGE_MQTT_INPUT_TOPIC='edge/edge_02/input'; `$env:SCHEDULER_SERVICE_BASE_URL='http://127.0.0.1:18051'; `$env:CLOUD_SERVICE_BASE_URL='http://127.0.0.1:18053'; `$env:EDGE_SUGGESTION_LLM_BASE_URL='http://127.0.0.1:8005'; `$env:EDGE_V12_DATABASE_PATH='$edge02Data\edge_v12.db'; `$env:EDGE_PACKET_ROUTE_ERROR_LOG='$edge02Data\edge_packet_route_errors.jsonl'; `$env:EDGE_CLOUD_REVIEW_CACHE_DIR='$edge02Data\cloud_review'; `$env:EDGE_RAW_SAMPLE_DIRECTORY='$edge02Data\raw_analysis_samples'; `$env:EDGE_MODEL_UPDATE_STATE_PATH='$edge02ModelState'; `$env:EDGE_NETWORK_LINK_ID='edge_02__to__scheduler__http'; conda activate moment; python edge_service/run_edge_service.py --host 127.0.0.1 --port 8002"
Start-Process powershell -ArgumentList "-NoExit","-Command",$edge02Cmd

# Window 6: Edge suggestion LLM (optional)
if (-not $SkipLLM) {
    Write-Host "[Win 6] Starting edge suggestion LLM service ..."
    $llmCmd = "Set-Location '$LLM_DIR'; .\llama-server.exe --model .\models\qwen2.5-0.5b-instruct-q3_k_m.gguf --host 127.0.0.1 --port 8005 --ctx-size 2048 --n-gpu-layers 99"
    Start-Process powershell -ArgumentList "-NoExit","-Command",$llmCmd
}

# Window 7: Cloud model-update LLM (optional)
if (-not $SkipLLM) {
    Write-Host "[Win 7] Starting cloud model-update LLM service ..."
    $cloudLlmCmd = "Set-Location '$LLM_DIR'; .\llama-server.exe --model .\models\qwen2.5-3b-instruct-q4_k_m.gguf --host 127.0.0.1 --port 6006 --ctx-size 4096 --n-gpu-layers 99"
    Start-Process powershell -ArgumentList "-NoExit","-Command",$cloudLlmCmd
}

# Wait and health check
Write-Host "Waiting for services (20s) ..."
Start-Sleep -Seconds 20

Write-Host "`n========== Health Checks =========="
function Check-Svc {
    param($Name,$Url,$Script,[int]$Attempts = 1)
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            $r = Invoke-RestMethod $Url -TimeoutSec 5
            $ok = & $Script $r
            if ($ok) {
                Write-Host "  [$Name] OK"
                return $true
            }
        } catch {
        }
        if ($attempt -lt $Attempts) {
            Start-Sleep -Seconds 2
        }
    }
    Write-Host "  [$Name] FAIL"
    return $false
}
$allHealthy = $true
if (-not (Check-Svc "NetSim(8090)" "http://127.0.0.1:8090/health" { param($r) $r.status -eq "ok" })) { $allHealthy = $false }
if (-not (Check-Svc "Scheduler(8003)" "http://127.0.0.1:8003/health" { param($r) $r.status -eq "ok" })) { $allHealthy = $false }
if (-not (Check-Svc "Cloud(8004)" "http://127.0.0.1:8004/health" { param($r) $r.status -eq "ok" -and $r.model_backend -eq "moment_light_adapt" } 20)) { $allHealthy = $false }
if (-not (Check-Svc "Edge(8001)" "http://127.0.0.1:8001/health" { param($r) $r.status -eq "ok" -and $r.node_id -eq "edge_01" -and $r.mqtt_connected -eq $true })) { $allHealthy = $false }
if (-not (Check-Svc "Edge(8002)" "http://127.0.0.1:8002/health" { param($r) $r.status -eq "ok" -and $r.node_id -eq "edge_02" -and $r.mqtt_connected -eq $true })) { $allHealthy = $false }
if (-not $SkipLLM) {
    if (-not (Check-Svc "LLM(8005)" "http://127.0.0.1:8005/v1/models" { param($r) $r.data.Count -gt 0 })) { $allHealthy = $false }
    if (-not (Check-Svc "CloudLLM(6006)" "http://127.0.0.1:6006/v1/models" { param($r) $r.data.Count -gt 0 })) { $allHealthy = $false }
}
if (-not $allHealthy) {
    Write-Host "`n========== Startup FAILED =========="
    Write-Host "One or more required services are unhealthy. Check the service windows above."
    exit 1
}

Write-Host "`n========== Done =========="
if ($SkipLLM) { Write-Host "(LLM skipped - fallback templates will be used)" }
Write-Host "Run Sender module to replay MAT data."
Write-Host "Stop: close windows or Ctrl+C, then 'docker compose down'"
