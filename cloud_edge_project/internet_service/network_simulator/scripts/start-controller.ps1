param(
    [string]$ConfigDir = (Join-Path $PSScriptRoot "..\config"),
    [string]$LogDir = (Join-Path $PSScriptRoot "..\logs"),
    [string]$PythonBin = "python"
)

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$env:NETWORK_CONFIG_DIR = [System.IO.Path]::GetFullPath($ConfigDir)
$env:NETWORK_LOG_DIR = [System.IO.Path]::GetFullPath($LogDir)

try {
    $pythonCommand = Get-Command $PythonBin -ErrorAction Stop
    Push-Location $projectRoot
    try {
        & $pythonCommand.Source -m controller.main
        $controllerExitCode = $LASTEXITCODE
        if ($null -eq $controllerExitCode) {
            $controllerExitCode = 1
        }
    }
    finally {
        Pop-Location
    }
}
catch {
    Write-Error "Unable to start network controller: $($_.Exception.GetType().Name)"
    $controllerExitCode = 1
}

exit $controllerExitCode
