# D-037/D-038 回滚（迁移成功后的回退：数据库已是 c9a1f4d27e30）
#
# 主审 F06-R：此前的手册把关键步骤写成注释（没有真正备份/导出）、用错了服务状态属性
# （Get-Service 返回对象没有 State，只有 Status），也没有检查退出码。本脚本把它落成
# **可执行、失败即停**的命令链：任一步失败都不会继续到 downgrade / start。
#
# 用法（**管理员 PowerShell**，工作目录 = 仓库根）：
#   powershell -ExecutionPolicy Bypass -File scripts\rollback_d037_d038.ps1 `
#       -ExpectedRevision b8d4f2a05e31 -TargetSha 68a94b9
#
# 生产回滚/重启须负责人另行授权并在维护窗口执行；先按 docs/deploy-runbook-d037-d038.md
# 的演练要求在**隔离库**上跑通。本脚本不替代人工复核：它只保证"该失败的会失败"。
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ExpectedRevision,  # 降级目标 revision（旧版 head）
    [Parameter(Mandatory = $true)][string]$TargetSha,          # 上一个已验收 SHA（例：68a94b9）
    [int]$StopTimeoutSeconds = 60,
    [string]$OutDir = "data\rollback-evidence"
)

$ErrorActionPreference = 'Stop'
$py = 'uv run python scripts/rollback_preflight.py'

function Fail([string]$message) {
    Write-Host "ROLLBACK_ABORT: $message" -ForegroundColor Red
    exit 1
}

function Assert-ExitCode([string]$step) {
    if ($LASTEXITCODE -ne 0) { Fail "$step 失败（exit $LASTEXITCODE），已中止，未继续后续步骤" }
}

Write-Host "== 第 0 步：两个服务必须存在（Get-Service 对象取 Status 属性，没有 State 属性）=="
foreach ($name in @('QQBotWeb', 'QQBotRuntime')) {
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    if ($null -eq $svc) { Fail "服务 $name 不存在，中止回滚" }
    Write-Host "  $name 当前 Status = $($svc.Status)"
}

Write-Host "== 第 1 步：停止两个服务并确认 STOPPED（STOP_PENDING ≠ STOPPED）=="
sc.exe stop QQBotWeb | Out-Null
sc.exe stop QQBotRuntime | Out-Null
Invoke-Expression "$py services --svc QQBotWeb --svc QQBotRuntime --timeout $StopTimeoutSeconds"
Assert-ExitCode "确认服务已停止"

Write-Host "== 第 2/3 步：一致性备份 + 从降级前当前库导出可再导入名单并回读校验 =="
# 备份走 SQLite Connection.backup + quick_check（WAL 下的已提交数据不会漏），
# 名单从**该备份**导出（不要用升级前的旧备份：allowlist_members 是本次新建的表）。
Invoke-Expression "$py backup-export --out-dir '$OutDir'"
Assert-ExitCode "一致性备份或名单导出"

Write-Host "== 第 4 步：降级 + 切代码 + 版本一致性核对（不一致就不启动服务）=="
uv run alembic current
Assert-ExitCode "读取当前迁移版本"
uv run alembic downgrade $ExpectedRevision
Assert-ExitCode "降级"
git switch --detach $TargetSha
Assert-ExitCode "切换代码版本"
Invoke-Expression "$py check-version --expected $ExpectedRevision"
Assert-ExitCode "版本一致性核对（数据库 revision 与代码 head 必须都等于 $ExpectedRevision）"

Write-Host "== 第 5 步：启动两个服务 =="
sc.exe start QQBotWeb | Out-Null
sc.exe start QQBotRuntime | Out-Null
Write-Host "ROLLBACK_DONE：降级到 $ExpectedRevision / $TargetSha，名单文件在 $OutDir（降级后需按需重新导入）"
