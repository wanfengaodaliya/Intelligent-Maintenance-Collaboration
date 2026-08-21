param(
    [string]$VllmUrl = "http://127.0.0.1:6006/v1/chat/completions",
    [string]$ModelName = "qwen3.5-2b-local",
    [int]$Port = 6008,
    [int]$TimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"
$projectDirectory = Split-Path -Parent $PSScriptRoot
$pythonCandidates = @(
    (Join-Path $projectDirectory "..\.venv\Scripts\python.exe"),
    (Join-Path $projectDirectory "..\..\.venv\Scripts\python.exe"),
    (Join-Path $projectDirectory "..\..\..\.venv\Scripts\python.exe")
)
$python = $pythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $python) {
    throw "Project virtual environment was not found: $($pythonCandidates -join '; ')"
}

# 正式诊断后端为 moment_light_adapt；vLLM（6006）只服务模型更新建议书，
# 相关变量保留供该路径使用。
$env:CLOUD_BACKEND = "moment_light_adapt"
$env:VLLM_URL = $VllmUrl
$env:VLLM_MODEL_NAME = $ModelName
$env:VLLM_TIMEOUT_SECONDS = "$TimeoutSeconds"
$env:CLOUD_SERVICE_PORT = "$Port"

Set-Location $projectDirectory
& $python -m uvicorn cloud_service.app:app --host 127.0.0.1 --port $Port
