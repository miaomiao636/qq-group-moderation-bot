# 注册每日自动清理计划任务（QQBotAutoCleanup，每天 04:00）。
# 前置：后台「开启自动清理」许可已打开（auto_cleanup_enabled=1），否则任务会跳过。
# 需以管理员权限运行。换电脑安装时：修改 $ProjectRoot 为实际项目路径后重新运行。
param(
    [string]$ProjectRoot = "D:\CodeBuddy工作空间\CB 项目\qq-group-moderation-bot"
)

$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
Start-Transcript -Path (Join-Path $ProjectRoot "data\_task_reg.log") -Force | Out-Null
try {
if (-not (Test-Path $python)) {
    Write-Error "python not found: $python"
    exit 1
}

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m app.reports.maintenance cleanup" -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At 04:00
Register-ScheduledTask -TaskName "QQBotAutoCleanup" -Action $action -Trigger $trigger `
    -Description "Daily retention cleanup for QQ moderation bot" -Force | Out-Null

$info = Get-ScheduledTask -TaskName "QQBotAutoCleanup"
Write-Output "registered: $($info.TaskName) state=$($info.State)"
} finally {
    Stop-Transcript | Out-Null
}
