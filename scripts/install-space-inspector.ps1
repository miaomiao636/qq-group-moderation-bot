# Install the optional desktop tool in its own per-user environment.
# No service, production database, account setting, or action switch is changed.
[CmdletBinding()]
param([string]$PythonPath = "python")
$ErrorActionPreference = "Stop"
$repoPath = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$appFolder = Join-Path $env:LOCALAPPDATA "QQSpaceInspector"
$runtimePath = Join-Path $appFolder "runtime"
$pythonExe = Join-Path $runtimePath "Scripts/python.exe"
$previousEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        & $PythonPath -c "import sys, tkinter; assert sys.version_info >= (3,12)"
        if ($LASTEXITCODE -ne 0) { throw "Python 3.12+ with Tk is required." }
        & uv venv $runtimePath --python $PythonPath
        if ($LASTEXITCODE -ne 0) { throw "Could not create inspector environment." }
    }
    $env:UV_PROJECT_ENVIRONMENT = $runtimePath
    & uv sync --project $repoPath --locked --no-dev --extra inspection
    if ($LASTEXITCODE -ne 0) { throw "Could not install locked inspector dependencies." }
    & $pythonExe -c "import tkinter; from app.space_inspector.gui import main"
    if ($LASTEXITCODE -ne 0) { throw "Inspector import check failed." }
    $desktopPath = [Environment]::GetFolderPath("Desktop")
    $shortcutName = -join ([char[]](0x51,0x51,0x7a7a,0x95f4,0x9650,0x5236,0x5de1,0x68c0))
    $shellObject = New-Object -ComObject WScript.Shell
    $shortcut = $shellObject.CreateShortcut((Join-Path $desktopPath ($shortcutName + ".lnk")))
    $shortcut.TargetPath = Join-Path $runtimePath "Scripts/pythonw.exe"
    $shortcut.Arguments = "-m app.space_inspector"
    $shortcut.WorkingDirectory = $repoPath
    $shortcut.Description = "QQ Space restriction inspector"
    $shortcut.Save()
    Write-Output "Desktop shortcut created. Runtime: $runtimePath"
} finally {
    $env:UV_PROJECT_ENVIRONMENT = $previousEnvironment
}
