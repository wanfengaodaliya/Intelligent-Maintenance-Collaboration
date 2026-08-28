[CmdletBinding()]
param(
    [string]$DatasetRoot,
    [switch]$PreviewOnly,
    [switch]$SkipLLM,
    [switch]$SkipCloudUpdateLLM,
    [int]$InterRoundDelaySeconds = 8
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

if ([string]::IsNullOrWhiteSpace($DatasetRoot)) {
    $parentName = -join @(
        [char]0x4e91, [char]0x8fb9, [char]0x534f, [char]0x540c,
        [char]0x9879, [char]0x76ee, [char]0x6587, [char]0x4ef6, [char]0x5939
    )
    $datasetName = -join @(
        [char]0x968f, [char]0x673a, [char]0x68ee, [char]0x6797,
        [char]0x8bca, [char]0x65ad, [char]0x6a21, [char]0x578b,
        [char]0x5206, [char]0x7ec4
    )
    $DatasetRoot = Join-Path (Join-Path "D:\desktop" $parentName) $datasetName
}

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartProjectScript = Join-Path $ProjectRoot "start_project.ps1"
$SenderRoot = Join-Path $ProjectRoot "cloud_edge_project\sender_module"
$FrontendRoot = Join-Path $ProjectRoot "cloud_edge_project\frontend"

function Get-RoundRobinMatFiles {
    param(
        [System.IO.DirectoryInfo[]]$Directories,
        [int]$Count
    )

    $buckets = @()
    foreach ($directory in $Directories) {
        $files = @(Get-ChildItem -LiteralPath $directory.FullName -File -Filter "*.mat" | Sort-Object Name)
        if ($files.Count -gt 0) {
            $conditionGroups = @(
                $files | Group-Object {
                    if ($_.BaseName -match '^(N\d+_M\d+_F\d+)_') {
                        $Matches[1]
                    } else {
                        "unknown"
                    }
                } | Sort-Object Name
            )
            $buckets += [pscustomobject]@{ condition_groups = $conditionGroups }
        }
    }
    if ($buckets.Count -eq 0) {
        throw "No MAT files were found."
    }

    $selected = @()
    for ($index = 0; $index -lt $Count; $index++) {
        $bucketIndex = $index % $buckets.Count
        $groups = @($buckets[$bucketIndex].condition_groups)
        $conditionIndex = $index % $groups.Count
        $conditionFiles = @($groups[$conditionIndex].Group | Sort-Object Name)
        $fileIndex = [Math]::Floor($index / ($buckets.Count * $groups.Count))
        if ($fileIndex -ge $conditionFiles.Count) {
            throw "The dataset does not contain $Count distinct usable samples."
        }
        $selected += $conditionFiles[$fileIndex].FullName
    }
    return @($selected)
}

function New-DemoPairs {
    param([string]$Root)

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "Dataset directory does not exist: $Root"
    }
    $directories = @(Get-ChildItem -LiteralPath $Root -Directory | Sort-Object Name)
    $healthyDirectories = @($directories | Where-Object { $_.Name -match '^K0\d{2}$' })
    $faultDirectories = @($directories | Where-Object { $_.Name -match '^K(?:A|B|I)\d{2}$' })
    if ($healthyDirectories.Count -eq 0 -or $faultDirectories.Count -eq 0) {
        throw "Dataset must contain both K0xx healthy and KA/KB/KI fault folders."
    }

    $healthy = @(Get-RoundRobinMatFiles -Directories $healthyDirectories -Count 10)
    $fault = @(Get-RoundRobinMatFiles -Directories $faultDirectories -Count 10)
    return @(
        [pscustomobject]@{ round = 1; combination = "healthy+healthy"; sender_01 = $healthy[0]; sender_02 = $healthy[1] },
        [pscustomobject]@{ round = 2; combination = "healthy+healthy"; sender_01 = $healthy[2]; sender_02 = $healthy[3] },
        [pscustomobject]@{ round = 3; combination = "healthy+healthy"; sender_01 = $healthy[4]; sender_02 = $healthy[5] },
        [pscustomobject]@{ round = 4; combination = "healthy+fault"; sender_01 = $healthy[6]; sender_02 = $fault[0] },
        [pscustomobject]@{ round = 5; combination = "healthy+fault"; sender_01 = $healthy[7]; sender_02 = $fault[1] },
        [pscustomobject]@{ round = 6; combination = "healthy+fault"; sender_01 = $healthy[8]; sender_02 = $fault[2] },
        [pscustomobject]@{ round = 7; combination = "healthy+fault"; sender_01 = $healthy[9]; sender_02 = $fault[3] },
        [pscustomobject]@{ round = 8; combination = "fault+fault"; sender_01 = $fault[4]; sender_02 = $fault[5] },
        [pscustomobject]@{ round = 9; combination = "fault+fault"; sender_01 = $fault[6]; sender_02 = $fault[7] },
        [pscustomobject]@{ round = 10; combination = "fault+fault"; sender_01 = $fault[8]; sender_02 = $fault[9] }
    )
}

function Test-TcpPort {
    param([int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.ConnectAsync("127.0.0.1", $Port)
        return $pending.Wait(1000) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Test-FrontendGateway {
    try {
        $response = Invoke-WebRequest -UseBasicParsing `
            -Uri "http://127.0.0.1:8088/index.html" `
            -TimeoutSec 2
        $server = [string]$response.Headers["Server"]
        return $response.StatusCode -eq 200 -and $server -like "FrontendGateway/*"
    } catch {
        return $false
    }
}

$pairs = @(New-DemoPairs -Root $DatasetRoot)
if ($PreviewOnly) {
    $pairs | ConvertTo-Json -Depth 3
    exit 0
}

if ($InterRoundDelaySeconds -lt 0) {
    throw "InterRoundDelaySeconds must not be negative."
}
$ReuseFrontend = $false
if (Test-TcpPort -Port 8088) {
    $ReuseFrontend = Test-FrontendGateway
    if (-not $ReuseFrontend) {
        throw "Port 8088 is already in use by another program."
    }
    Write-Host "Reusing the existing frontend on port 8088." -ForegroundColor Green
}

Write-Host "========== Frontend demo: starting the full project ==========" -ForegroundColor Cyan
$startParameters = @{ ProjectRoot = $ProjectRoot; SenderCount = 2 }
if ($SkipLLM) { $startParameters["SkipLLM"] = $true }
if ($SkipCloudUpdateLLM) { $startParameters["SkipCloudUpdateLLM"] = $true }
& $StartProjectScript @startParameters
if (-not $?) {
    throw "Project startup failed; frontend and senders were not started."
}

Write-Host "========== Starting frontend and opening browser ==========" -ForegroundColor Cyan
if (-not $ReuseFrontend) {
    $frontendCommand = "Set-Location '$FrontendRoot'; conda shell.powershell hook | Out-String | Invoke-Expression; conda activate moment; python server.py"
    $frontendProcess = Start-Process powershell -ArgumentList "-NoProfile", "-Command", $frontendCommand -WindowStyle Hidden -PassThru
    $frontendDeadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $frontendDeadline -and -not (Test-TcpPort -Port 8088)) {
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-TcpPort -Port 8088)) {
        if (-not $frontendProcess.HasExited) { $frontendProcess.Kill() }
        throw "Frontend did not listen on port 8088 within 20 seconds."
    }
}
Start-Process "http://127.0.0.1:8088/index.html"
Start-Sleep -Seconds 2

$condaHook = conda shell.powershell hook | Out-String
Invoke-Expression $condaHook
conda activate moment
if ($LASTEXITCODE -ne 0) {
    throw "Could not activate the Conda moment environment."
}

$failedRounds = @()
Push-Location $SenderRoot
try {
    foreach ($pair in $pairs) {
        $sender01Code = Split-Path (Split-Path $pair.sender_01 -Parent) -Leaf
        $sender02Code = Split-Path (Split-Path $pair.sender_02 -Parent) -Leaf
        Write-Host ("`n[{0}/10] {1}: sender_01={2}, sender_02={3}" -f $pair.round, $pair.combination, $sender01Code, $sender02Code) -ForegroundColor Yellow
        & python -m sender --config "config\local.json" `
            --source ("sender_01=" + $pair.sender_01) `
            --source ("sender_02=" + $pair.sender_02)
        if ($LASTEXITCODE -ne 0) {
            $failedRounds += $pair.round
            Write-Host ("  Round {0} did not fully complete; continuing." -f $pair.round) -ForegroundColor Red
        } else {
            Write-Host ("  Round {0} completed." -f $pair.round) -ForegroundColor Green
        }
        if ($pair.round -lt $pairs.Count -and $InterRoundDelaySeconds -gt 0) {
            Write-Host ("  Waiting {0}s for Edge diagnosis and aggregation..." -f $InterRoundDelaySeconds)
            Start-Sleep -Seconds $InterRoundDelaySeconds
        }
    }
} finally {
    Pop-Location
}

Write-Host "`n========== Demo data delivery finished ==========" -ForegroundColor Cyan
if ($failedRounds.Count -eq 0) {
    Write-Host "All 10 rounds and 20 bearing samples completed." -ForegroundColor Green
} else {
    Write-Host ("Failed rounds: {0}. Frontend and services remain running." -f ($failedRounds -join ", ")) -ForegroundColor Red
}
Write-Host "Frontend: http://127.0.0.1:8088/index.html"
