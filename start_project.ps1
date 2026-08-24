param(
    [string]$ProjectRoot,
    [string]$EnvFile,
    [switch]$CheckConfig,
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

if (-not $EnvFile) { $EnvFile = Join-Path $ProjectRoot ".env" }
if (Test-Path -LiteralPath $EnvFile) {
    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        if ($line -notmatch '^\s*(?:#|$)' -and $line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            $name = $Matches[1]
            $value = $Matches[2].Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            if ($null -eq [Environment]::GetEnvironmentVariable($name)) {
                [Environment]::SetEnvironmentVariable($name, $value)
            }
        }
    }
    Write-Host "[info] Loaded deployment defaults from $EnvFile"
}

function Get-EnvValue {
    param([string]$Name, [string]$Default)
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value
}

function Resolve-DeploymentPath {
    param([string]$Path, [string]$BasePath)
    if ([System.IO.Path]::IsPathRooted($Path)) { return $Path }
    return Join-Path $BasePath $Path
}

function Assert-UrlPort {
    param([string]$Name, [string]$Url, [int]$ExpectedPort)
    try { $uri = [uri]$Url } catch { throw "$Name must be a valid URL: $Url" }
    if (-not $uri.IsAbsoluteUri -or $uri.Port -ne $ExpectedPort) {
        throw "$Name port ($($uri.Port)) must match configured service port ($ExpectedPort)"
    }
}

$CloudEdge = Join-Path $ProjectRoot "cloud_edge_project"
$NetSim = Join-Path $CloudEdge "internet_service\network_simulator"
$EdgeService = Join-Path $CloudEdge "edge_service"
$NetSimProject = Get-EnvValue "NETWORK_COMPOSE_PROJECT" "network_simulator"
$LLM_DIR = Get-EnvValue "LLAMA_CPP_DIR" (Join-Path $ProjectRoot "tools\llama.cpp")
if (-not [System.IO.Path]::IsPathRooted($LLM_DIR)) { $LLM_DIR = Join-Path $ProjectRoot $LLM_DIR }
$CondaEnvName = Get-EnvValue "PROJECT_CONDA_ENV" "moment"
$HealthHost = Get-EnvValue "PROJECT_HEALTH_HOST" "127.0.0.1"
$SchedulerHost = Get-EnvValue "SCHEDULER_SERVICE_HOST" "127.0.0.1"
$SchedulerPort = [int](Get-EnvValue "SCHEDULER_SERVICE_PORT" "8003")
$CloudHost = Get-EnvValue "CLOUD_SERVICE_HOST" "127.0.0.1"
$CloudPort = [int](Get-EnvValue "CLOUD_SERVICE_PORT" "8004")
$Edge01Port = [int](Get-EnvValue "EDGE_01_HOST_PORT" "8001")
$Edge02Port = [int](Get-EnvValue "EDGE_02_HOST_PORT" "8002")
$Edge01NodeId = Get-EnvValue "EDGE_01_NODE_ID" "edge_01"
$Edge02NodeId = Get-EnvValue "EDGE_02_NODE_ID" "edge_02"
$ToxiproxyHost = Get-EnvValue "TOXIPROXY_API_HOST" $HealthHost
$ToxiproxyPort = [int](Get-EnvValue "TOXIPROXY_API_PORT" "8474")
$MqttHost = Get-EnvValue "MQTT_BROKER_HOST" $HealthHost
$MqttPort = [int](Get-EnvValue "MQTT_BROKER_PORT" "1883")
$NetworkApiHost = Get-EnvValue "NETWORK_API_HOST" $HealthHost
$NetworkApiPort = [int](Get-EnvValue "NETWORK_API_HOST_PORT" "8090")
$EdgeLlmHost = Get-EnvValue "EDGE_SUGGESTION_LLM_HOST" $HealthHost
$EdgeLlmPort = [int](Get-EnvValue "EDGE_SUGGESTION_LLM_PORT" "8005")
$CloudLlmHost = Get-EnvValue "CLOUD_MODEL_UPDATE_LLM_HOST" $HealthHost
$CloudLlmPort = [int](Get-EnvValue "CLOUD_MODEL_UPDATE_LLM_PORT" "6006")
$LlmBindHost = Get-EnvValue "LLM_SERVICE_BIND_HOST" "127.0.0.1"
$CloudSchedulerUrl = Get-EnvValue "CLOUD_SCHEDULER_SERVICE_BASE_URL" "http://$HealthHost`:18045"
$EdgeLlmBaseUrl = Get-EnvValue "EDGE_SUGGESTION_LLM_BASE_URL" "http://host.docker.internal:8005"
$VllmUrl = Get-EnvValue "VLLM_URL" "http://127.0.0.1:6006/v1/chat/completions"
$EdgeLlmModelPath = Resolve-DeploymentPath (Get-EnvValue "EDGE_SUGGESTION_LLM_MODEL_PATH" "models\qwen2.5-0.5b-instruct-q3_k_m.gguf") $LLM_DIR
$CloudLlmModelPath = Resolve-DeploymentPath (Get-EnvValue "CLOUD_MODEL_UPDATE_LLM_MODEL_PATH" "models\qwen2.5-3b-instruct-q4_k_m.gguf") $LLM_DIR
$PollIntervalSeconds = 2

Assert-UrlPort "EDGE_SUGGESTION_LLM_BASE_URL" $EdgeLlmBaseUrl $EdgeLlmPort
Assert-UrlPort "VLLM_URL" $VllmUrl $CloudLlmPort

# 通用 conda 激活引导：不依赖用户是否执行过 conda init powershell，
# 只要 conda 在 PATH 中即可（前置检查已保证），队友机器同样适用。
# 用法：在子进程命令前拼接本前缀，即可用标准 "conda activate moment"。
$CondaActivatePrefix = "conda shell.powershell hook | Out-String | Invoke-Expression; conda activate '$CondaEnvName'; "

if ($CheckConfig) {
    Write-Host "=== Read-only deployment preflight ==="
    Write-Host "ProjectRoot=$ProjectRoot"
    Write-Host "CondaEnv=$CondaEnvName"
    Write-Host "Scheduler=http://$SchedulerHost`:$SchedulerPort"
    Write-Host "Cloud=http://$CloudHost`:$CloudPort"
    Write-Host "Edges=$Edge01NodeId`:$Edge01Port,$Edge02NodeId`:$Edge02Port"
    Write-Host "NetworkApi=http://$NetworkApiHost`:$NetworkApiPort"
    Write-Host "LlamaCpp=$LLM_DIR"
}

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
        if ($CheckConfig) {
            Write-Host "  EDGE_CONTROL_SHARED_SECRET must contain at least 32 bytes (no secret was created during preflight)"
            exit 1
        }
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

Write-Host "[Check] External Docker network network_simulator_default ..."
$null = docker network inspect network_simulator_default 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Network missing. Create it with: docker network create network_simulator_default"
    exit 1
}
Write-Host "  External network OK"

Write-Host "[Check] Conda $CondaEnvName env ..."
$condaEnvs = conda env list 2>&1
if ($LASTEXITCODE -ne 0) { Write-Host "  Could not query Conda environments"; exit 1 }
$escapedCondaEnv = [regex]::Escape($CondaEnvName)
if (-not ($condaEnvs -match "^\s*$escapedCondaEnv\s")) { Write-Host "  $CondaEnvName env missing, create it first"; exit 1 }
Write-Host "  $CondaEnvName OK"

Write-Host "[Check] Python in Conda environment ..."
$pythonVersion = conda run -n $CondaEnvName python --version 2>&1
if ($LASTEXITCODE -ne 0) { Write-Host "  Python is unavailable in Conda env $CondaEnvName"; exit 1 }
Write-Host "  $pythonVersion"

Write-Host "[Check] MOMENT model ..."
$momentCheckpoint = Resolve-DeploymentPath (Get-EnvValue "CLOUD_MOMENT_CHECKPOINT_PATH" "model_assets\moment\releases\moment-scl05-final\best_model.pt") $CloudEdge
$momentConditionNorm = Resolve-DeploymentPath (Get-EnvValue "CLOUD_MOMENT_CONDITION_NORM_PATH" "model_assets\moment\releases\moment-scl05-final\condition_norm.json") $CloudEdge
$momentPretrained = Resolve-DeploymentPath (Get-EnvValue "CLOUD_MOMENT_PRETRAINED_PATH" "model_assets\moment\pretrained\MOMENT-1-small") $CloudEdge
$momentDeployment = Resolve-DeploymentPath (Get-EnvValue "CLOUD_MOMENT_DEPLOYMENT_DIR" "model_assets\moment\releases\moment-scl05-final") $CloudEdge
$requiredMomentFiles = @(
    $momentCheckpoint,
    $momentConditionNorm,
    (Join-Path $momentDeployment "moment_model.py"),
    (Join-Path $momentPretrained "config.json"),
    (Join-Path $momentPretrained "model.safetensors")
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

Write-Host "[Check] Docker Compose interpolation ..."
Push-Location $EdgeService
try {
    docker compose -f compose.multi-edge.yml config --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  compose.multi-edge.yml interpolation failed"
        exit 1
    }
} finally {
    Pop-Location
}
Write-Host "  Compose OK"

Write-Host "[Check] Host ports $Edge01Port/$Edge02Port must be free for the Edge containers ..."
$occupiedPorts = @()
foreach ($port in $Edge01Port, $Edge02Port) {
    if (Test-TcpPort $HealthHost $port) { $occupiedPorts += $port }
}
if ($occupiedPorts.Count -gt 0) {
    if ($CheckConfig) {
        Write-Host "  Ports $($occupiedPorts -join ', ') are occupied; read-only preflight will not stop containers"
        exit 1
    }
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
    foreach ($port in $Edge01Port, $Edge02Port) {
        if (Test-TcpPort $HealthHost $port) { $occupiedPorts += $port }
    }
}
if ($occupiedPorts.Count -gt 0) {
    Write-Host "  Ports $($occupiedPorts -join ', ') still in use (not from Edge containers). Stop the occupying process first."
    exit 1
}
Write-Host "  Ports free"

if (-not $SkipLLM) {
    $lb = Join-Path $LLM_DIR "llama-server.exe"
    $lm = $EdgeLlmModelPath
    $cm = $CloudLlmModelPath
    if (-not (Test-Path $lb) -or -not (Test-Path $lm) -or -not (Test-Path $cm)) {
        Write-Host "  LLM not fully deployed (need llama-server.exe + 0.5B + 3B models), use -SkipLLM to skip"
        exit 1
    }
    Write-Host "[Check] LLM OK (0.5B suggestion + 3B cloud model-update)"
}

if ($CheckConfig) {
    Write-Host "=== Read-only deployment preflight passed; no files, processes, or containers were changed ==="
    exit 0
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

$defaultRequiredProxies = @(
    "edge_01__to__scheduler__http",
    "edge_01__to__cloud__http",
    "scheduler__to__edge_01__http",
    "edge_02__to__scheduler__http",
    "edge_02__to__cloud__http",
    "scheduler__to__edge_02__http",
    "cloud__to__scheduler__http"
)
$requiredProxiesJson = Get-EnvValue "NETWORK_REQUIRED_PROXIES_JSON" ""
if ($requiredProxiesJson) {
    try {
        $requiredProxies = @($requiredProxiesJson | ConvertFrom-Json)
    } catch {
        Write-Host "  NETWORK_REQUIRED_PROXIES_JSON must be a JSON array"
        exit 1
    }
} else {
    $requiredProxies = $defaultRequiredProxies
}
# toxiproxy API 仅放行其官方客户端 UA，用于绕过 PowerShell 默认 UA 的 403。
$ToxiproxyUserAgent = "toxiproxy-http-client/2.12.0"

$stage1 = $true
if (-not (Wait-Gate "Toxiproxy API ($ToxiproxyPort)" { $null -ne (Get-Json "http://$ToxiproxyHost`:$ToxiproxyPort/proxies" -UserAgent $ToxiproxyUserAgent) })) { $stage1 = $false }
if ($stage1 -and -not (Wait-Gate "Toxiproxy proxies present" {
    $proxies = Get-Json "http://$ToxiproxyHost`:$ToxiproxyPort/proxies" -UserAgent $ToxiproxyUserAgent
    if ($null -eq $proxies) { return $false }
    foreach ($name in $requiredProxies) {
        if ($null -eq $proxies.PSObject.Properties[$name]) { return $false }
    }
    return $true
})) { $stage1 = $false }
if ($stage1 -and -not (Wait-Gate "MQTT broker ($MqttPort)" { Test-TcpPort $MqttHost $MqttPort })) { $stage1 = $false }
if ($stage1 -and -not (Wait-Gate "Network controller ($NetworkApiPort) reachable" {
    # 注意：此处只要求 controller 可达，不要求 status=="ok"。
    # controller 的 scheduler_reporter 每 1s 探测 Scheduler(host:8003)，而 Scheduler
    # 要到 Stage 2 才启动；若在此要求 status=="ok"，会因 Scheduler 未起而永远
    # degraded，形成 Stage 1 死锁（Scheduler 永远无法被拉起）。故控制器可达即可，
    # 待 Stage 2 的 Scheduler 起来后其 reporter 自然转 healthy。
    $controller = Get-Json "http://$NetworkApiHost`:$NetworkApiPort/health"
    $null -ne $controller
})) { $stage1 = $false }
if (-not $stage1) { Show-NetSimDiagnostics; exit 1 }

# ---------- Stage 2: host Scheduler + Cloud ----------
Write-Host "`n========== Stage 2/4: Host Scheduler ($SchedulerPort) + Cloud ($CloudPort) =========="
$schCmd = "Set-Location '$CloudEdge'; $CondaActivatePrefix python -m uvicorn scheduler.api:app --host $SchedulerHost --port $SchedulerPort"
Start-Process powershell -ArgumentList "-NoExit","-Command",$schCmd

$cloudCmd = "Set-Location '$CloudEdge'; `$env:CLOUD_BACKEND='$(Get-EnvValue "CLOUD_BACKEND" "moment_light_adapt")'; `$env:SCHEDULER_SERVICE_BASE_URL='$CloudSchedulerUrl'; $CondaActivatePrefix python -m uvicorn cloud_service.app:app --host $CloudHost --port $CloudPort"
Start-Process powershell -ArgumentList "-NoExit","-Command",$cloudCmd

$stage2 = $true
if (-not (Wait-Gate "Scheduler /health ($SchedulerPort)" {
    $scheduler = Get-Json "http://$HealthHost`:$SchedulerPort/health"
    $null -ne $scheduler -and $scheduler.status -eq "ok"
})) { $stage2 = $false }
if ($stage2 -and -not (Wait-Gate "Cloud /health ($CloudPort, backend loaded)" {
    # Cloud /health 仅在 MOMENT 模型加载完成后返回 200。
    $cloud = Get-Json "http://$HealthHost`:$CloudPort/health"
    $null -ne $cloud -and $cloud.status -eq "ok"
})) { $stage2 = $false }
if (-not $stage2) {
    Write-Host "  Check the Scheduler / Cloud PowerShell windows above."
    exit 1
}

# ---------- Stage 3: LLM services ----------
if (-not $SkipLLM) {
    Write-Host "`n========== Stage 3/4: LLM services (edge $EdgeLlmPort + cloud $CloudLlmPort) =========="
    # 边缘建议 LLM（0.5B）：Edge 容器经 host.docker.internal:8005 调用。
    $llmCmd = "Set-Location '$LLM_DIR'; .\llama-server.exe --model '$EdgeLlmModelPath' --host $LlmBindHost --port $EdgeLlmPort --ctx-size 2048 --n-gpu-layers 99"
    Start-Process powershell -ArgumentList "-NoExit","-Command",$llmCmd
    # 云端模型更新 LLM（3B）：Cloud 模型更新建议书使用（VLLM_URL 默认 6006）。
    $cloudLlmCmd = "Set-Location '$LLM_DIR'; .\llama-server.exe --model '$CloudLlmModelPath' --host $LlmBindHost --port $CloudLlmPort --ctx-size 4096 --n-gpu-layers 99"
    Start-Process powershell -ArgumentList "-NoExit","-Command",$cloudLlmCmd
    $stage3 = $true
    if (-not (Wait-Gate "Edge suggestion LLM /v1/models ($EdgeLlmPort)" {
        $models = Get-Json "http://$EdgeLlmHost`:$EdgeLlmPort/v1/models"
        $null -ne $models -and $models.data.Count -gt 0
    })) { $stage3 = $false }
    if ($stage3 -and -not (Wait-Gate "Cloud model-update LLM /v1/models ($CloudLlmPort)" {
        $models = Get-Json "http://$CloudLlmHost`:$CloudLlmPort/v1/models"
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
if (-not (Wait-Gate "edge_01 /health/ready ($Edge01Port)" {
    $ready = Get-Json "http://$HealthHost`:$Edge01Port/health/ready"
    $null -ne $ready -and $ready.ready -eq $true
})) { $stage4 = $false }
if ($stage4 -and -not (Wait-Gate "edge_02 /health/ready ($Edge02Port)" {
    $ready = Get-Json "http://$HealthHost`:$Edge02Port/health/ready"
    $null -ne $ready -and $ready.ready -eq $true
})) { $stage4 = $false }
if (-not $stage4) { Show-EdgeDiagnostics; exit 1 }

$edge01Health = Get-Json "http://$HealthHost`:$Edge01Port/health"
$edge02Health = Get-Json "http://$HealthHost`:$Edge02Port/health"
Write-Host ("  edge_01 node_id={0} mqtt_connected={1}" -f $edge01Health.node_id, $edge01Health.mqtt_connected)
Write-Host ("  edge_02 node_id={0} mqtt_connected={1}" -f $edge02Health.node_id, $edge02Health.mqtt_connected)
if ($edge01Health.node_id -ne $Edge01NodeId -or $edge02Health.node_id -ne $Edge02NodeId -or
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
