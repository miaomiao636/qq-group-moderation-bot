"""Execute PowerShell with fake services and executables; never alter real services."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(not SHELL, reason="PowerShell is unavailable")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install-services-nssm.ps1"


def run_installer(tmp_path, args="", *, existing=False, failed=""):
    root = tmp_path / "Synthetic Project"
    (root / "app").mkdir(parents=True)
    (root / "app/__main__.py").write_text("")
    (root / ".env").write_text("SYNTHETIC_ONLY=1")
    log = tmp_path / "calls.jsonl"
    exe = tmp_path / "fake.ps1"
    exe.write_text(
        "param([Parameter(ValueFromRemainingArguments=$true)][object[]]$Values)\n"
        "$Values | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:FAKE_CALL_LOG\n"
        "if ($env:FAKE_FAILURE -and ($Values -join ' ').Contains($env:FAKE_FAILURE)) { exit 7 }; exit 0\n"
    )
    harness = tmp_path / "harness.ps1"

    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    harness.write_text(
        "$ErrorActionPreference='Stop'\n"
        f"$env:FAKE_CALL_LOG={quote(log)}\n$env:FAKE_FAILURE={quote(failed)}\n"
        "function Get-Service { param($Name,$ErrorAction) "
        + ("[pscustomobject]@{Name=$Name;Status='Stopped'}" if existing else "$null")
        + " }\n"
        f"& {quote(SCRIPT)} -ProjectDir {quote(root)} -NssmPath {quote(exe)} -PythonExe {quote(exe)} {args}\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [
            SHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    calls = [json.loads(s) for s in log.read_text().splitlines()] if log.exists() else []
    return result, calls, root


def test_default_is_read_only_napcat_plan(tmp_path):
    result, calls, root = run_installer(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "QQBotWeb" in result.stdout and "QQBotRuntime" not in result.stdout
    assert calls == []
    assert not (root / "data").exists()


def test_explicit_apply_installs_only_web_and_does_not_start(tmp_path):
    result, calls, _ = run_installer(tmp_path, "-Apply")
    assert result.returncode == 0, result.stderr
    assert [v for v in calls if v[0] == "install"] == [
        ["install", "QQBotWeb", str(tmp_path / "fake.ps1"), "-m", "app"]
    ]
    assert not any(v[0] in {"start", "stop", "remove"} for v in calls)
    assert ["set", "QQBotWeb", "AppExit", "Default", "Exit"] in calls
    assert ["-m", "app.service_recovery", "QQBotWeb"] in calls


def test_existing_services_are_never_overwritten(tmp_path):
    result, calls, _ = run_installer(tmp_path, "-Apply", existing=True)
    assert result.returncode != 0
    assert calls == []


def test_official_mode_is_explicit_and_start_is_last(tmp_path):
    result, calls, _ = run_installer(tmp_path, "-Mode WithOfficial -Apply -StartServices")
    assert result.returncode == 0, result.stderr
    assert {v[1] for v in calls if v[0] == "install"} == {"QQBotWeb", "QQBotRuntime"}
    assert calls[-2:] == [["start", "QQBotWeb"], ["start", "QQBotRuntime"]]


@pytest.mark.parametrize(
    "failed", ["-c", "install QQBotWeb", "AppDirectory", "app.service_recovery"]
)
def test_external_failure_stops_before_starting(tmp_path, failed):
    result, calls, _ = run_installer(tmp_path, "-Apply -StartServices", failed=failed)
    assert result.returncode != 0
    assert not any(v[0] == "start" for v in calls)
    assert not any(v[0] in {"stop", "remove"} for v in calls)


@pytest.mark.parametrize(
    "mode,overrides,ok",
    [
        ("NapCat", {}, True),
        ("WithOfficial", {}, False),
        ("WithOfficial", {"QQ_APP_ID": "synthetic", "QQ_APP_SECRET": "synthetic"}, True),
        ("NapCat", {"ONEBOT_WS_ENABLED": "false"}, False),
        ("NapCat", {"ONEBOT_SELF_ID": ""}, False),
        ("NapCat", {"ONEBOT_ACTIONS_ENABLED": "true"}, False),
        ("NapCat", {"APP_ENV": "dev"}, False),
    ],
)
def test_real_preflight_is_safe_even_with_python_optimizations(tmp_path, mode, overrides, ok):
    preflight = SCRIPT.read_text().split("$preflight = @'\n", 1)[1].split("\n'@", 1)[0]
    settings = {
        "APP_ENV": "prod",
        "ADMIN_PASSWORD": "synthetic-not-real-admin",
        "ONEBOT_WS_ENABLED": "true",
        "ONEBOT_SELF_ID": "11111111",
        "ONEBOT_ACCESS_TOKEN": "synthetic-test-token",
        "ACTION_MODE": "SHADOW",
        "ONEBOT_ACTIONS_ENABLED": "false",
        **overrides,
    }
    (tmp_path / ".env").write_text("\n".join(f"{k}={v}" for k, v in settings.items()))
    env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "TEMP", "TMP") if k in os.environ}
    env.update(PYTHONPATH=str(SCRIPT.parents[1]), PYTHONOPTIMIZE="1", PYTHONUTF8="1")
    result = subprocess.run(
        [sys.executable, "-c", preflight, str(tmp_path), mode],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert (result.returncode == 0) == ok
    assert result.stdout == result.stderr == b""
