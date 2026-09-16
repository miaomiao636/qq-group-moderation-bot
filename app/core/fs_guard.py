"""文件系统边界防护：符号链接 / Windows junction / 重解析点一律 fail-closed。

R-112 N01（登记副本清理）与 R-114 F01（普通媒体清理）共用同一套边界检查——
普通媒体与登记副本删除必须使用一致的根/祖先链保护，避免"只修了一条路径"。
无法确认属性（OSError）时按链接处理，拒绝删除/遍历。
"""

from __future__ import annotations

import stat
from pathlib import Path

_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_link_like(p: Path) -> bool:
    """符号链接 / Windows junction / 其他重解析点——一律拒绝（R-112 N01）。

    登记名若被链接到根内未登记目录，仅校验 resolve 后仍在 data 根内，
    会跟随链接删除未登记目录的原文件，违反 D-026"不得按未知目录批量删除"。
    遍历与删除前均须先过此关；无法确认属性时按链接处理（fail-closed）。
    """
    try:
        if p.is_symlink():
            return True
        st = p.lstat()
    except OSError:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)


def chain_has_link(root: Path, target: Path) -> bool:
    """从 root 到 target 的**每一级**（含 root 与 target）检查链接/重解析点（R-113 F01）。

    旧实现只查叶节点：`data/media`（祖先）是链接时无法发现，遍历会沿链接
    删除未登记目录的原文件（主审合成复现：media → unknown_keep 后
    media/_frames 被错删）。这里逐级核对，任一级为链接即整体拒绝
    （fail-closed）；目标不在根内同样拒绝。
    """
    try:
        rel = target.relative_to(root)
    except ValueError:
        return True
    current = root
    if is_link_like(current):
        return True
    for part in rel.parts:
        current = current / part
        if is_link_like(current):
            return True
    return False
