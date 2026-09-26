# Run elevated after reviewed source/dependencies and private config are installed.
# Creates only QQBotDailyBackup; never stops services or changes cleanup.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$ConfigPath,
    [string]$At = '04:30'
)
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath $ProjectRoot).Path
$config = (Resolve-Path -LiteralPath $ConfigPath).Path
$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'PYTHON_NOT_FOUND' }
if ($project.Contains('"') -or $config.Contains('"')) { throw 'INVALID_PATH_QUOTE' }
if (Get-ScheduledTask -ErrorAction Stop | Where-Object {$_.TaskName -eq 'QQBotDailyBackup'}) {
    throw 'BACKUP_TASK_ALREADY_EXISTS_REVIEW_BEFORE_REPLACING'
}
$action = New-ScheduledTaskAction -Execute $python -Argument ('-m app.reports.scheduled_backup run --config "' + $config + '"') -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 40) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'QQBotDailyBackup' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Daily QQBot snapshot without service credentials; isolated verification; no deletion' | Out-Null
$task = Get-ScheduledTask -TaskName 'QQBotDailyBackup'
if ($task.Principal.UserId -notin @('SYSTEM','S-1-5-18') -or $task.Settings.MultipleInstances -ne 'IgnoreNew' -or -not $task.Settings.StartWhenAvailable -or $task.Actions.Execute -ne $python -or $task.Actions.WorkingDirectory -ne $project) {
    throw 'REGISTERED_TASK_MISMATCH'
}
Write-Output 'QQBotDailyBackup registered; actual execution must still be verified.'
