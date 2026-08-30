param(
    [string]$ProjectRoot,
    [string]$EnvFile,
    [switch]$CheckConfig,
    [switch]$SkipLLM,
    [switch]$SkipCloudUpdateLLM,
    # 默认使用已存在的 cloud-edge/edge-service:latest，绝不 build/pull。
    # 仅在显式传入 -RebuildEdgeImage 时，Stage 4 才以 --build 重新构建镜像。
    [switch]$RebuildEdgeImage,
    # 默认在 Stage 2 前发现 Scheduler/Cloud/Summary 端口被占用就报错并退出，
    # 不清杀任何无法确认归属的进程。显式传入此开关时，才允许清理确实属于
    # 本项目的旧 uvicorn 进程后重启宿主机服务。
    [switch]$RestartHostServices,
    [int]$EdgeModelInferenceWorkers = 2,
    [int]$EdgeModelQueueCapacity = 160,
    [int]$EdgeModelQueueWaitMs = 15000,
    [int]$EdgeModelTotalTimeoutMs = 20000,
    [int]$SummaryWindowTimeoutSeconds = 40,
    [int]$ExpectedPacketCount = 80,
    # 当前正式合同固定为双 Sender / 双轴承；Summary 和云端仲裁均只接受 bearing_01/02。
    [ValidateSet(2)]
    [int]$SenderCount = 2,
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
#   Stage 4  edge_01 + edge_02 容器（compose.multi-edge.yml，默认 --no-build 复用
#            已构建镜像；仅 -RebuildEdgeImage 时 --build），轮询 /health/ready
#            （Docker HEALTHCHECK 仅代表 liveness）。
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
$NetworkSimNetwork = Get-EnvValue "NETWORK_SIM_NETWORK" "network_simulator_default"
$LLM_DIR = Get-EnvValue "LLAMA_CPP_DIR" (Join-Path $ProjectRoot "tools\llama.cpp")
if (-not [System.IO.Path]::IsPathRooted($LLM_DIR)) { $LLM_DIR = Join-Path $ProjectRoot $LLM_DIR }
$CondaEnvName = Get-EnvValue "PROJECT_CONDA_ENV" "moment"
$PythonExecutable = Get-EnvValue "PROJECT_PYTHON_EXECUTABLE" ""
if (-not [string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $PythonExecutable = Resolve-DeploymentPath $PythonExecutable $ProjectRoot
}
$HealthHost = Get-EnvValue "PROJECT_HEALTH_HOST" "127.0.0.1"
$SchedulerHost = Get-EnvValue "SCHEDULER_SERVICE_HOST" "127.0.0.1"
$SchedulerPort = [int](Get-EnvValue "SCHEDULER_SERVICE_PORT" "8003")
$CloudHost = Get-EnvValue "CLOUD_SERVICE_HOST" "127.0.0.1"
$CloudPort = [int](Get-EnvValue "CLOUD_SERVICE_PORT" "8004")
$SummaryHost = Get-EnvValue "SUMMARY_SERVICE_HOST" "127.0.0.1"
$SummaryPort = [int](Get-EnvValue "SUMMARY_SERVICE_PORT" "8006")
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
$SummaryLlmHost = Get-EnvValue "SUMMARY_SUGGESTION_LLM_HOST" $HealthHost
$SummaryLlmPort = [int](Get-EnvValue "SUMMARY_SUGGESTION_LLM_PORT" "8005")
$CloudLlmHost = Get-EnvValue "CLOUD_MODEL_UPDATE_LLM_HOST" $HealthHost
$CloudLlmPort = [int](Get-EnvValue "CLOUD_MODEL_UPDATE_LLM_PORT" "6006")
$LlmBindHost = Get-EnvValue "LLM_SERVICE_BIND_HOST" "127.0.0.1"
$CloudSchedulerUrl = Get-EnvValue "CLOUD_SCHEDULER_SERVICE_BASE_URL" "http://$HealthHost`:18045"
$SummaryLlmBaseUrl = Get-EnvValue "SUMMARY_SUGGESTION_LLM_BASE_URL" "http://$SummaryLlmHost`:$SummaryLlmPort"
$VllmUrl = Get-EnvValue "VLLM_URL" "http://127.0.0.1:6006/v1/chat/completions"
$SummaryLlmModelPath = Resolve-DeploymentPath (Get-EnvValue "SUMMARY_SUGGESTION_LLM_MODEL_PATH" "models\qwen2.5-0.5b-instruct-q3_k_m.gguf") $LLM_DIR
$CloudLlmModelPath = Resolve-DeploymentPath (Get-EnvValue "CLOUD_MODEL_UPDATE_LLM_MODEL_PATH" "models\qwen2.5-3b-instruct-q4_k_m.gguf") $LLM_DIR
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
if (-not $CheckConfig) {
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
}

Assert-UrlPort "SUMMARY_SUGGESTION_LLM_BASE_URL" $SummaryLlmBaseUrl $SummaryLlmPort
Assert-UrlPort "VLLM_URL" $VllmUrl $CloudLlmPort

# 通用 conda 激活引导：不依赖用户是否执行过 conda init powershell，
# 只要 conda 在 PATH 中即可（前置检查已保证），队友机器同样适用。
# 用法：在子进程命令前拼接本前缀，即可用标准 "conda activate moment"。
$CondaActivatePrefix = "conda shell.powershell hook | Out-String | Invoke-Expression; conda activate '$CondaEnvName'; "
if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $PythonLaunchPrefix = "$CondaActivatePrefix python"
} else {
    $escapedPythonExecutable = $PythonExecutable.Replace("'", "''")
    $PythonLaunchPrefix = "& '$escapedPythonExecutable'"
}

if ($CheckConfig) {
    Write-Host "=== Read-only deployment preflight ==="
    Write-Host "ProjectRoot=$ProjectRoot"
    if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
        Write-Host "CondaEnv=$CondaEnvName"
    } else {
        Write-Host "PythonExecutable=$PythonExecutable"
    }
    Write-Host "Scheduler=http://$SchedulerHost`:$SchedulerPort"
    Write-Host "Cloud=http://$CloudHost`:$CloudPort"
    Write-Host "Summary=http://$SummaryHost`:$SummaryPort"
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
$edgeComposeMode = if ($RebuildEdgeImage) { " --build (RebuildEdgeImage)" } else { " --no-build" }
Write-Host "=== Edge mode: containers only (compose.multi-edge.yml up -d)$edgeComposeMode ==="
Write-Host "=== Sender mode: $SenderCount senders ==="
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

# 查询占用某个 TCP 监听端口的进程 PID 与进程名；无占用时返回空。
# 注意：这里只读取归属信息，绝不主动杀死进程。
function Get-PortOwner {
    param([string]$HostName, [int]$Port)
    # 必须查询该端口的所有监听地址；服务可能绑定 0.0.0.0，而健康检查访问 127.0.0.1。
    $listeners = Get-NetTCPConnection -State Listen -LocalPort $Port `
        -ErrorAction SilentlyContinue
    foreach ($conn in $listeners) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($null -ne $proc) {
            return [PSCustomObject]@{
                Pid = $proc.Id
                Name = $proc.ProcessName
                Path = $proc.Path
            }
        }
    }
    return $null
}

# 判断一个进程是否确实属于本项目宿主机服务，避免误杀他人进程。
# uvicorn 监听者是启动 PowerShell 的子进程，因此同时校验模块与父子关系。
function Test-OwnedHostProcess {
    param(
        [int]$ProcessId,
        [string]$Module,
        [int]$ExpectedParentPid = 0
    )
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" `
            -ErrorAction SilentlyContinue
        $cmd = $process.CommandLine
    } catch {
        $process = $null
        $cmd = $null
    }
    if ([string]::IsNullOrWhiteSpace($cmd)) { return $false }
    $uvicornTarget = '(?i)(^|\s)-m\s+uvicorn\s+' + [regex]::Escape($Module) + ':app(\s|$)'
    if ($cmd -notmatch $uvicornTarget) { return $false }

    # Windows venv 启动器可能形成 PowerShell -> venv python -> runtime python，
    # 所以在有限层级内查找启动 PowerShell，而不是只比较直接父进程。
    $ancestorPid = [int]$process.ParentProcessId
    $expectedProjectLocation = "Set-Location '$CloudEdge'"
    for ($depth = 0; $depth -lt 6 -and $ancestorPid -gt 0; $depth++) {
        if ($ExpectedParentPid -gt 0 -and $ancestorPid -eq $ExpectedParentPid) {
            return $true
        }
        try {
            $ancestor = Get-CimInstance Win32_Process -Filter "ProcessId=$ancestorPid" `
                -ErrorAction SilentlyContinue
        } catch { $ancestor = $null }
        if ($null -eq $ancestor) { break }

        $ancestorCmd = $ancestor.CommandLine
        if ($ExpectedParentPid -le 0 `
            -and $ancestor.Name -in @('powershell.exe', 'pwsh.exe') `
            -and -not [string]::IsNullOrWhiteSpace($ancestorCmd) `
            -and $ancestorCmd.IndexOf($expectedProjectLocation, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 `
            -and $ancestorCmd -match $uvicornTarget) {
            return $true
        }
        $ancestorPid = [int]$ancestor.ParentProcessId
    }
    return $false
}

# 一个主机端口的健康门（Problem 2）：
#   默认（未传 -RestartHostServices）：端口一旦被任何进程占用就报错并退出，
#   输出 PID/进程名/路径与安全提示，绝不静默信任占用者。
#   显式 -RestartHostServices：仅在占用进程确实属于本项目时才允许停止并返回 true；
#   无法确认归属时仍报错退出。
function Assert-HostPortFree {
    param([string]$ServiceName, [string]$HostName, [int]$Port, [string]$Module)
    $owner = Get-PortOwner -HostName $HostName -Port $Port
    if ($null -eq $owner) { return }
    if ($CheckConfig) {
        Write-Host "  $ServiceName port $Port is occupied by PID $($owner.Pid)" +
            " ($($owner.Name)); read-only preflight will not stop it"
        exit 1
    }
    if ($RestartHostServices) {
        if (Test-OwnedHostProcess -ProcessId $owner.Pid -Module $Module) {
            Write-Host "  $ServiceName port $Port occupied by our own $($owner.Name)" +
                " PID $($owner.Pid)); stopping it (RestartHostServices) ..."
            Stop-Process -Id $owner.Pid -Force -ErrorAction SilentlyContinue
            return
        }
        Write-Host "  $ServiceName port $Port is occupied by PID $($owner.Pid)" +
            " ($($owner.Name)) at $($owner.Path); it does not belong to this project."
        Write-Host ("  Safe handling: close the process manually, or run: " +
            "Stop-Process -Id $($owner.Pid) -Force")
        exit 1
    }
    Write-Host "  $ServiceName port $Port is already in use."
    Write-Host "  Occupied by PID $($owner.Pid) - $($owner.Name) ($($owner.Path))"
    Write-Host "  This may be a leftover host process from a previous run."
    Write-Host "  The script will NOT kill processes it cannot confirm as this project's."
    Write-Host "  Safe options:"
    Write-Host "    - Stop the occupying process manually (Stop-Process -Id $($owner.Pid) -Force)"
    Write-Host "    - Re-run with -RestartHostServices to stop this project's own stale services"
    exit 1
}

# ---------- Pre-checks ----------
Write-Host "[Check] Docker Desktop ..."
$dockerInfo = docker info 2>&1
if ($LASTEXITCODE -ne 0) { Write-Host "  Docker not running, start Docker Desktop first"; exit 1 }
Write-Host "  Docker OK"

Write-Host "[Check] External Docker network $NetworkSimNetwork ..."
$null = docker network inspect $NetworkSimNetwork 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Network missing. Create it with: docker network create $NetworkSimNetwork"
    exit 1
}
Write-Host "  External network OK"

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    Write-Host "[Check] Conda $CondaEnvName env ..."
    $condaEnvs = conda env list 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Host "  Could not query Conda environments"; exit 1 }
    $escapedCondaEnv = [regex]::Escape($CondaEnvName)
    if (-not ($condaEnvs -match "^\s*$escapedCondaEnv\s")) { Write-Host "  $CondaEnvName env missing, create it first"; exit 1 }
    Write-Host "  $CondaEnvName OK"

    Write-Host "[Check] Python in Conda environment ..."
    $pythonVersion = conda run -n $CondaEnvName python --version 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Host "  Python is unavailable in Conda env $CondaEnvName"; exit 1 }
} else {
    Write-Host "[Check] Configured Python executable ..."
    if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
        Write-Host "  Python executable missing: $PythonExecutable"
        exit 1
    }
    $pythonVersion = & $PythonExecutable --version 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Host "  Configured Python is unavailable: $PythonExecutable"; exit 1 }
}
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
    # 镜像不存在时：默认绝不自动构建。给出清晰、可复制的构建命令后退出。
    Write-Host "  Image not found. It must be built once before first startup."
    if ($RebuildEdgeImage) {
        Write-Host "  Building it now because -RebuildEdgeImage was passed ..."
    } else {
        Write-Host "  Build it manually first (this also downloads PyTorch/CUDA deps):"
        Write-Host "    cd $EdgeService ; docker compose -f compose.multi-edge.yml build"
        Write-Host "  Then re-run start_project.ps1. Or pass -RebuildEdgeImage to build now."
        exit 1
    }
} else {
    Write-Host "  Image present; using --no-build unless -RebuildEdgeImage is passed."
}

# 镜像版本 vs 当前源码版本的一致性提示（不静默、不自动构建）。
if ($LASTEXITCODE -eq 0 -and -not $RebuildEdgeImage) {
    try {
        $imageRevision = ((docker image inspect cloud-edge/edge-service:latest `
            --format '{{ index .Config.Env }}' 2>$null) | Out-String)
        $imageRevMatch = [regex]::Match($imageRevision, 'EDGE_BUILD_REVISION=([^\s"\]]*)')
        $imageRev = if ($imageRevMatch.Success) { $imageRevMatch.Groups[1].Value } else { "unknown" }
    } catch { $imageRev = "unknown" }
    if ($imageRev -ne $gitRevision -and $imageRev -ne "unknown") {
        Write-Host "  [warn] Edge image EDGE_BUILD_REVISION = $imageRev, source revision = $gitRevision."
        Write-Host "  The image may be out of date vs current source. Rebuild deliberately with:"
        Write-Host "    cd $EdgeService ; docker compose -f compose.multi-edge.yml build"
        Write-Host "    or re-run: .\start_project.ps1 -RebuildEdgeImage"
    }
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
    $lm = $SummaryLlmModelPath
    $cm = $CloudLlmModelPath
    if (-not (Test-Path $lb) -or -not (Test-Path $lm)) {
        Write-Host "  Summary suggestion LLM not deployed (need llama-server.exe + 0.5B model)."
        Write-Host "  This is OPTIONAL: run with -SkipLLM to start the core link in template mode"
        Write-Host "  (summary suggestions fall back to fixed Chinese templates), e.g.:"
        Write-Host "    .\start_project.ps1 -SkipLLM"
        exit 1
    }
    if (-not $SkipCloudUpdateLLM -and -not (Test-Path $cm)) {
        Write-Host "  Cloud model-update LLM not deployed (need 3B model)."
        Write-Host "  This is OPTIONAL: add -SkipCloudUpdateLLM to skip only it (the core link"
        Write-Host "  still runs; model-update proposals fall back to templates), e.g.:"
        Write-Host "    .\start_project.ps1 -SkipLLM -SkipCloudUpdateLLM"
        exit 1
    }
    Write-Host "[Check] Summary suggestion LLM OK (0.5B)"
    if (-not $SkipCloudUpdateLLM) {
        Write-Host "[Check] Cloud model-update LLM OK (3B)"
    }
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

# ---------- Stage 2: host Scheduler + Cloud + Summary ----------
Write-Host "`n========== Stage 2/4: Scheduler ($SchedulerPort) + Cloud ($CloudPort) + Summary ($SummaryPort) =========="

# 启动前先确认三个宿主机端口空闲（Problem 2）：默认发现被占用就报错退出，
# 绝不清杀任何无法确认归属的进程；-RestartHostServices 时才清理本项目的旧进程。
Assert-HostPortFree -ServiceName "Scheduler" -HostName $SchedulerHost -Port $SchedulerPort -Module "scheduler.api"
Assert-HostPortFree -ServiceName "Cloud" -HostName $CloudHost -Port $CloudPort -Module "cloud_service.app"
Assert-HostPortFree -ServiceName "Summary" -HostName $SummaryHost -Port $SummaryPort -Module "summary_service.app"

# 记录本次启动的宿主机服务进程，供健康门确认新进程未提前退出。
$script:StartedHostPids = @{}

# Scheduler 使用实验独立的 SQLite：持久 scheduler.db 会跨实验残留 task_id/device_id，
# 造成 TASK_ID_CONFLICT 且污染 stability_score（其读取历史执行记录）。每次实验指向
# 实验 data 子目录，与 Cloud/Summary 的隔离策略一致。
$schedulerDb = Join-Path $ExperimentData "scheduler.db"
$schCmd = "Set-Location '$CloudEdge'; `$env:SCHEDULER_EXPECTED_PACKET_COUNT='$ExpectedPacketCount'; `$env:SCHEDULER_DB_PATH='$schedulerDb'; $PythonLaunchPrefix -m uvicorn scheduler.api:app --host $SchedulerHost --port $SchedulerPort"
$script:StartedHostPids["Scheduler"] = (Start-Process powershell -ArgumentList "-NoExit","-Command",$schCmd -PassThru).Id

$cloudDb = Join-Path $ExperimentData "cloud_review.db"
$cloudBackend = Get-EnvValue "CLOUD_BACKEND" "moment_light_adapt"
$cloudMomentDevice = Get-EnvValue "CLOUD_MOMENT_DEVICE" "auto"
$cloudCmd = "Set-Location '$CloudEdge'; `$env:CLOUD_BACKEND='$cloudBackend'; `$env:CLOUD_MOMENT_DEVICE='$cloudMomentDevice'; `$env:CLOUD_REVIEW_DB_PATH='$cloudDb'; `$env:SCHEDULER_SERVICE_BASE_URL='$CloudSchedulerUrl'; $PythonLaunchPrefix -m uvicorn cloud_service.app:app --host $CloudHost --port $CloudPort"
$script:StartedHostPids["Cloud"] = (Start-Process powershell -ArgumentList "-NoExit","-Command",$cloudCmd -PassThru).Id

$summaryDb = Join-Path $ExperimentData "summary_service.db"
$summaryLlmEnabled = if ($SkipLLM) { "false" } else { "true" }
$summaryExpectedBearingIds = "bearing_01,bearing_02"
$summaryCmd = "Set-Location '$CloudEdge'; `$env:SUMMARY_DATABASE_PATH='$summaryDb'; `$env:SUMMARY_WINDOW_TIMEOUT_SECONDS='$SummaryWindowTimeoutSeconds'; `$env:SUMMARY_EXPECTED_BEARING_IDS='$summaryExpectedBearingIds'; `$env:SUMMARY_SUGGESTION_LLM_ENABLED='$summaryLlmEnabled'; `$env:SUMMARY_SUGGESTION_LLM_BASE_URL='$SummaryLlmBaseUrl'; $PythonLaunchPrefix -m uvicorn summary_service.app:app --host $SummaryHost --port $SummaryPort"
$script:StartedHostPids["Summary"] = (Start-Process powershell -ArgumentList "-NoExit","-Command",$summaryCmd -PassThru).Id

# 健康门必须同时满足：HTTP /health 正常 且 端口监听者确为本项目新进程
# （命令行含对应模块）且监听者确为本次启动 PowerShell 的子进程。
function Test-HostedServiceReady {
    param([string]$Name, [string]$HostName, [int]$Port, [string]$Module)
    $listener = Get-PortOwner -HostName $HostName -Port $Port
    if ($null -eq $listener) { return $false }
    $parentPid = $script:StartedHostPids[$Name]
    if ($parentPid -and -not (Get-Process -Id $parentPid -ErrorAction SilentlyContinue)) { return $false }
    if (-not (Test-OwnedHostProcess -ProcessId $listener.Pid -Module $Module `
        -ExpectedParentPid $parentPid)) { return $false }
    $health = Get-Json "http://$HealthHost`:$Port/health"
    if ($null -eq $health -or $health.status -ne "ok") { return $false }
    if ($Name -eq "Summary" -and $health.mqtt_connected -ne $true) { return $false }
    return $true
}

$stage2 = $true
if (-not (Wait-Gate "Scheduler /health ($SchedulerPort)" {
    Test-HostedServiceReady -Name "Scheduler" -HostName $HealthHost -Port $SchedulerPort -Module "scheduler.api"
})) { $stage2 = $false }
if ($stage2 -and -not (Wait-Gate "Cloud /health ($CloudPort, backend loaded)" {
    # Cloud /health 仅在 MOMENT 模型加载完成后返回 200。
    Test-HostedServiceReady -Name "Cloud" -HostName $HealthHost -Port $CloudPort -Module "cloud_service.app"
})) { $stage2 = $false }
if ($stage2 -and -not (Wait-Gate "Summary /health ($SummaryPort, MQTT connected)" {
    Test-HostedServiceReady -Name "Summary" -HostName $HealthHost -Port $SummaryPort -Module "summary_service.app"
})) { $stage2 = $false }
if (-not $stage2) {
    Write-Host "  Check the Scheduler / Cloud / Summary PowerShell windows above."
    Write-Host ("  Started host service PIDs: {0}" -f ($script:StartedHostPids | Out-String).Trim())
    exit 1
}

# ---------- Stage 3: LLM services ----------
if (-not $SkipLLM) {
    Write-Host "`n========== Stage 3/4: LLM services (summary $SummaryLlmPort + cloud $CloudLlmPort) =========="
    # Summary 建议 LLM（0.5B）。
    $llmCmd = "Set-Location '$LLM_DIR'; .\llama-server.exe --model '$SummaryLlmModelPath' --host $LlmBindHost --port $SummaryLlmPort --ctx-size 2048 --n-gpu-layers 99"
    Start-Process powershell -ArgumentList "-NoExit","-Command",$llmCmd
    $stage3 = $true
    if (-not (Wait-Gate "Summary suggestion LLM /v1/models ($SummaryLlmPort)" {
        $models = Get-Json "http://$SummaryLlmHost`:$SummaryLlmPort/v1/models"
        $null -ne $models -and $models.data.Count -gt 0
    })) { $stage3 = $false }
    if (-not $SkipCloudUpdateLLM) {
        # 云端模型更新 LLM（3B）：Cloud 模型更新建议书使用（VLLM_URL 默认 6006）。
        $cloudLlmCmd = "Set-Location '$LLM_DIR'; .\llama-server.exe --model '$CloudLlmModelPath' --host $LlmBindHost --port $CloudLlmPort --ctx-size 4096 --n-gpu-layers 99"
        Start-Process powershell -ArgumentList "-NoExit","-Command",$cloudLlmCmd
        if ($stage3 -and -not (Wait-Gate "Cloud model-update LLM /v1/models ($CloudLlmPort)" {
            $models = Get-Json "http://$CloudLlmHost`:$CloudLlmPort/v1/models"
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
    # Problem 1: 默认复用已构建镜像(--no-build)；仅显式 -RebuildEdgeImage 才构建。
    # 绝不默认 pull，绝不在镜像已存在时重复下载 PyTorch/CUDA。
    $edgeUpArgs = @("-f", "compose.multi-edge.yml", "up", "-d")
    if ($RebuildEdgeImage) {
        $edgeUpArgs += "--build"
    } else {
        $edgeUpArgs += "--no-build"
    }
    docker compose @edgeUpArgs
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
