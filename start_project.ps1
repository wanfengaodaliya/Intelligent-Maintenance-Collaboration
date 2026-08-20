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

Write-Host "=== Project Root: $ProjectRoot ==="

# Pre-checks
Write-Host "[Check] Docker Desktop ..."
try { docker info 2>&1 | Out-Null; Write-Host "  Docker OK" }
catch { Write-Host "  Docker not running, start Docker Desktop first"; exit 1 }

Write-Host "[Check] Conda moment env ..."
$condaEnvs = conda env list 2>&1
if ($condaEnvs -notmatch "^\s*moment\s") { Write-Host "  moment env missing, create it first"; exit 1 }
Write-Host "  moment OK"

Write-Host "[Check] MOMENT model ..."
$p = Join-Path $CloudEdge "local_experiment\analysis\final_model\moment_final_chance\SCL05\fold_3\best_model.pt"
if (-not (Test-Path $p)) { Write-Host "  MOMENT missing, download first"; exit 1 }
Write-Host "  MOMENT OK"

Write-Host "[Check] H5 model ..."
$h5Root = Join-Path $CloudEdge "edge_service\models\distilled_h5"
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
    if (-not (Test-Path $lb) -or -not (Test-Path $lm)) {
        Write-Host "  LLM not deployed, use -SkipLLM to skip"
        exit 1
    }
    Write-Host "  LLM OK"
}

# Window 1: Docker Network Simulator
Write-Host "[Win 1] Starting Docker network simulator ..."
Push-Location $NetSim
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
docker compose --env-file .env up -d --build
Pop-Location
Start-Sleep -Seconds 15

# Window 2: Scheduler
Write-Host "[Win 2] Starting Scheduler ..."
$schCmd = "Set-Location '$CloudEdge'; `$env:SCHEDULER_EDGE_NODES_JSON='{""edge_01"":{""control_url"":""http://127.0.0.1:18042"",""target_topic"":""edge/edge_01/input""}}'; conda activate moment; python -m uvicorn scheduler.api:app --host 127.0.0.1 --port 8003"
Start-Process powershell -ArgumentList "-NoExit","-Command",$schCmd

# Window 3: Cloud
Write-Host "[Win 3] Starting Cloud service ..."
$cloudCmd = "Set-Location '$CloudEdge'; `$env:CLOUD_BACKEND='moment_light_adapt'; `$env:CLOUD_MOMENT_DEVICE='auto'; `$env:SCHEDULER_SERVICE_BASE_URL='http://127.0.0.1:18045'; conda activate moment; python -m uvicorn cloud_service.app:app --host 127.0.0.1 --port 8004"
Start-Process powershell -ArgumentList "-NoExit","-Command",$cloudCmd

# Window 4: Edge
Write-Host "[Win 4] Starting Edge service ..."
$edgeCmd = "Set-Location '$CloudEdge'; `$env:EDGE_NODE_ID='edge_01'; `$env:SCHEDULER_SERVICE_BASE_URL='http://127.0.0.1:18011'; `$env:CLOUD_SERVICE_BASE_URL='http://127.0.0.1:18021'; conda activate moment; python edge_service/run_edge_service.py --host 127.0.0.1 --port 8001"
Start-Process powershell -ArgumentList "-NoExit","-Command",$edgeCmd

# Window 5: LLM (optional)
if (-not $SkipLLM) {
    Write-Host "[Win 5] Starting LLM service ..."
    $llmCmd = "Set-Location '$LLM_DIR'; .\llama-server.exe --model .\models\qwen2.5-0.5b-instruct-q3_k_m.gguf --host 127.0.0.1 --port 8002 --ctx-size 2048 --n-gpu-layers 99"
    Start-Process powershell -ArgumentList "-NoExit","-Command",$llmCmd
}

# Wait and health check
Write-Host "Waiting for services (20s) ..."
Start-Sleep -Seconds 20

Write-Host "`n========== Health Checks =========="
function Check-Svc {
    param($Name,$Url,$Script)
    try {
        $r = Invoke-RestMethod $Url -TimeoutSec 5
        $ok = & $Script $r
        Write-Host "  [$Name] $(if($ok){'OK'}else{'WARN'})"
    } catch {
        Write-Host "  [$Name] FAIL"
    }
}
Check-Svc "NetSim(8090)" "http://127.0.0.1:8090/health" { param($r) $r.status -eq "ok" }
Check-Svc "Scheduler(8003)" "http://127.0.0.1:8003/health" { param($r) $r.status -eq "ok" }
Check-Svc "Cloud(8004)" "http://127.0.0.1:8004/health" { param($r) $r.status -eq "ok" -and $r.model_backend -eq "moment_light_adapt" }
Check-Svc "Edge(8001)" "http://127.0.0.1:8001/health" { param($r) $r.status -eq "ok" -and $r.node_id -eq "edge_01" -and $r.mqtt_connected -eq $true }
if (-not $SkipLLM) {
    Check-Svc "LLM(8002)" "http://127.0.0.1:8002/v1/models" { param($r) $r.data.Count -gt 0 }
}

Write-Host "`n========== Done =========="
if ($SkipLLM) { Write-Host "(LLM skipped - fallback templates will be used)" }
Write-Host "Run Sender module to replay MAT data."
Write-Host "Stop: close windows or Ctrl+C, then 'docker compose down'"
