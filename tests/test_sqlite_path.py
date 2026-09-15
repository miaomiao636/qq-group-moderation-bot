"""SQLite 相对路径规范化与迁移定位回归测试（R-101 复验项9/10）。

安全约束（主审复验项9整改）：
- 本文件所有测试只操作 pytest 的 `tmp_path` 临时目录，
  禁止创建、修改或删除 `PROJECT_ROOT/data/moderation.db` 等真实数据库文件。
- 模块级守卫夹具在测试前后对项目真实数据目录做内容快照，
  一旦发现被触碰立即断言失败。

覆盖：
- README 默认配置 `sqlite+aiosqlite:///./data/moderation.db` 稳定解析到项目数据目录。
- 从非项目工作目录启动时，相对路径仍解析到同一绝对路径，不依赖当前工作目录。
- Windows 绝对路径格式（`C:/...` 与 `C:\\...`）保持不变。
- Windows 盘符相对路径（`C:relative\\db.db`）被明确拒绝。
- 绝对路径与 `:memory:` 保持不变。
- 子进程端到端：从非项目目录执行真实 `alembic upgrade head`，
  再从另一个目录启动应用，确认连接同一数据库；
  预先存在的数据库文件不会被删除。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from app.config import PROJECT_ROOT, Settings, _normalize_sqlite_url

# README/.env.example 中的默认相对路径
README_DEFAULT_URL = "sqlite+aiosqlite:///./data/moderation.db"
# 项目真实数据目录（只读快照，绝不写入或删除）
PROJECT_DATA_DIR = PROJECT_ROOT / "data"


# ---------------------------------------------------------------------------
# 守卫夹具：确保本模块所有测试不触碰项目真实数据目录
# ---------------------------------------------------------------------------


def _snapshot_data_dir() -> str:
    """对项目真实数据目录做内容快照（文件名 + 内容哈希）。

    目录不存在时也记录为快照，保证测试不会凭空创建它。
    """
    if not PROJECT_DATA_DIR.exists():
        return json.dumps({"exists": False, "files": {}})
    files: dict[str, str] = {}
    for p in sorted(PROJECT_DATA_DIR.iterdir()):
        if p.is_file():
            try:
                files[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
            except PermissionError:
                # 运行时单实例锁（如 moderation.db.onebot-runtime.lock）拒绝共享读：
                # 记录存在性即可，守卫仍能检测新增/删除，不因锁文件整体失败。
                files[p.name] = "locked"
        else:
            files[p.name + "/"] = "dir"
    return json.dumps({"exists": True, "files": files}, sort_keys=True)


def _runtime_lock_held_by_service() -> bool:
    """生产服务是否正持有运行时锁（持有则 data/ 会被持续写入，快照守卫不适用）。

    注意：测试用临时数据库，不能用 settings.database_url 判断——
    守卫守的是仓库 data/ 目录，因此直接探测该目录下的生产数据库锁。
    Windows 的 msvcrt 字节锁不阻止普通 open/read，必须实际尝试获取
    （与 inbox.acquire_runtime_lock 相同的非阻塞语义）才能判断是否被持有。
    """
    import sys as _sys

    lock_path = PROJECT_DATA_DIR / "moderation.db.onebot-runtime.lock"
    try:
        handle = lock_path.open("a+b")
    except OSError:
        return False  # 无法打开：无法判断，按未持有处理（守卫照常运行）
    try:
        try:
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
        except OSError:
            # 第 1 字节落在生产服务的独占锁范围内：读也被拒 = 持有中
            return True
        handle.seek(0)
        if _sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


@pytest.fixture(autouse=True)
def _guard_real_data_dir():
    """每个测试前后对比项目真实数据目录快照，被触碰即失败。

    生产服务运行时会持续写 data/（WAL/媒体/锁），前后快照天然不一致——
    此时守卫无法区分"测试写入"与"服务写入"，明确跳过而不是误报失败。
    停止服务后再运行本文件即可恢复完整守卫。
    """
    if _runtime_lock_held_by_service():
        pytest.skip(
            "生产服务运行中（持有运行时锁）：data/ 快照守卫不可靠，已跳过。停止服务后重跑可恢复。"
        )
    before = _snapshot_data_dir()
    yield
    after = _snapshot_data_dir()
    assert after == before, f"测试触碰了项目真实数据目录！\n之前：{before}\n之后：{after}"


# ---------------------------------------------------------------------------
# 配置层规范化单元测试（纯内存，无文件系统副作用）
# ---------------------------------------------------------------------------


def test_readme_default_relative_path_resolves_to_project_root() -> None:
    """README 默认相对路径应解析到项目根目录下的 data/moderation.db。"""
    url = _normalize_sqlite_url(README_DEFAULT_URL)
    expected = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'moderation.db'}"
    assert url == expected
    # 解析结果必须是绝对路径，且位于项目根目录下
    assert url.startswith("sqlite+aiosqlite:///")
    assert str(PROJECT_ROOT) in url


def test_relative_path_without_dot_slash() -> None:
    """不含 `./` 的相对路径也应解析到项目根目录。"""
    url = _normalize_sqlite_url("sqlite+aiosqlite:///data/moderation.db")
    expected = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'moderation.db'}"
    assert url == expected


def test_relative_path_resolves_same_regardless_of_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """从不同工作目录解析相对路径，结果必须一致（不依赖当前工作目录）。"""
    # 模拟从非项目目录启动：切换工作目录到 pytest 临时目录
    monkeypatch.chdir(tmp_path)

    s = Settings(database_url=README_DEFAULT_URL, _env_file=None)
    expected = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'moderation.db'}"
    assert s.database_url == expected


def test_absolute_unix_path_preserved() -> None:
    """Unix 绝对路径应保持不变。"""
    url = _normalize_sqlite_url("sqlite+aiosqlite:////var/lib/qqbot/moderation.db")
    assert url == "sqlite+aiosqlite:////var/lib/qqbot/moderation.db"


def test_windows_absolute_path_forward_slash_preserved() -> None:
    """Windows 绝对路径（正斜杠）应保持不变。"""
    url = _normalize_sqlite_url("sqlite+aiosqlite:///C:/data/moderation.db")
    assert url == "sqlite+aiosqlite:///C:/data/moderation.db"


def test_windows_absolute_path_backslash_preserved() -> None:
    """Windows 绝对路径（反斜杠）应保持不变。"""
    url = _normalize_sqlite_url(r"sqlite+aiosqlite:///C:\data\moderation.db")
    assert url == r"sqlite+aiosqlite:///C:\data\moderation.db"


def test_windows_drive_relative_path_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Windows 盘符相对路径 `C:relative\\db.db` 不是绝对路径，必须被明确拒绝。

    该路径依赖各盘符的当前工作目录，行为不可靠，配置加载时应直接报错，
    而不是原样保留或静默解析到错误位置。
    """
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Exception) as exc_info:
        Settings(database_url=r"sqlite+aiosqlite:///C:relative\db.db", _env_file=None)
    # pydantic ValidationError 会包含内层 ValueError 的信息
    assert "盘符相对路径" in str(exc_info.value)


def test_memory_database_preserved() -> None:
    """内存数据库 `:memory:` 应保持不变。"""
    url = _normalize_sqlite_url("sqlite+aiosqlite:///:memory:")
    assert url == "sqlite+aiosqlite:///:memory:"


def test_non_sqlite_url_preserved() -> None:
    """非 SQLite URL（如 PostgreSQL）应保持不变。"""
    url = _normalize_sqlite_url("postgresql+asyncpg://user:pass@host/db")
    assert url == "postgresql+asyncpg://user:pass@host/db"


def test_settings_normalizes_relative_url() -> None:
    """Settings 加载时应自动规范化相对 SQLite 路径。"""
    s = Settings(database_url=README_DEFAULT_URL, _env_file=None)
    expected = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'moderation.db'}"
    assert s.database_url == expected


# ---------------------------------------------------------------------------
# 子进程端到端测试：真实 Alembic CLI + 真实应用启动，全部使用 tmp_path
# ---------------------------------------------------------------------------


def _alembic_upgrade_cmd(ini_path: Path) -> list[str]:
    """构造跨目录可用的 Alembic CLI 命令（等价于 `alembic -c <ini> upgrade head`）。"""
    return [
        sys.executable,
        "-c",
        "from alembic.config import main; main()",
        "--raiseerr",
        "-c",
        str(ini_path),
        "upgrade",
        "head",
    ]


def _run_alembic_upgrade(ini_path: Path, cwd: Path, database_url: str) -> None:
    """在指定工作目录用子进程执行真实 `alembic upgrade head`。"""
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    result = subprocess.run(
        _alembic_upgrade_cmd(ini_path),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"alembic upgrade head 失败（cwd={cwd}）:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _sqlite_version(db: Path) -> str:
    """读取数据库中的 alembic_version。"""
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()
    assert row is not None, "缺少 alembic_version 表"
    return row[0]


def _get_head_revision() -> str:
    from app.db import get_head_revision

    return get_head_revision()


def _free_port() -> int:
    """获取一个空闲端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthz(port: int, timeout: float = 30.0) -> dict[str, str]:
    """轮询 /healthz 直到就绪。"""
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:  # noqa: BLE001 - 轮询期内的所有异常都重试
            last_err = e
            time.sleep(0.3)
    raise AssertionError(f"/healthz 在 {timeout}s 内未就绪: {last_err}")


def test_e2e_migrate_and_start_from_non_project_dirs(tmp_path: Path) -> None:
    """端到端回归（全部发生在 tmp_path）：

    1. 预先创建数据库文件（含哨兵内容），验证迁移不会删除已有文件。
    2. 从非项目目录执行真实 `alembic upgrade head`（子进程）。
    3. 从另一个非项目目录启动应用（子进程），确认连接同一数据库。
    """
    ini_path = PROJECT_ROOT / "alembic.ini"
    head = _get_head_revision()

    db = tmp_path / "e2e" / "moderation.db"
    db.parent.mkdir(parents=True)
    # 预先存在的数据库文件（合法空 SQLite 库 + 哨兵表）：
    # 迁移不得删除或替换它，只能在其中建表
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sentinel_marker (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    # 步骤1：从非项目目录（run_cwd）执行真实 Alembic 迁移
    run_cwd = tmp_path / "run"
    run_cwd.mkdir()
    _run_alembic_upgrade(ini_path, run_cwd, f"sqlite+aiosqlite:///{db}")

    # 预先存在的文件未被删除，哨兵表保留，且已迁移到 head
    assert db.exists(), "迁移删除了预先存在的数据库文件"
    conn = sqlite3.connect(db)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "sentinel_marker" in tables, "预先存在的数据库被替换，哨兵表丢失"
    assert _sqlite_version(db) == head

    # 步骤2：从另一个非项目目录（app_cwd）启动应用，连接同一数据库
    port = _free_port()
    app_cwd = tmp_path / "app_run"
    app_cwd.mkdir()
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db}"
    env["WEB_PORT"] = str(port)
    # 从非项目目录启动时，app 包需要通过 PYTHONPATH 定位
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.Popen(
        [sys.executable, "-m", "app"],
        cwd=app_cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        health = _wait_healthz(port)
        assert health["status"] == "ok"
        # 应用确实连接了同一个数据库：迁移版本仍为 head，文件未被替换
        assert db.exists()
        assert _sqlite_version(db) == head
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()

    # 步骤3：迁移/启动过程不应在非项目目录产生漂移的空库
    assert not (run_cwd / "data" / "moderation.db").exists()
    assert not (app_cwd / "data" / "moderation.db").exists()


def test_e2e_unmigrated_db_rejected_from_non_project_dir(tmp_path: Path) -> None:
    """未迁移的数据库必须拒绝启动（Alembic 是唯一建表路径）。

    从非项目目录创建全新空库后直接启动应用，应因缺少迁移记录而失败。
    """
    db = tmp_path / "fresh" / "moderation.db"
    db.parent.mkdir(parents=True)

    port = _free_port()
    app_cwd = tmp_path / "app_run"
    app_cwd.mkdir()
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db}"
    env["WEB_PORT"] = str(port)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.Popen(
        [sys.executable, "-m", "app"],
        cwd=app_cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # 应用应启动失败：健康检查永远不可用
        with pytest.raises(AssertionError, match="未就绪"):
            _wait_healthz(port, timeout=20.0)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()

    # 未迁移的空库文件可能已被连接创建，但绝不会有 alembic_version 表
    if db.exists():
        with pytest.raises(sqlite3.OperationalError):
            _sqlite_version(db)


def test_e2e_alembic_cli_works_from_any_cwd(tmp_path: Path) -> None:
    """`alembic.ini` 使用 %(here)s 后，从任意工作目录执行 Alembic CLI 都能成功。

    回归背景：`script_location = alembic` 与 `prepend_sys_path = .` 按
    当前工作目录解析，从非项目目录执行报
    `No 'script_location' key found in configuration`。
    """
    db = tmp_path / "cli" / "moderation.db"
    db.parent.mkdir(parents=True)

    # 从 tmp_path（非项目目录）执行
    _run_alembic_upgrade(PROJECT_ROOT / "alembic.ini", tmp_path, f"sqlite+aiosqlite:///{db}")

    assert db.exists()
    assert _sqlite_version(db) == _get_head_revision()
