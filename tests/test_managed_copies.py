"""登记式副本清理（"15 天全副本工程"）回归：只删登记路径、保底、dry-run。"""

from __future__ import annotations

import os
import time
from pathlib import Path

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
