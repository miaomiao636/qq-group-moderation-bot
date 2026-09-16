"""R-114 F01：完整清理入口对链接根 fail-closed（主审探针转正式回归）。

主审探针 ``qqbot-r113-purge-entry-probe.py``（其 macOS 预期失败已复现）正式化：
- Windows 用 ``mklink /J`` 真建 junction，**创建失败不能当通过**；
- 控制用例：正常布局下过期文件仍被清理——防护不得把正常清理废掉。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"Could not create synthetic NTFS junction: {result.stderr}"
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.asyncio
async def test_real_cleanup_entry_preserves_unregistered_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db import Base
    from app.reports import cleanup
    from app.runtime import pipeline
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    root = tmp_path / "data"
    unknown = root / "unknown_keep"
    unknown.mkdir(parents=True)
    original = unknown / "synthetic.jpg"
    before = b"synthetic-only, not a real group message"
    original.write_bytes(before)
    os.utime(original, (0, 0))
    directory_link(root / "media", unknown)
    monkeypatch.setattr(pipeline, "MEDIA_DIR", root / "media")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine) as session:
            result = await cleanup.purge_expired(session)
        assert original.exists(), f"Unregistered original was deleted: {result}"
        assert original.read_bytes() == before
        assert result["media_files_deleted"] == 0
        assert result["managed_copy_files_deleted"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_cleanup_entry_still_deletes_expired_normal_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """控制：正常布局（无链接根）下过期文件仍被清理——防护不得一刀切拒绝。"""
    from app.db import Base
    from app.reports import cleanup
    from app.runtime import pipeline
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    media = tmp_path / "data" / "media"
    media.mkdir(parents=True)
    expired = media / "synthetic-expired.jpg"
    expired.write_bytes(b"synthetic-expired")
    os.utime(expired, (0, 0))
    monkeypatch.setattr(pipeline, "MEDIA_DIR", media)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine) as session:
            result = await cleanup.purge_expired(session)
        assert not expired.exists()
        assert result["media_files_deleted"] >= 1
    finally:
        await engine.dispose()
