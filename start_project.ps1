param(
    [string]$ProjectRoot,
    [switch]$SkipLLM,
    # 每个健康门的统一总超时（秒），每 2 秒轮询一次。
    [int]$HealthTimeoutSeconds = 180
)

# 正式启动编排（唯一模式：容器 Edge）：
#   Stage 1  网络模拟器（toxiproxy + mqtt-broker + network-controller，project name
#            固定为 network_simulator）全部 healthy 且所需代理已创建。
#   Stage 2  宿主机 Scheduler 8003 + Cloud 8004（HTTP ready，Cloud 模型已加载）。
#   Stage 3  LLM 服务：边缘建议 LLM（0.5B @ 8005）+ 云端模型更新 LLM
#            （3B @ 6006）；-SkipLLM 时跳过并显式禁用 Edge LLM 调用。
#   Stage 4  edge_01 + edge_02 容器（compose.multi-edge.yml，--no-build），
#            轮询 /health/ready（Docker HEALTHCHECK 仅代表 liveness）。
#   全部通过后才允许 Sender 开始发送。
# 任一健康门失败：打印对应容器/进程状态与最近日志并终止，不依赖 restart 策略排序。

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$CloudEdge = Join-Path $ProjectRoot "cloud_edge_project"
$NetSim = Join-Path $CloudEdge "internet_service\network_simulator"
$EdgeService = Join-Path $CloudEdge "edge_service"
$NetSimProject = "network_simulator"
$LLM_DIR = "D:\develop\llama.cpp"
$PollIntervalSeconds = 2

if ([string]::IsNullOrWhiteSpace($env:EDGE_CONTROL_SHARED_SECRET) -or
    [System.Text.Encoding]::UTF8.GetByteCount($env:EDGE_CONTROL_SHARED_SECRET) -lt 32) {
    throw "EDGE_CONTROL_SHARED_SECRET must be set to at least 32 bytes before startup"
}

Write-Host "=== Project Root: $ProjectRoot ==="
Write-Host "=== Edge mode: containers only (compose.multi-edge.yml up -d --no-build) ==="

function Get-Json {
    param([string]$Url)
    try { return Invoke-RestMethod -Uri $Url -TimeoutSec 5 } catch { return $null }
}

function Test-TcpPort {
    param([string]$HostName, [int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connect = $client.ConnectAsync($HostName, $Port)
        if ($connect.Wait(3000) -and $client.Connected) { return $true }
        return $false
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Wait-Gate {
    param([string]$Name, [scriptblock]$Probe)
    $deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Probe) {
            Write-Host "  [$Name] OK"
            return $true
        }
        Start-Sleep -Seconds $PollIntervalSeconds
    }
    Write-Host "  [$Name] FAIL (not healthy within ${HealthTimeoutSeconds}s)"
    return $false
}

function Show-NetSimDiagnostics {
    Write-Host "--- network simulator status ---"
    Push-Location $NetSim
    docker compose -p $NetSimProject ps
    docker compose -p $NetSimProject logs --tail 30 toxiproxy mqtt-broker network-controller
    Pop-Location
}

function Show-EdgeDiagnostics {
    Write-Host "--- edge compose status ---"
    Push-Location $EdgeService
    docker compose -f compose.multi-edge.yml ps
    docker compose -f compose.multi-edge.yml logs --tail 50 edge_01 edge_02
    Pop-Location
}

# ---------- Pre-checks ----------
Write-Host "[Check] Docker Desktop ..."
$dockerInfo = docker info 2>&1
if ($LASTEXITCODE -ne 0) { Write-Host "  Docker not running, start Docker Desktop first"; exit 1 }
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

Write-Host "[Check] H5 distilled model (edge image build context) ..."
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
if (-not (Test-Path (Join-Path $h5Root "$activeVersion\best_model.pt"))) {
    Write-Host "  H5 checkpoint missing for active version: $activeVersion"
    exit 1
}
Write-Host "  H5 OK (active: $activeVersion)"

Write-Host "[Check] Edge image cloud-edge/edge-service:latest ..."
$null = docker image inspect cloud-edge/edge-service:latest 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Image missing. Build it first:"
    Write-Host "    cd $EdgeService ; docker compose -f compose.multi-edge.yml build"
    exit 1
}
Write-Host "  Image OK"

Write-Host "[Check] Host ports 8001/8002 must be free for the Edge containers ..."
foreach ($port in 8001, 8002) {
    if (Test-TcpPort "127.0.0.1" $port) {
        Write-Host "  Port $port already in use (stale host Edge process or container). Stop it first."
        exit 1
    }
}
Write-Host "  Ports free"

if (-not $SkipLLM) {
    $lb = Join-Path $LLM_DIR "llama-server.exe"
    $lm = Join-Path $LLM_DIR "models\qwen2.5-0.5b-instruct-q3_k_m.gguf"
    $cm = Join-Path $LLM_DIR "models\qwen2.5-3b-instruct-q4_k_m.gguf"
    if (-not (Test-Path $lb) -or -not (Test-Path $lm) -or -not (Test-Path $cm)) {
        Write-Host "  LLM not fully deployed (need llama-server.exe + 0.5B + 3B models), use -SkipLLM to skip"
        exit 1
    }
    Write-Host "[Check] LLM OK (0.5B suggestion + 3B cloud model-update)"
}

# ---------- Stage 1: network simulator ----------
Write-Host "`n========== Stage 1/4: Network simulator =========="
Push-Location $NetSim
try {
    if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
    docker compose -p $NetSimProject --env-file .env up -d --build
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Network simulator failed to start"
        Show-NetSimDiagnostics
        exit 1
    }
} finally {
    Pop-Location
}

$requiredProxies = @(
    "edge_01__to__scheduler__http",
    "edge_01__to__cloud__http",
    "scheduler__to__edge_01__http",
    "edge_02__to__scheduler__http",
    "edge_02__to__cloud__http",
    "scheduler__to__edge_02__http",
    "cloud__to__scheduler__http"
)

$stage1 = $true
if (-not (Wait-Gate "Toxiproxy API (8474)" { $null -ne (Get-Json "http://127.0.0.1:8474/proxies") })) { $stage1 = $false }
if ($stage1 -and -not (Wait-Gate "Toxiproxy proxies present" {
    $proxies = Get-Json "http://127.0.0.1:8474/proxies"
    if ($null -eq $proxies) { return $false }
    foreach ($name in $requiredProxies) {
        if ($null -eq $proxies.PSObject.Properties[$name]) { return $false }
    }
    return $true
})) { $stage1 = $false }
if ($stage1 -and -not (Wait-Gate "MQTT broker (1883)" { Test-TcpPort "127.0.0.1" 1883 })) { $stage1 = $false }
if ($stage1 -and -not (Wait-Gate "Network controller (8090)" {
    $controller = Get-Json "http://127.0.0.1:8090/health"
    $null -ne $controller -and $controller.status -eq "ok"
})) { $stage1 = $false }
if (-not $stage1) { Show-NetSimDiagnostics; exit 1 }

# ---------- Stage 2: host Scheduler + Cloud ----------
Write-Host "`n========== Stage 2/4: Host Scheduler (8003) + Cloud (8004) =========="
$schCmd = "Set-Location '$CloudEdge'; conda activate moment; python -m uvicorn scheduler.api:app --host 127.0.0.1 --port 8003"
Start-Process powershell -ArgumentList "-NoExit","-Command",$schCmd

$cloudCmd = "Set-Location '$CloudEdge'; `$env:CLOUD_BACKEND='moment_light_adapt'; `$env:CLOUD_MOMENT_DEVICE='auto'; `$env:SCHEDULER_SERVICE_BASE_URL='http://127.0.0.1:18045'; conda activate moment; python -m uvicorn cloud_service.app:app --host 127.0.0.1 --port 8004"
Start-Process powershell -ArgumentList "-NoExit","-Command",$cloudCmd

$stage2 = $true
if (-not (Wait-Gate "Scheduler /health (8003)" {
    $scheduler = Get-Json "http://127.0.0.1:8003/health"
    $null -ne $scheduler -and $scheduler.status -eq "ok"
})) { $stage2 = $false }
if ($stage2 -and -not (Wait-Gate "Cloud /health (8004, moment_light_adapt loaded)" {
    # Cloud /health 仅在 MOMENT 模型加载完成后返回 200。
    $cloud = Get-Json "http://127.0.0.1:8004/health"
    $null -ne $cloud -and $cloud.status -eq "ok" -and $cloud.model_backend -eq "moment_light_adapt"
})) { $stage2 = $false }
if (-not $stage2) {
    Write-Host "  Check the Scheduler / Cloud PowerShell windows above."
    exit 1
}

# ---------- Stage 3: LLM services ----------
if (-not $SkipLLM) {
    Write-Host "`n========== Stage 3/4: LLM services (edge 8005 + cloud 6006) =========="
    # 边缘建议 LLM（0.5B）：Edge 容器经 host.docker.internal:8005 调用。
    $llmCmd = "Set-Location '$LLM_DIR'; .\llama-server.exe --model .\models\qwen2.5-0.5b-instruct-q3_k_m.gguf --host 127.0.0.1 --port 8005 --ctx-size 2048 --n-gpu-layers 99"
    Start-Process powershell -ArgumentList "-NoExit","-Command",$llmCmd
    # 云端模型更新 LLM（3B）：Cloud 模型更新建议书使用（VLLM_URL 默认 6006）。
    $cloudLlmCmd = "Set-Location '$LLM_DIR'; .\llama-server.exe --model .\models\qwen2.5-3b-instruct-q4_k_m.gguf --host 127.0.0.1 --port 6006 --ctx-size 4096 --n-gpu-layers 99"
    Start-Process powershell -ArgumentList "-NoExit","-Command",$cloudLlmCmd
    $stage3 = $true
    if (-not (Wait-Gate "Edge suggestion LLM /v1/models (8005)" {
        $models = Get-Json "http://127.0.0.1:8005/v1/models"
        $null -ne $models -and $models.data.Count -gt 0
    })) { $stage3 = $false }
    if ($stage3 -and -not (Wait-Gate "Cloud model-update LLM /v1/models (6006)" {
        $models = Get-Json "http://127.0.0.1:6006/v1/models"
        $null -ne $models -and $models.data.Count -gt 0
    })) { $stage3 = $false }
    if (-not $stage3) {
        Write-Host "  Check the LLM PowerShell windows above."
        exit 1
    }
    $env:EDGE_SUGGESTION_LLM_ENABLED = "true"
} else {
    Write-Host "`n========== Stage 3/4: LLM skipped =========="
    Write-Host "  EDGE_SUGGESTION_LLM_ENABLED=false - Edge suggestion LLM calls are disabled in both containers."
    Write-Host "  Cloud model-update LLM (6006) not started; suggestions fall back to templates."
    $env:EDGE_SUGGESTION_LLM_ENABLED = "false"
}

# ---------- Stage 4: Edge containers ----------
Write-Host "`n========== Stage 4/4: Edge containers (edge_01 + edge_02) =========="
Push-Location $EdgeService
try {
    docker compose -f compose.multi-edge.yml up -d --no-build
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Edge compose failed to start"
        Show-EdgeDiagnostics
        exit 1
    }
} finally {
    Pop-Location
}

$stage4 = $true
if (-not (Wait-Gate "edge_01 /health/ready (8001)" {
    $ready = Get-Json "http://127.0.0.1:8001/health/ready"
    $null -ne $ready -and $ready.ready -eq $true
})) { $stage4 = $false }
if ($stage4 -and -not (Wait-Gate "edge_02 /health/ready (8002)" {
    $ready = Get-Json "http://127.0.0.1:8002/health/ready"
    $null -ne $ready -and $ready.ready -eq $true
})) { $stage4 = $false }
if (-not $stage4) { Show-EdgeDiagnostics; exit 1 }

$edge01Health = Get-Json "http://127.0.0.1:8001/health"
$edge02Health = Get-Json "http://127.0.0.1:8002/health"
Write-Host ("  edge_01 node_id={0} mqtt_connected={1}" -f $edge01Health.node_id, $edge01Health.mqtt_connected)
Write-Host ("  edge_02 node_id={0} mqtt_connected={1}" -f $edge02Health.node_id, $edge02Health.mqtt_connected)
if ($edge01Health.node_id -ne "edge_01" -or $edge02Health.node_id -ne "edge_02" -or
    $edge01Health.mqtt_connected -ne $true -or $edge02Health.mqtt_connected -ne $true) {
    Write-Host "  Edge node identity or MQTT connection mismatch"
    Show-EdgeDiagnostics
    exit 1
}

Write-Host "`n========== All health gates passed =========="
if ($SkipLLM) { Write-Host "(LLM skipped - EDGE_SUGGESTION_LLM_ENABLED=false, cloud model-update LLM not started)" }
Write-Host "Sender may start replaying MAT data now."
Write-Host "Stop: close host windows (Scheduler/Cloud/LLM), then:"
Write-Host "  cd $EdgeService ; docker compose -f compose.multi-edge.yml down"
Write-Host "  cd $NetSim ; docker compose -p $NetSimProject down"
