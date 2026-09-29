"""回滚前置检查的回归（主审 F06-R）。

覆盖主审要求的最小项：可执行链路、准确的服务状态、真实备份与导出、退出码/fail-fast，
并且**失败不得到达 downgrade/start**。服务状态用可注入的 query 驱动（StopPending、超时、
缺服务三种路径），因此不依赖本机是否真的装了这两个服务，也可在非 Windows CI 上跑。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "rollback_preflight.py"
PS1_PATH = ROOT / "scripts" / "rollback_d037_d038.ps1"


def _load_script():
    """以模块方式加载 scripts/rollback_preflight.py（scripts 不是包）。"""
    spec = importlib.util.spec_from_file_location("rollback_preflight", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


preflight = _load_script()


# --- 服务状态：STOP_PENDING 不算已停、超时与缺服务都必须中止 ------------------------------


@pytest.mark.parametrize(
    "output,expected",
    [
        ("STATE              : 4  RUNNING", "Running"),
        ("STATE              : 3  STOP_PENDING", "StopPending"),
        ("STATE              : 1  STOPPED", "Stopped"),
        ("SERVICE_NAME: QQBotWeb\n", "Unknown"),
        ("", "Unknown"),
    ],
)
def test_parse_sc_state(output: str, expected: str) -> None:
    assert preflight.parse_sc_state(output) == expected


def test_all_stopped_requires_exact_stopped() -> None:
    assert preflight.all_stopped({"a": "Stopped", "b": "Stopped"}) is True
    assert preflight.all_stopped({"a": "Stopped", "b": "StopPending"}) is False
    assert preflight.all_stopped({"a": "Stopped", "b": "Unknown"}) is False
    assert preflight.all_stopped({}) is False


def test_wait_services_stopped_polls_through_stop_pending() -> None:
    states = iter(["StopPending", "StopPending", "Stopped", "Stopped"])
    sleeps: list[float] = []
    result = preflight.wait_services_stopped(
        ["QQBotWeb", "QQBotRuntime"],
        timeout_seconds=30,
        query=lambda _name: next(states),
        sleep=sleeps.append,
    )
    assert result == {"QQBotWeb": "Stopped", "QQBotRuntime": "Stopped"}
    assert sleeps, "必须在未停止时轮询等待，而不是一次性判定"


def test_wait_services_stopped_times_out_on_stop_pending() -> None:
    sleeps: list[float] = []
    with pytest.raises(preflight.PreflightError, match="未在"):
        preflight.wait_services_stopped(
            ["QQBotWeb"],
            timeout_seconds=0,
            query=lambda _name: "StopPending",
            sleep=sleeps.append,
        )


def test_wait_services_stopped_aborts_on_missing_service() -> None:
    def missing(_name: str) -> str:
        raise preflight.PreflightError("服务 QQBotWeb 不存在或不可查询（sc.exe 退出码 1060）")

    with pytest.raises(preflight.PreflightError, match="不存在"):
        preflight.wait_services_stopped(["QQBotWeb"], timeout_seconds=5, query=missing)


# --- 备份 + 导出：真实生成、回读校验、失败即非 0 --------------------------------------------


def _make_db(path: Path, members: list[tuple[str, str, int]]) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "create table allowlist_members (external_user_id text primary key, note text, "
        "enabled integer not null)"
    )
    connection.executemany(
        "insert into allowlist_members (external_user_id, note, enabled) values (?, ?, ?)",
        members,
    )
    connection.commit()
    connection.close()


def test_backup_export_writes_reimportable_file(tmp_path: Path) -> None:
    db = tmp_path / "moderation.db"
    _make_db(db, [("920000001", "甲", 1), ("920000002", "乙", 1), ("920000003", "停用", 0)])
    backup, export_path, count = preflight.backup_and_export(
        database_url=f"sqlite+aiosqlite:///{db.as_posix()}",
        out_dir=tmp_path / "evidence",
    )
    assert count == 2
    assert backup.is_file() and backup.stat().st_size > 0
    assert export_path.is_file() and export_path.stat().st_size > 0
    # 导出的文件必须**能再导入**，且只含启用成员（与库内一致）
    from app.moderation.allowlist_members_io import parse_member_list

    parsed = parse_member_list(export_path.read_text(encoding="utf-8"))
    assert {member.user_id for member in parsed.valid} == {"920000001", "920000002"}
    assert not parsed.invalid


def test_backup_export_refuses_zero_enabled_members(tmp_path: Path) -> None:
    db = tmp_path / "moderation.db"
    _make_db(db, [("920000001", "全停用", 0)])
    with pytest.raises(preflight.PreflightError, match="启用中的成员为 0"):
        preflight.backup_and_export(
            database_url=f"sqlite+aiosqlite:///{db.as_posix()}",
            out_dir=tmp_path / "evidence",
        )


def test_backup_export_refuses_backup_without_allowlist_table(tmp_path: Path) -> None:
    db = tmp_path / "moderation.db"
    sqlite3.connect(db).close()  # 空库：模拟"用了升级前的旧备份"
    with pytest.raises(preflight.PreflightError, match="allowlist_members"):
        preflight.backup_and_export(
            database_url=f"sqlite+aiosqlite:///{db.as_posix()}",
            out_dir=tmp_path / "evidence",
        )


def test_cli_backup_export_exit_code_is_nonzero_on_failure(tmp_path: Path) -> None:
    db = tmp_path / "moderation.db"
    _make_db(db, [("920000001", "全停用", 0)])
    code = preflight.main(
        [
            "backup-export",
            "--out-dir",
            str(tmp_path / "evidence"),
            "--database-url",
            f"sqlite+aiosqlite:///{db.as_posix()}",
        ]
    )
    assert code != 0, "备份/导出失败必须返回非 0（调用方据此中止，不进入 downgrade）"


# --- 版本一致性：数据库 revision 与代码 head 必须一致 ---------------------------------------


@pytest.mark.parametrize(
    "db_revision,code_head,ok",
    [
        ("b8d4f2a05e31", "b8d4f2a05e31", True),
        ("c9a1f4d27e30", "b8d4f2a05e31", False),  # 降级没生效
        ("b8d4f2a05e31", "c9a1f4d27e30", False),  # 代码没切
        (None, "b8d4f2a05e31", False),  # 库读不到版本
    ],
)
def test_check_version(db_revision: str | None, code_head: str, ok: bool) -> None:
    if ok:
        preflight.check_version(
            expected="b8d4f2a05e31", db_revision=db_revision, code_head=code_head
        )
    else:
        with pytest.raises(preflight.PreflightError):
            preflight.check_version(
                expected="b8d4f2a05e31", db_revision=db_revision, code_head=code_head
            )


def test_read_db_revision_on_isolated_db(tmp_path: Path) -> None:
    db = tmp_path / "moderation.db"
    connection = sqlite3.connect(db)
    connection.execute("create table alembic_version (version_num varchar(32) not null)")
    connection.execute("insert into alembic_version values ('b8d4f2a05e31')")
    connection.commit()
    connection.close()
    assert preflight.read_db_revision(f"sqlite+aiosqlite:///{db.as_posix()}") == "b8d4f2a05e31"
    assert (
        preflight.main(
            [
                "check-version",
                "--expected",
                "b8d4f2a05e31",
                "--database-url",
                f"sqlite+aiosqlite:///{db.as_posix()}",
            ]
        )
        != 0
    ), "代码 head 与期望不一致（旧代码才能通过）时必须非 0"


# --- PowerShell 主链静态门禁：属性正确、退出码检查、顺序不可颠倒 ----------------------------


def _ps1_lines() -> list[str]:
    return PS1_PATH.read_text(encoding="utf-8").splitlines()


def test_rollback_script_uses_status_property() -> None:
    text = PS1_PATH.read_text(encoding="utf-8")
    assert ".Status" in text, "Get-Service 返回对象必须取 Status 属性"
    assert ".State" not in text, "Get-Service 对象没有 State 属性（主审 F06-R 第 1 条）"


@pytest.mark.parametrize(
    "marker",
    [
        "$py services",
        "$py backup-export",
        "uv run alembic downgrade",
        "git switch --detach",
        "$py check-version",
    ],
)
def test_rollback_script_checks_exit_code_after_each_critical_step(marker: str) -> None:
    lines = _ps1_lines()
    index = next(i for i, line in enumerate(lines) if marker in line)
    window = "\n".join(lines[index : index + 4])
    assert "Assert-ExitCode" in window, f"{marker} 之后必须检查退出码"


def test_rollback_script_order_prevents_reaching_downgrade_on_failure() -> None:
    text = PS1_PATH.read_text(encoding="utf-8")
    order = [
        "$py services",
        "$py backup-export",
        "uv run alembic downgrade",
        "$py check-version",
        "sc.exe start $name",
        "ROLLBACK_DONE",
    ]
    positions = [text.index(item) for item in order]
    assert positions == sorted(positions), (
        "步骤顺序必须：停服确认 → 备份/导出 → 降级 → 版本核对 → 启动 → 完成"
    )
    assert "exit 1" in text and "ROLLBACK_ABORT" in text, "失败必须以非 0 退出码中止脚本"


def test_rollback_script_checks_each_service_start_individually() -> None:
    """主审 F06-C：每个服务的启动都要单独查退出码，且 DONE 必须晚于"实际 Running"判定。"""
    lines = _ps1_lines()
    index = next(i for i, line in enumerate(lines) if "sc.exe start $name" in line)
    assert "Assert-ExitCode" in "\n".join(lines[index : index + 3]), "启动后必须单独检查退出码"
    text = "\n".join(lines)
    assert text.index("-ne 'Running'") < text.index("ROLLBACK_DONE"), (
        "未确认 Running 前不得报告完成"
    )


def test_rollback_script_uses_out_of_repo_checker_copy() -> None:
    """主审 F06-B：检查程序必须复制到仓库外，否则 `git switch` 之后就不存在了。"""
    text = PS1_PATH.read_text(encoding="utf-8")
    assert "$env:TEMP" in text and "Copy-Item" in text
    assert text.index("Copy-Item") < text.index("git switch --detach"), "复制必须发生在切换版本之前"
    assert "check-version --expected $ExpectedRevision --code-head" in text, (
        "版本核对要显式传代码 head"
    )


def test_version_checker_runs_outside_the_repository(tmp_path: Path) -> None:
    """主审 F06-B：把检查程序拷出仓库后 `check-version` 仍可用（只依赖标准库）。"""
    import shutil
    import subprocess

    outside = tmp_path / "near-old-tree"
    outside.mkdir()
    copied = outside / "rollback_preflight.py"
    shutil.copy2(SCRIPT_PATH, copied)
    db = tmp_path / "old.db"
    connection = sqlite3.connect(db)
    connection.execute("create table alembic_version (version_num varchar(32) not null)")
    connection.execute("insert into alembic_version values ('b8d4f2a05e31')")
    connection.commit()
    connection.close()
    base = [
        sys.executable,
        str(copied),
        "check-version",
        "--expected",
        "b8d4f2a05e31",
        "--database-url",
        f"sqlite+aiosqlite:///{db.as_posix()}",
    ]
    ok = subprocess.run([*base, "--code-head", "b8d4f2a05e31"], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    bad = subprocess.run([*base, "--code-head", "c9a1f4d27e30"], capture_output=True, text=True)
    assert bad.returncode != 0, "代码 head 与期望不一致时必须非 0"


def test_default_entry_uses_the_existing_config_module() -> None:
    """主审 F06-A：默认入口（不传 --database-url）必须走仓库真实的配置模块。"""
    assert (ROOT / "app" / "config.py").is_file()
    assert not (ROOT / "app" / "core" / "config.py").exists()
    url = preflight._configured_database_url()
    assert url.startswith("sqlite"), url
