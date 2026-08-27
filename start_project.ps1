param(
    [string]$ProjectRoot,
    [switch]$SkipLLM,
    [switch]$SkipCloudUpdateLLM,
    [int]$EdgeModelInferenceWorkers = 2,
    [int]$EdgeModelQueueCapacity = 160,
    [int]$EdgeModelQueueWaitMs = 15000,
    [int]$EdgeModelTotalTimeoutMs = 20000,
    [int]$SummaryWindowTimeoutSeconds = 40,
    [int]$ExpectedPacketCount = 80,
    # 每个健康门的统一总超时（秒），每 2 秒轮询一次。
    [int]$HealthTimeoutSeconds = 180
)

# 正式启动编排（唯一模式：容器 Edge）：
#   Stage 1  网络模拟器（toxiproxy + mqtt-broker + network-controller，project name
#            固定为 network_simulator）全部 healthy 且所需代理已创建。
#   Stage 2  宿主机 Scheduler 8003 + Cloud 8004 + Summary 8006
#            （HTTP ready，Cloud 模型已加载，Summary 已连接 MQTT）。
#   Stage 3  LLM 服务：Summary 建议 LLM（0.5B @ 8005）+ 云端模型更新 LLM
#            （3B @ 6006）；-SkipLLM 时跳过并禁用 Summary LLM 调用。
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
$ExperimentId = (Get-Date -Format "yyyyMMdd_HHmmss") + "_" + ([guid]::NewGuid().ToString("N").Substring(0, 8))
$ExperimentData = Join-Path $CloudEdge "data\experiments\$ExperimentId"
$env:EXPERIMENT_ID = $ExperimentId
$env:EDGE_MODEL_INFERENCE_WORKERS = [string]$EdgeModelInferenceWorkers
$env:EDGE_MODEL_QUEUE_CAPACITY = [string]$EdgeModelQueueCapacity
$env:EDGE_MODEL_QUEUE_WAIT_MS = [string]$EdgeModelQueueWaitMs
$env:EDGE_MODEL_TOTAL_TIMEOUT_MS = [string]$EdgeModelTotalTimeoutMs
$env:EDGE_EXPECTED_PACKET_COUNT = [string]$ExpectedPacketCount
$gitRevision = (& git -C $ProjectRoot rev-parse --short=12 HEAD 2>$null)
if (-not $gitRevision) { $gitRevision = "unknown" }
$gitRevision = $gitRevision.Trim()
$gitDirty = [bool](& git -C $ProjectRoot status --porcelain 2>$null)
$env:EDGE_BUILD_REVISION = if ($gitDirty) { "$gitRevision-dirty" } else { $gitRevision }
New-Item -ItemType Directory -Path $ExperimentData -Force | Out-Null
@{
    experiment_id = $ExperimentId
    git_revision = $gitRevision
    git_dirty = $gitDirty
    edge_model_inference_workers = $EdgeModelInferenceWorkers
    edge_model_queue_capacity = $EdgeModelQueueCapacity
    edge_model_queue_wait_ms = $EdgeModelQueueWaitMs
    edge_model_total_timeout_ms = $EdgeModelTotalTimeoutMs
    summary_window_timeout_seconds = $SummaryWindowTimeoutSeconds
    expected_packet_count = $ExpectedPacketCount
    created_at = [DateTimeOffset]::Now.ToString("o")
} | ConvertTo-Json | Set-Content -Path (Join-Path $ExperimentData "run_config.json") -Encoding UTF8

# 通用 conda 激活引导：不依赖用户是否执行过 conda init powershell，
# 只要 conda 在 PATH 中即可（前置检查已保证），队友机器同样适用。
# 用法：在子进程命令前拼接本前缀，即可用标准 "conda activate moment"。
$CondaActivatePrefix = "conda shell.powershell hook | Out-String | Invoke-Expression; conda activate moment; "

# EDGE_CONTROL_SHARED_SECRET：Scheduler 与 Edge 之间控制链路的 HMAC 密钥（≥32字节）。
# 优先使用已设置的环境变量；未设置时自动生成一个固定密钥并写入项目根目录的
# .edge_control_secret 文件（首次生成后即固定，后续每次复用同一个值）。
# 该文件不入库，仅本机 Scheduler 与 Edge 配对使用，与他人配置互不影响。
$SecretFile = Join-Path $ProjectRoot ".edge_control_secret"
if ([string]::IsNullOrWhiteSpace($env:EDGE_CONTROL_SHARED_SECRET) -or
    [System.Text.Encoding]::UTF8.GetByteCount($env:EDGE_CONTROL_SHARED_SECRET) -lt 32) {
    if (Test-Path $SecretFile) {
        $env:EDGE_CONTROL_SHARED_SECRET = (Get-Content $SecretFile -Raw).Trim()
    }
    # 未设置、文件不存在，或文件内容无效(过短)时，用 PowerShell 5.1 兼容方式重新生成。
    if ([string]::IsNullOrWhiteSpace($env:EDGE_CONTROL_SHARED_SECRET) -or
        [System.Text.Encoding]::UTF8.GetByteCount($env:EDGE_CONTROL_SHARED_SECRET) -lt 32) {
        $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
        $bytes = New-Object byte[] 48
        $rng.GetBytes($bytes)
        $rng.Dispose()
        $generated = [Convert]::ToBase64String($bytes)
        Set-Content -Path $SecretFile -Value $generated -NoNewline
        $env:EDGE_CONTROL_SHARED_SECRET = $generated
        Write-Host "[info] EDGE_CONTROL_SHARED_SECRET not set/invalid; generated a fixed one at $SecretFile"
    }
}

Write-Host "=== Project Root: $ProjectRoot ==="
Write-Host "=== Edge mode: containers only (compose.multi-edge.yml up -d --no-build) ==="
Write-Host "=== Experiment: $ExperimentId ==="

function Get-Json {
    param([string]$Url, [string]$UserAgent)
    try {
        $params = @{ Uri = $Url; TimeoutSec = 5 }
        if ($UserAgent) {
            # toxiproxy API 会校验 User-Agent：仅放行 toxiproxy 官方客户端 UA，
            # 默认的 PowerShell UA 会被 403 拒绝。这里用官方 client UA 规避。
            $params["Headers"] = @{ "User-Agent" = $UserAgent }
        }
        return Invoke-RestMethod @params
    } catch { return $null }
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
$occupiedPorts = @()
foreach ($port in 8001, 8002) {
    if (Test-TcpPort "127.0.0.1" $port) { $occupiedPorts += $port }
}
if ($occupiedPorts.Count -gt 0) {
    # 端口被占用：通常是上次运行遗留的 edge 容器（Docker 容器不随终端关闭而停止）。
    # 自动执行 compose down 清理，再重新检查；若仍被占用则说明不是本项目的容器。
    Write-Host "  Ports $($occupiedPorts -join ', ') in use. Stopping stale Edge containers ..."
    Push-Location $EdgeService
    try {
        docker compose -f compose.multi-edge.yml down
    } finally {
        Pop-Location
    }
    $occupiedPorts = @()
    foreach ($port in 8001, 8002) {
        if (Test-TcpPort "127.0.0.1" $port) { $occupiedPorts += $port }
    }
}
if ($occupiedPorts.Count -gt 0) {
    Write-Host "  Ports $($occupiedPorts -join ', ') still in use (not from Edge containers). Stop the occupying process first."
    exit 1
}
Write-Host "  Ports free"

if (-not $SkipLLM) {
    $lb = Join-Path $LLM_DIR "llama-server.exe"
    $lm = Join-Path $LLM_DIR "models\qwen2.5-0.5b-instruct-q3_k_m.gguf"
    $cm = Join-Path $LLM_DIR "models\qwen2.5-3b-instruct-q4_k_m.gguf"
    if (-not (Test-Path $lb) -or -not (Test-Path $lm)) {
        Write-Host "  Summary suggestion LLM not deployed (need llama-server.exe + 0.5B model), use -SkipLLM to skip"
        exit 1
    }
    if (-not $SkipCloudUpdateLLM -and -not (Test-Path $cm)) {
        Write-Host "  Cloud model-update LLM not deployed (need 3B model), use -SkipCloudUpdateLLM to skip only it"
        exit 1
    }
    Write-Host "[Check] Summary suggestion LLM OK (0.5B)"
    if (-not $SkipCloudUpdateLLM) {
        Write-Host "[Check] Cloud model-update LLM OK (3B)"
    }
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
# toxiproxy API 仅放行其官方客户端 UA，用于绕过 PowerShell 默认 UA 的 403。
$ToxiproxyUserAgent = "toxiproxy-http-client/2.12.0"

$stage1 = $true
if (-not (Wait-Gate "Toxiproxy API (8474)" { $null -ne (Get-Json "http://127.0.0.1:8474/proxies" -UserAgent $ToxiproxyUserAgent) })) { $stage1 = $false }
if ($stage1 -and -not (Wait-Gate "Toxiproxy proxies present" {
    $proxies = Get-Json "http://127.0.0.1:8474/proxies" -UserAgent $ToxiproxyUserAgent
    if ($null -eq $proxies) { return $false }
    foreach ($name in $requiredProxies) {
        if ($null -eq $proxies.PSObject.Properties[$name]) { return $false }
    }
    return $true
})) { $stage1 = $false }
if ($stage1 -and -not (Wait-Gate "MQTT broker (1883)" { Test-TcpPort "127.0.0.1" 1883 })) { $stage1 = $false }
if ($stage1 -and -not (Wait-Gate "Network controller (8090) reachable" {
    # 注意：此处只要求 controller 可达，不要求 status=="ok"。
    # controller 的 scheduler_reporter 每 1s 探测 Scheduler(host:8003)，而 Scheduler
    # 要到 Stage 2 才启动；若在此要求 status=="ok"，会因 Scheduler 未起而永远
    # degraded，形成 Stage 1 死锁（Scheduler 永远无法被拉起）。故控制器可达即可，
    # 待 Stage 2 的 Scheduler 起来后其 reporter 自然转 healthy。
    $controller = Get-Json "http://127.0.0.1:8090/health"
    $null -ne $controller
})) { $stage1 = $false }
if (-not $stage1) { Show-NetSimDiagnostics; exit 1 }

# ---------- Stage 2: host Scheduler + Cloud + Summary ----------
Write-Host "`n========== Stage 2/4: Scheduler (8003) + Cloud (8004) + Summary (8006) =========="
# Scheduler 使用实验独立的 SQLite：持久 scheduler.db 会跨实验残留 task_id/device_id，
# 造成 TASK_ID_CONFLICT 且污染 stability_score（其读取历史执行记录）。每次实验指向
# 实验 data 子目录，与 Cloud/Summary 的隔离策略一致。
$schedulerDb = Join-Path $ExperimentData "scheduler.db"
$schCmd = "Set-Location '$CloudEdge'; `$env:SCHEDULER_EXPECTED_PACKET_COUNT='$ExpectedPacketCount'; `$env:SCHEDULER_DB_PATH='$schedulerDb'; $CondaActivatePrefix python -m uvicorn scheduler.api:app --host 127.0.0.1 --port 8003"
Start-Process powershell -ArgumentList "-NoExit","-Command",$schCmd

$cloudDb = Join-Path $ExperimentData "cloud_review.db"
$cloudCmd = "Set-Location '$CloudEdge'; `$env:CLOUD_BACKEND='moment_light_adapt'; `$env:CLOUD_MOMENT_DEVICE='auto'; `$env:CLOUD_REVIEW_DB_PATH='$cloudDb'; `$env:SCHEDULER_SERVICE_BASE_URL='http://127.0.0.1:18045'; $CondaActivatePrefix python -m uvicorn cloud_service.app:app --host 127.0.0.1 --port 8004"
Start-Process powershell -ArgumentList "-NoExit","-Command",$cloudCmd

$summaryDb = Join-Path $ExperimentData "summary_service.db"
$summaryLlmEnabled = if ($SkipLLM) { "false" } else { "true" }
$summaryCmd = "Set-Location '$CloudEdge'; `$env:SUMMARY_DATABASE_PATH='$summaryDb'; `$env:SUMMARY_WINDOW_TIMEOUT_SECONDS='$SummaryWindowTimeoutSeconds'; `$env:SUMMARY_SUGGESTION_LLM_ENABLED='$summaryLlmEnabled'; `$env:SUMMARY_SUGGESTION_LLM_BASE_URL='http://127.0.0.1:8005'; $CondaActivatePrefix python -m uvicorn summary_service.app:app --host 127.0.0.1 --port 8006"
Start-Process powershell -ArgumentList "-NoExit","-Command",$summaryCmd

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
if ($stage2 -and -not (Wait-Gate "Summary /health (8006, MQTT connected)" {
    $summary = Get-Json "http://127.0.0.1:8006/health"
    $null -ne $summary -and $summary.status -eq "ok" -and $summary.mqtt_connected -eq $true
})) { $stage2 = $false }
if (-not $stage2) {
    Write-Host "  Check the Scheduler / Cloud / Summary PowerShell windows above."
    exit 1
}

# ---------- Stage 3: LLM services ----------
if (-not $SkipLLM) {
    Write-Host "`n========== Stage 3/4: LLM services =========="
    # Summary 建议 LLM（0.5B）：Summary 宿主机进程通过 127.0.0.1:8005 调用。
    $llmCmd = "Set-Location '$LLM_DIR'; .\llama-server.exe --model .\models\qwen2.5-0.5b-instruct-q3_k_m.gguf --host 127.0.0.1 --port 8005 --ctx-size 2048 --n-gpu-layers 99"
    Start-Process powershell -ArgumentList "-NoExit","-Command",$llmCmd
    $stage3 = $true
    if (-not (Wait-Gate "Summary suggestion LLM /v1/models (8005)" {
        $models = Get-Json "http://127.0.0.1:8005/v1/models"
        $null -ne $models -and $models.data.Count -gt 0
    })) { $stage3 = $false }
    if (-not $SkipCloudUpdateLLM) {
        # 云端模型更新 LLM（3B）：Cloud 模型更新建议书使用（VLLM_URL 默认 6006）。
        $cloudLlmCmd = "Set-Location '$LLM_DIR'; .\llama-server.exe --model .\models\qwen2.5-3b-instruct-q4_k_m.gguf --host 127.0.0.1 --port 6006 --ctx-size 4096 --n-gpu-layers 99"
        Start-Process powershell -ArgumentList "-NoExit","-Command",$cloudLlmCmd
        if ($stage3 -and -not (Wait-Gate "Cloud model-update LLM /v1/models (6006)" {
            $models = Get-Json "http://127.0.0.1:6006/v1/models"
            $null -ne $models -and $models.data.Count -gt 0
        })) { $stage3 = $false }
    } else {
        Write-Host "  Cloud model-update LLM skipped; Summary suggestion LLM remains enabled."
    }
    if (-not $stage3) {
        Write-Host "  Check the LLM PowerShell windows above."
        exit 1
    }
} else {
    Write-Host "`n========== Stage 3/4: LLM skipped =========="
    Write-Host "  SUMMARY_SUGGESTION_LLM_ENABLED=false - Summary suggestions fall back to templates."
    Write-Host "  Cloud model-update LLM (6006) is not started."
}

# ---------- Stage 4: Edge containers ----------
Write-Host "`n========== Stage 4/4: Edge containers (edge_01 + edge_02) =========="
Push-Location $EdgeService
try {
    # 每次实验使用新的 SQLite 子目录。让正式 UID/GID 10001 自己创建目录，
    # 避免 Docker Desktop 命名卷中 root 创建的目录无法再 chown 给 Edge 用户。
    docker compose -f compose.multi-edge.yml stop edge_01 edge_02 | Out-Null
    $edgeDbInitCode = "import os; from pathlib import Path; p=Path(os.environ['EDGE_EXPERIMENT_DATABASE_PATH']).parent; p.mkdir(parents=True, exist_ok=True)"
    foreach ($edgeServiceName in "edge_01", "edge_02") {
        docker compose -f compose.multi-edge.yml run --rm --no-deps --entrypoint python $edgeServiceName -c $edgeDbInitCode
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  Failed to initialize experiment database directory for $edgeServiceName"
            exit 1
        }
    }
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
foreach ($edgeHealth in @($edge01Health, $edge02Health)) {
    if ($edgeHealth.model_queue.consumer_count -ne $EdgeModelInferenceWorkers -or
        $edgeHealth.model_queue.capacity -ne $EdgeModelQueueCapacity -or
        $edgeHealth.routing_pool.alive -ne $true -or
        $edgeHealth.bearing_publisher.alive -ne $true) {
        Write-Host "  Edge benchmark runtime configuration mismatch"
        Show-EdgeDiagnostics
        exit 1
    }
}

Write-Host "`n========== All health gates passed =========="
if ($SkipLLM) { Write-Host "(LLM skipped - SUMMARY_SUGGESTION_LLM_ENABLED=false, cloud model-update LLM not started)" }
Write-Host "Sender may start replaying MAT data now."
Write-Host "Experiment data: $ExperimentData"
Write-Host "Stop: close host windows (Scheduler/Cloud/Summary/LLM), then:"
Write-Host "  cd $EdgeService ; docker compose -f compose.multi-edge.yml down"
Write-Host "  cd $NetSim ; docker compose -p $NetSimProject down"
