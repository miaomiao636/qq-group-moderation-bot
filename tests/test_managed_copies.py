"""登记式副本清理（"15 天全副本工程"）回归：只删登记路径、保底、dry-run。"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from app.reports.cleanup import plan_managed_copies, purge_managed_copies


def _mkfile(p: Path, days_ago: float) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    ts = time.time() - days_ago * 86400
    os.utime(p, (ts, ts))
    return p


def _make_tree(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    _mkfile(root / "media_snapshot_test" / "old.jpg", 20)
    _mkfile(root / "media_snapshot_test" / "new.jpg", 1)
    _mkfile(root / "media_snapshot_test" / "_frames" / "frame1.png", 20)
    _mkfile(root / "t002_media" / "a.jpg", 20)
    _mkfile(root / "backup_shadow_1.json", 20)
    _mkfile(root / "backup_shadow_2.json", 1)
    _mkfile(root / "_dbg_tmp" / "x.tmp", 5)
    # 备份集：老集 20 天前、新集 19 天前（两者都超期；保底应保住最新 1 个）
    _mkfile(root / "backups" / "set_old" / "db.bak", 20)
    _mkfile(root / "backups" / "set_new" / "db.bak", 19)
    # 未登记目录：绝不能触碰
    _mkfile(root / "unknown_dir" / "keep.txt", 60)
    return root


def test_purge_deletes_expired_registered_copies_only(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    stats = purge_managed_copies(root=root)
    assert stats["managed_copy_files_deleted"] >= 6
    assert not (root / "media_snapshot_test" / "old.jpg").exists()
    assert not (root / "media_snapshot_test" / "_frames" / "frame1.png").exists()
    assert not (root / "t002_media" / "a.jpg").exists()
    assert not (root / "backup_shadow_1.json").exists()
    assert not (root / "_dbg_tmp" / "x.tmp").exists()
    # 未到期保留
    assert (root / "media_snapshot_test" / "new.jpg").exists()
    assert (root / "backup_shadow_2.json").exists()
    # 备份保底：最新集保留、最老集被清
    assert (root / "backups" / "set_new" / "db.bak").exists()
    assert not (root / "backups" / "set_old" / "db.bak").exists()
    # 未登记目录不碰（60 天前文件也保留）
    assert (root / "unknown_dir" / "keep.txt").exists()


def test_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    report = plan_managed_copies(root=root)
    assert report["file_count"] >= 6
    names = {Path(str(item["path"])).name for item in report["files"]}
    assert "old.jpg" in names
    # 磁盘未被改动
    assert (root / "media_snapshot_test" / "old.jpg").exists()
    stats = purge_managed_copies(root=root, dry_run=True)
    assert stats["managed_copy_files_deleted"] == 0
    assert (root / "media_snapshot_test" / "old.jpg").exists()


def test_keep_min_entries_protects_latest_backup(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _mkfile(root / "backups" / "set_a" / "db.bak", 40)
    _mkfile(root / "backups" / "set_b" / "db.bak", 30)  # 最新（虽也超期）
    purge_managed_copies(root=root)
    assert (root / "backups" / "set_b" / "db.bak").exists()
    assert not (root / "backups" / "set_a" / "db.bak").exists()


def test_empty_dirs_removed_after_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _mkfile(root / "t002_media" / "sub" / "x.jpg", 20)
    stats = purge_managed_copies(root=root)
    assert stats["managed_copy_dirs_removed"] >= 1
    assert not (root / "t002_media" / "sub").exists()


# ---------- R-112 N01 回归：拒绝链接/重解析点绕行（2026-09-15 主审复现） ----------


def _make_dir_link(link: Path, target: Path) -> bool:
    """创建目录链接（POSIX symlink / Windows junction）；环境不支持时返回 False。"""
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        if os.name == "nt":
            r = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
            )
            return r.returncode == 0 and link.exists()
        return False


def test_alias_link_to_unregistered_dir_is_never_deleted(tmp_path: Path) -> None:
    """N01 实锤场景：登记名 → 根内未登记目录 的链接不得绕过删除边界。"""
    root = tmp_path / "data"
    unknown = root / "unknown_keep"
    _mkfile(unknown / "synthetic.json", 40)
    alias = root / "media_snapshot_alias"
    if not _make_dir_link(alias, unknown):
        pytest.skip("本环境不支持创建目录链接")
    plan = plan_managed_copies(root=root)
    assert plan["file_count"] == 0
    stats = purge_managed_copies(root=root)
    assert stats["managed_copy_files_deleted"] == 0
    assert (unknown / "synthetic.json").exists()


def test_nested_link_inside_registered_dir_is_not_followed(tmp_path: Path) -> None:
    """登记目录内部的链接：不进入、不删其目标；正常超期文件仍照删（对照）。"""
    root = tmp_path / "data"
    reg = root / "t002_media"
    _mkfile(reg / "own.jpg", 40)
    outside = root / "unknown_keep"
    _mkfile(outside / "keep.jpg", 40)
    if not _make_dir_link(reg / "nested_link", outside):
        pytest.skip("本环境不支持创建目录链接")
    stats = purge_managed_copies(root=root)
    assert stats["managed_copy_files_deleted"] == 1
    assert not (reg / "own.jpg").exists()
    assert (outside / "keep.jpg").exists()


def test_file_link_inside_registered_dir_not_deleted(tmp_path: Path) -> None:
    """登记目录内的文件链接：跳过该条目，不删其目标文件。"""
    root = tmp_path / "data"
    reg = root / "t002_media"
    reg.mkdir(parents=True)
    outside = root / "keep_target.jpg"
    _mkfile(outside, 40)
    link = reg / "linked.jpg"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("本环境不支持创建文件符号链接")
    stats = purge_managed_copies(root=root)
    assert stats["managed_copy_files_deleted"] == 0
    assert outside.exists()


def test_normal_registered_dir_still_cleaned_control(tmp_path: Path) -> None:
    """对照：无链接的正常登记目录，超期文件仍被清理（防护不误杀）。"""
    root = tmp_path / "data"
    _mkfile(root / "t002_media" / "old.jpg", 40)
    stats = purge_managed_copies(root=root)
    assert stats["managed_copy_files_deleted"] == 1
    assert not (root / "t002_media" / "old.jpg").exists()


# ---------- R-113 F01 回归：完整祖先链检查（主审合成复现：data/media → 未登记目录） ----------


def test_ancestor_link_to_unregistered_dir_is_never_deleted(tmp_path: Path) -> None:
    """F01 场景：登记路径的**祖先**是链接（media → unknown_keep），不得删除。"""
    root = tmp_path / "data"
    unknown = root / "unknown_keep"
    _mkfile(unknown / "_frames" / "synthetic.jpg", 40)
    if not _make_dir_link(root / "media", unknown):
        pytest.skip("本环境不支持创建目录链接")
    plan = plan_managed_copies(root=root)
    assert plan["file_count"] == 0
    stats = purge_managed_copies(root=root)
    assert stats["managed_copy_files_deleted"] == 0
    assert (unknown / "_frames" / "synthetic.jpg").exists()


def test_delete_precheck_rejects_parent_swapped_after_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F01 纵深：扫描返回路径在执行删除前被换到链接下 → 删除前复核必须拒绝。"""
    from app.reports import cleanup as cleanup_mod

    root = tmp_path / "data"
    unknown = root / "unknown_keep"
    _mkfile(unknown / "x.jpg", 40)
    link = root / "t002_media"
    if not _make_dir_link(link, unknown):
        pytest.skip("本环境不支持创建目录链接")
    entry = next(e for e in cleanup_mod._MANAGED_COPIES if e.pattern == "t002_media")
    monkeypatch.setattr(
        cleanup_mod, "_scan_managed_copies", lambda ts, r: [(entry, link / "x.jpg")]
    )
    stats = cleanup_mod.purge_managed_copies(root=root)
    assert stats["managed_copy_files_deleted"] == 0
    assert (unknown / "x.jpg").exists()


def test_normal_media_frames_still_cleaned_control(tmp_path: Path) -> None:
    """对照：正常 media/_frames（无链接）超期文件仍照删。"""
    root = tmp_path / "data"
    _mkfile(root / "media" / "_frames" / "frame.jpg", 40)
    stats = purge_managed_copies(root=root)
    assert stats["managed_copy_files_deleted"] == 1
    assert not (root / "media" / "_frames" / "frame.jpg").exists()
