"""D-037/D-038 回滚前置检查（主审 F06-R：把手册落成可执行、失败即停的命令链）。

子命令全部以**退出码**表达结论（0=通过；非 0=失败即停，调用方 PowerShell 必须检查）：

- ``services``      轮询 Windows 服务是否**真的 STOPPED**（STOP_PENDING ≠ STOPPED）；
                    超时或服务不存在 → 非 0。
- ``backup-export`` 用项目的一致性备份入口（``app.reports.backup.backup_sqlite``：走
                    SQLite ``Connection.backup`` + ``quick_check``，不是复制主库文件）
                    留备份，并**从该备份**导出可再导入的成员名单文件、回读校验；
                    备份缺失/为空、启用成员数为 0、导出文件不能再解析 → 非 0。
- ``check-version`` 核对数据库 ``alembic_version`` 与代码 head 是否都等于期望 revision；
                    不一致 → 非 0（**不启动服务**）。

本脚本只读生产库（备份是只读源 + 新文件），不连接网络、不启动/停止服务、不改任何状态。
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess  # noqa: S404 - 仅用于 Windows 服务状态查询（sc.exe query）
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

# 让"仓库内直接运行"和"拷到仓库外运行"（PS1 为跨 `git switch` 而复制到 TEMP）都能找到 app 包。
for _candidate in (Path(__file__).resolve().parents[1], Path.cwd()):
    if (_candidate / "app").is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

# 应用侧依赖一律**延迟导入**（主审 F06-B）：`check-version` 必须能在"目标工作树不含本脚本、
# 也不含 app.moderation.allowlist_members_io / app.reports.backup"的旧版本里照跑。

# sc.exe query 的 STATE 数值 → 可读状态（1 STOPPED / 2 START_PENDING / 3 STOP_PENDING / 4 RUNNING）
_SC_STATE_NAMES = {
    "1": "Stopped",
    "2": "StartPending",
    "3": "StopPending",
    "4": "Running",
    "5": "ContinuePending",
    "6": "PausePending",
    "7": "Paused",
}


class PreflightError(RuntimeError):
    """任一前置条件不满足 → 调用方必须中止回滚（不得继续到 downgrade / start）。"""


def parse_sc_state(output: str) -> str:
    """从 ``sc.exe query`` 输出解析服务状态；识别不了返回 ``Unknown``。"""
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("STATE"):
            continue
        _, _, tail = stripped.partition(":")
        parts = tail.split()
        if len(parts) >= 2 and parts[0].isdigit():
            # 英文为 "4  RUNNING"；避免依赖语言，统一取状态码再映射
            return _SC_STATE_NAMES.get(parts[0], "Unknown")
        if parts:
            return parts[0].strip().capitalize()
    return "Unknown"


def all_stopped(states: Mapping[str, str]) -> bool:
    """两个服务都必须**精确**等于 Stopped；StopPending / Unknown 一律不算停。"""
    return bool(states) and all(state == "Stopped" for state in states.values())


def wait_services_stopped(
    names: Sequence[str],
    *,
    timeout_seconds: float = 60,
    query: Callable[[str], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    """轮询直到全部 STOPPED；超时或查询失败抛 ``PreflightError``。

    ``query`` 可注入（drill/测试用合成状态驱动 StopPending、超时、缺服务三种路径）。
    """
    query = query or _query_service_state
    deadline = time.monotonic() + timeout_seconds
    states: dict[str, str] = {}
    while True:
        for name in names:
            try:
                states[name] = query(name)
            except PreflightError:
                raise
            except Exception as exc:  # noqa: BLE001 - 任何查询异常都视为不可确认
                raise PreflightError(f"无法查询服务 {name} 状态：{exc}") from exc
        if all_stopped(states):
            return states
        if time.monotonic() >= deadline:
            raise PreflightError(
                f"服务未在 {timeout_seconds:.0f} 秒内变成 Stopped（当前 "
                + ", ".join(f"{k}={v}" for k, v in states.items())
                + "），中止回滚"
            )
        sleep(0.5)


def _query_service_state(name: str) -> str:
    """用 ``sc.exe query`` 取服务状态；服务不存在（非 0 退出码）→ ``PreflightError``。"""
    completed = subprocess.run(  # noqa: S603 - 固定可执行文件与参数
        ["sc.exe", "query", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise PreflightError(
            f"服务 {name} 不存在或不可查询（sc.exe 退出码 {completed.returncode}），中止回滚"
        )
    return parse_sc_state(completed.stdout)


def _backup_database_rows(backup: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
        exists = connection.execute(
            "select name from sqlite_master where type='table' and name='allowlist_members'"
        ).fetchone()
        if exists is None:
            raise PreflightError("备份里没有 allowlist_members 表（用了升级前的旧备份？），中止")
        return [
            (str(user_id), str(note or ""))
            for user_id, note in connection.execute(
                "select external_user_id, note from allowlist_members "
                "where enabled=1 order by external_user_id"
            )
        ]


def backup_and_export(*, database_url: str, out_dir: Path) -> tuple[Path, Path, int]:
    """一致性备份 → 从备份导出可再导入名单 → 回读校验；返回（备份, 导出文件, 条数）。"""
    from app.moderation.allowlist_members_io import format_member_list, parse_member_list
    from app.reports.backup import backup_sqlite

    backup = backup_sqlite(database_url)
    if not backup.is_file() or backup.stat().st_size == 0:
        raise PreflightError(f"备份文件缺失或为空：{backup}")
    rows = _backup_database_rows(backup)
    if not rows:
        raise PreflightError("启用中的成员为 0，禁止降级（downgrade 会 DROP 整张表，名单会全丢）")
    out_dir.mkdir(parents=True, exist_ok=True)
    export_path = out_dir / f"allowlist-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.txt"
    export_path.write_text(format_member_list(rows), encoding="utf-8")
    if not export_path.is_file() or export_path.stat().st_size == 0:
        raise PreflightError(f"名单导出文件缺失或为空：{export_path}")
    parsed = parse_member_list(export_path.read_text(encoding="utf-8"))
    exported = {member.user_id for member in parsed.valid}
    if not exported or exported != {user_id for user_id, _note in rows}:
        raise PreflightError(
            f"导出的名单不能再导入或与库内不一致（文件 {len(exported)} 条 / 库内 {len(rows)} 条）"
        )
    return backup, export_path, len(rows)


def check_version(*, expected: str, db_revision: str | None, code_head: str) -> None:
    """数据库 revision 与代码 head 必须**都**等于期望值，否则中止（不启动服务）。"""
    if db_revision != expected:
        raise PreflightError(f"数据库 revision={db_revision!r}，期望 {expected!r}")
    if code_head != expected:
        raise PreflightError(f"代码 head={code_head!r}，期望 {expected!r}")


def _sqlite_file_path(database_url: str) -> Path:
    """从 SQLite URL 取文件路径——**只用标准库**（不放 SQLAlchemy，旧目标树里也能跑）。"""
    _, separator, tail = database_url.partition(":///")
    if not separator or not tail:
        raise PreflightError(f"无法从 DATABASE_URL 解析 SQLite 文件路径：{database_url!r}")
    return Path(tail).resolve()


def read_db_revision(database_url: str) -> str | None:
    """读 ``alembic_version.version_num``（表不存在时返回 ``None``）；只依赖标准库。"""
    path = _sqlite_file_path(database_url)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
        row = connection.execute("select version_num from alembic_version").fetchone()
    return None if row is None else str(row[0])


def _code_head_revision() -> str:
    """当前工作树声明的 head（延迟导入）。旧目标树里 app.db 仍应有此函数；失败要显式报错。"""
    try:
        from app.db import get_head_revision

        return str(get_head_revision())
    except Exception as exc:  # noqa: BLE001
        raise PreflightError(
            f"无法从当前工作树读取代码 head（{type(exc).__name__}: {exc}）——"
            "请显式传 --code-head（切换版本后建议由调用方内联 python 取值）"
        ) from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="D-037/D-038 回滚前置检查（失败即停）")
    sub = parser.add_subparsers(dest="command", required=True)

    services = sub.add_parser("services", help="确认服务已 STOPPED（超时/缺服务 → 非 0）")
    services.add_argument("--svc", action="append", default=[], help="服务名（可重复）")
    services.add_argument("--timeout", type=float, default=60.0)

    backup = sub.add_parser("backup-export", help="一致性备份 + 从备份导出可再导入名单")
    backup.add_argument("--out-dir", type=Path, default=Path("data/rollback-evidence"))
    backup.add_argument("--database-url", default=None)

    version = sub.add_parser("check-version", help="数据库 revision 与代码 head 必须一致")
    version.add_argument("--expected", required=True)
    version.add_argument("--database-url", default=None)
    version.add_argument("--code-head", default=None, help="覆盖代码 head（drill/测试用）")

    args = parser.parse_args(argv)
    try:
        if args.command == "services":
            names = args.svc or ["QQBotWeb", "QQBotRuntime"]
            states = wait_services_stopped(names, timeout_seconds=args.timeout)
            print("PREFLIGHT_OK services=" + ",".join(f"{k}:{v}" for k, v in states.items()))
        elif args.command == "backup-export":
            url = args.database_url or _configured_database_url()
            backup_path, export_path, count = backup_and_export(
                database_url=url, out_dir=args.out_dir
            )
            print(f"PREFLIGHT_OK backup={backup_path} export={export_path} members={count}")
        else:
            url = args.database_url or _configured_database_url()
            check_version(
                expected=args.expected,
                db_revision=read_db_revision(url),
                code_head=args.code_head or _code_head_revision(),
            )
            print(f"PREFLIGHT_OK revision={args.expected}")
    except PreflightError as exc:
        print(f"PREFLIGHT_FAIL: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - 非预期异常同样必须非 0（失败即停）
        print(f"PREFLIGHT_FAIL(unexpected): {type(exc).__name__}: {exc}")
        return 3
    return 0


def _configured_database_url() -> str:
    from app.config import get_settings

    return str(get_settings().database_url)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
