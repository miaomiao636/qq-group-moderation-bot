# New-install helper. Default is a read-only plan; -Apply requires Administrator.
# Keep ASCII-only for Windows PowerShell 5.1. Never replaces an existing service.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectDir,
    [string]$NssmPath = 'C:\nssm\nssm.exe',
    [string]$PythonExe = '',
    [ValidateSet('NapCat', 'WithOfficial')][string]$Mode = 'NapCat',
    [ValidatePattern('^[A-Za-z][A-Za-z0-9_-]{0,63}$')][string]$ServicePrefix = 'QQBot',
    [switch]$Apply,
    [switch]$StartServices
)

$ErrorActionPreference = 'Stop'
$proj = (Resolve-Path -LiteralPath $ProjectDir).Path
if (-not $PythonExe) { $PythonExe = Join-Path $proj '.venv\Scripts\python.exe' }
$services = @([pscustomobject]@{Name = ($ServicePrefix + 'Web'); Module = 'app'})
if ($Mode -eq 'WithOfficial') {
    $services += [pscustomobject]@{Name = ($ServicePrefix + 'Runtime'); Module = 'app.runtime'}
}
if ($StartServices -and -not $Apply) { throw 'StartServices requires Apply.' }
if (-not $Apply) {
    [pscustomobject]@{
        Mode = $Mode; Project = $proj; Python = $PythonExe; Services = $services
        Apply = $false; StartsServices = $false
        Recovery = '5s retry, 15s retry, then stop; reset after 24h without failure'
    } | ConvertTo-Json -Depth 4
    return
}

# Check every selected name before creating anything; never stop/remove old services.
foreach ($service in $services) {
    if (Get-Service -Name $service.Name -ErrorAction SilentlyContinue) {
        throw ('Service already exists; separate reviewed upgrade required: ' + $service.Name)
    }
}
foreach ($path in @($NssmPath, $PythonExe, (Join-Path $proj 'app\__main__.py'), (Join-Path $proj '.env'))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Required installation file is missing.' }
}
$NssmPath = (Resolve-Path -LiteralPath $NssmPath).Path
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path

function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Installation command failed; review partial installation and service state.' }
}

$preflight = @'
import sys
from pathlib import Path
try:
    from app.config import Settings
    s = Settings(_env_file=Path(sys.argv[1]) / '.env')
    if not (s.app_env == 'prod' and s.admin_password):
        raise ValueError()
    if s.action_mode != 'SHADOW' or s.onebot_actions_enabled:
        raise ValueError()
    if not (s.onebot_ws_enabled and s.onebot_self_id and s.onebot_access_token):
        raise ValueError()
    if sys.argv[2] == 'WithOfficial' and not (s.qq_app_id and s.qq_app_secret):
        raise ValueError()
except Exception:
    sys.exit(2)
'@
Push-Location -LiteralPath $proj
try {
    Invoke-Checked $PythonExe @('-c', $preflight, $proj, $Mode)
    $logDir = Join-Path $proj 'data'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    foreach ($service in $services) {
        $name = $service.Name
        Invoke-Checked $NssmPath @('install', $name, $PythonExe, '-m', $service.Module)
        Invoke-Checked $NssmPath @('set', $name, 'Start', 'SERVICE_DEMAND_START')
        Invoke-Checked $NssmPath @('set', $name, 'AppDirectory', $proj)
        Invoke-Checked $NssmPath @('set', $name, 'AppStdout', (Join-Path $logDir "service_$name.log"))
        Invoke-Checked $NssmPath @('set', $name, 'AppStderr', (Join-Path $logDir "service_$name.err.log"))
        Invoke-Checked $NssmPath @('set', $name, 'AppRotateFiles', '1')
        Invoke-Checked $NssmPath @('set', $name, 'AppRotateBytes', '10485760')
        Invoke-Checked $NssmPath @('set', $name, 'AppRotateOnline', '1')
        Invoke-Checked $NssmPath @('set', $name, 'AppEnvironmentExtra', 'PYTHONUNBUFFERED=1')
        Invoke-Checked $NssmPath @('set', $name, 'AppExit', 'Default', 'Exit')
        Invoke-Checked $NssmPath @('set', $name, 'AppExit', '0', 'Exit')
        Invoke-Checked $NssmPath @('set', $name, 'AppStopMethodConsole', '15000')
        Invoke-Checked $PythonExe @('-m', 'app.service_recovery', $name)
    }
    foreach ($service in $services) {
        Invoke-Checked $NssmPath @('set', $service.Name, 'Start', 'SERVICE_AUTO_START')
    }
    if ($StartServices) {
        foreach ($service in $services) { Invoke-Checked $NssmPath @('start', $service.Name) }
    }
    Write-Output 'New services configured. Reboot and fault-recovery acceptance remain required.'
} finally { Pop-Location }
