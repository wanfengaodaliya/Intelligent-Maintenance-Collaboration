$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..\..")).Path
$runtimeRoot = Join-Path $projectRoot "cloud_edge_project\edge_service\model_training\bearing_random_forest\var\runtime"
$paths = @{
    TEMP = Join-Path $runtimeRoot "temp"
    TMP = Join-Path $runtimeRoot "temp"
    PIP_CACHE_DIR = Join-Path $runtimeRoot "pip-cache"
    PYTHONPYCACHEPREFIX = Join-Path $runtimeRoot "pycache"
    MPLCONFIGDIR = Join-Path $runtimeRoot "matplotlib"
}

foreach ($entry in $paths.GetEnumerator()) {
    $resolvedParent = [System.IO.Path]::GetFullPath($entry.Value)
    if ($resolvedParent.StartsWith("C:\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝使用 C 盘可写路径: $resolvedParent"
    }
    New-Item -ItemType Directory -Force -Path $resolvedParent | Out-Null
    Set-Item -Path "Env:$($entry.Key)" -Value $resolvedParent
}

$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
$env:PYTHONNOUSERSITE = "1"

Write-Host "随机森林训练可写路径已固定到 D 盘: $runtimeRoot"
