# QQ Group Moderation Bot - register Windows services via NSSM (run as Administrator)
# Registers two services:
#   QQBotWeb     = python -m app          (web UI + OneBot reverse-WS + notifications)
#   QQBotRuntime = python -m app.runtime  (resident shadow runner)
# Keep this file ASCII-only. If you add non-ASCII text, save as UTF-8 WITH BOM:
# PowerShell 5.1 mis-parses BOM-less UTF-8 and mangles non-ASCII paths.
param(
    [Parameter(Mandatory = $true)][string]$ProjectDir,
    [string]$NssmPath = 'C:\nssm\nssm.exe',
    [string]$PythonExe = ''
)

$ErrorActionPreference = 'Stop'
$proj = (Resolve-Path $ProjectDir).Path

if (-not (Test-Path $NssmPath)) {
    throw "nssm.exe not found: $NssmPath (download from https://nssm.cc/)"
}
if (-not $PythonExe) {
    $PythonExe = Join-Path $proj '.venv\Scripts\python.exe'
}
if (-not (Test-Path $PythonExe)) {
    throw "python.exe not found: $PythonExe (run 'uv sync --all-groups' first)"
}

$logDir = Join-Path $proj 'data'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Install-BotService([string]$Name, [string]$Args) {
    & $NssmPath stop $Name *> $null
    & $NssmPath remove $Name confirm *> $null
    & $NssmPath install $Name $PythonExe $Args *> $null
    & $NssmPath set $Name AppDirectory $proj *> $null
    & $NssmPath set $Name AppStdout (Join-Path $logDir "service_$Name.log") *> $null
    & $NssmPath set $Name AppStderr (Join-Path $logDir "service_$Name.err.log") *> $null
    & $NssmPath set $Name AppRotateFiles 1 *> $null
    & $NssmPath set $Name AppRotateBytes 10485760 *> $null
    & $NssmPath set $Name AppRotateOnline 1 *> $null
    & $NssmPath set $Name AppEnvironmentExtra PYTHONUNBUFFERED=1 *> $null
    & $NssmPath set $Name Start SERVICE_AUTO_START *> $null
    & $NssmPath set $Name AppExit Default Restart *> $null
    & $NssmPath set $Name AppRestartDelay 5000 *> $null
    & $NssmPath set $Name AppStopMethodConsole 15000 *> $null
    Write-Host "installed: $Name"
}

Install-BotService 'QQBotWeb' '-m app'
Install-BotService 'QQBotRuntime' '-m app.runtime'

& $NssmPath start QQBotWeb *> $null
Start-Sleep -Seconds 3
& $NssmPath start QQBotRuntime *> $null
Start-Sleep -Seconds 5

sc.exe query QQBotWeb
sc.exe query QQBotRuntime
Write-Host "done. service logs: $logDir\service_*.log"
