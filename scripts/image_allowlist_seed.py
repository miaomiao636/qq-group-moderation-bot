"""图片哈希白名单种子导入（负责人 2026-09-19：「确定放行」的图 + 历史放行图）。

种子来源两类：
1. ``--samples-dir``（默认 ``docs/evidence/allowlist-samples``）：负责人手动确认「确定放行」的原图；
2. ``--from-history``：历史判定里 ``has_miniprogram_code=true`` 且 verdict=allow、且原图仍在
   ``data/media`` 的记录（相当于"负责人已认可的那类图"的真实样本）。

行为：算 64 位 dHash → A2 权威事务写入决定和历史（phash 唯一，普通重复导入跳过）。
``--dry-run`` 只统计不写库。不改判定逻辑；派生 JSON 失败返回待重导出，已提交决定保留。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.moderation.image_hash import dhash64_file, to_hex  # noqa: E402

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def scan_samples(samples_dir: Path) -> list[tuple[Path, str, str]]:
    """返回 [(路径, 来源, 备注)]。"""
    if not samples_dir.is_dir():
        return []
    return [
        (path, "sample", path.stem[:64])
        for path in sorted(samples_dir.iterdir())
        if path.suffix.lower() in IMAGE_SUFFIXES
    ]


def scan_history(db: Path, media_dir: Path, *, limit: int = 5000) -> list[tuple[Path, str, str]]:
    """历史"带小程序码 + allow"且原图仍在盘的图片。只读数据库。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select detail_json from shadow_decisions where kind='image' "
        "and verdict='allow' order by created_at desc limit ?",
        (limit,),
    ).fetchall()
    found: dict[str, tuple[Path, str, str]] = {}
    for (detail_json,) in rows:
        try:
            detail = json.loads(detail_json or "{}")
        except ValueError:
            continue
        results = detail.get("ai_results") or []
        if not any(isinstance(r, dict) and r.get("has_miniprogram_code") for r in results):
            continue
        for entry in detail.get("media_files") or []:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str):
                continue
            path = media_dir / name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                found.setdefault(str(path), (path, "history", path.name[:64]))
    return list(found.values())


def load_excluded(path: Path) -> set[str]:
    """负责人审核后**排除**的哈希（小写十六进制）；文件不存在则为空集。"""
    if not path.is_file():
        return set()
    excluded: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip().lower()
        if value:
            excluded.add(value)
    return excluded


def disabled_hashes(db: Path) -> set[str]:
    """负责人**显式停用/排除**的行（``enabled=0``）——离线审核范围必须把它们剔除。

    表缺失/不可读时返回空集（调用方另有 `effective_hashes` 判断"无法判定"）。
    """
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute("select phash from image_allowlist where enabled=0").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return set()
    return {str(row[0]).lower() for row in rows}


def effective_state(db: Path) -> tuple[set[str] | None, str]:
    """生效名单 + **读取状态原因**（主审 R6-02）：``("ok")`` / ``("missing_table")`` /
    ``("bad_schema")`` / ``("unreadable")``。

    三态底层契约不变（表存在但为空 = 空集；缺表/缺列 = ``None`` = unavailable），
    但**上层报告必须写出原因**，不能把"缺迁移 / 坏结构"与"生效名单就是空的"混成一句结论。
    """
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None, "unreadable"
    try:
        try:
            con.execute("select phash from image_allowlist where enabled=1").fetchall()
        except sqlite3.OperationalError as exc:
            return None, "missing_table" if "no such table" in str(exc).lower() else "bad_schema"
    finally:
        con.close()
    from scripts.image_decision_authority import AuthorityError, read_authority

    try:
        authority = read_authority(db)
    except AuthorityError as exc:
        return None, str(exc).split(":", 1)[0].lower()
    return {key for key, row in authority.items() if row["enabled"]}, "ok"


def effective_hashes(db: Path) -> set[str] | None:
    """Approved assessment requires initialized consistent authority; otherwise unavailable."""
    return effective_state(db)[0]


def detail_blockers(detail: dict) -> list[str]:
    """**A06-R / R6-01 离线同源**：回放/导出必须与在线看**同一套**完整证据。

    在线判据是 `app.moderation.ai.attachment_reviews_unresolved`，覆盖：任一附件严重类别、
    任一附件未定论——**含二审异类 / 二审置信不足 / 二审与主审同模型（不独立）/ 孤儿二审**——
    以及 `evidence_vetoes`、附件缺失。
    离线此前只检查严重类别 / `needs_review` / `degraded_reason` / veto，于是"二审无效"的三类
    情形会被离线误报成"会改变判定"（主审 R6-01 实测 6 项）。现在**直接调同一个函数**。

    旧记录缺 `review_role`/`review_group` 等复核字段时无法判定是否已消疑 →
    标 `unresolved_unknown`（**保守**：计入例外），**绝不按默认阈值猜成已消疑**。
    """
    blockers: list[str] = []
    if detail.get("evidence_vetoes"):
        blockers.append("evidence_veto")
    raw = detail.get("ai_results")
    results = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    if any(str(item.get("category") or "") in ("porn", "violence") for item in results):
        blockers.append("attachment_category")
    if any(item.get("needs_review") or item.get("degraded_reason") for item in results):
        blockers.append("unresolved")
    if not results:
        return blockers
    # R6-01-R：缺**复核元数据**的旧记录无法判定是否已消疑 → 一律 `unresolved_unknown`
    # （**不得**静默返回空 blockers 当成"已消疑"）。
    if any("review_role" not in item or "review_group" not in item for item in results):
        blockers.append("unresolved_unknown")
        return blockers
    try:
        from app.moderation.ai import AIModerationResult, attachment_reviews_unresolved

        parsed = [AIModerationResult.model_validate(item) for item in results]
    except Exception:  # noqa: BLE001 - **解析失败也不能静默变成空 blockers**
        blockers.append("unresolved_unknown")
        return blockers
    # R6-01-R / R9-08-R：**必须**用该判定落库时的**真实阈值**做解释，且**必须**能证明那是真实政策：
    # 来源不是 `service`（例如服务未暴露政策、或旧记录缺来源）→ 一律 `unresolved_unknown`——
    # 既不拿函数默认值当"已消疑"，也不当"已否决"。
    policy = detail.get("review_policy")
    if not isinstance(policy, dict) or str(detail.get("review_policy_source") or "") != "service":
        blockers.append("unresolved_unknown")
        return blockers
    try:
        low = float(policy["secondary_review_low"])
        high = float(policy["secondary_review_high"])
        direct = float(policy["primary_direct_threshold"])
    except (KeyError, TypeError, ValueError):
        blockers.append("unresolved_unknown")
        return blockers
    if attachment_reviews_unresolved(
        parsed,
        None,
        primary_direct_threshold=direct,
        secondary_review_low=low,
        secondary_review_high=high,
    ):
        # local=None：只用结果里已持久化的 review_reason，不重算
        blockers.append("unresolved_secondary")
    return blockers


def rejection_snapshot_path(db: Path) -> Path:
    """拒绝快照文件路径（与库同目录、按库名区分）。

    R6-02-R：负责人明确判"撤回"的哈希必须有一份**可被所有工具读到**的记录——
    导入侧曾"只跳过 INSERT"，导出/回放看不到这次拒绝，候选就会把已排除的图又列出来。
    """
    return db.with_suffix(db.suffix + ".rejections.json")


@contextlib.contextmanager
def _snapshot_lock(path: Path, *, timeout: float = 10.0, stale: float = 60.0) -> Iterator[None]:
    """拒绝快照的**互斥**读写（跨线程/跨进程）。

    - 用 `O_EXCL` 建旁路锁文件；获取超时 → `RuntimeError`（**可见失败**，绝不静默丢记录）；
    - 锁文件时间戳超过 `stale` 秒视为崩溃残留并回收。
    """
    lock = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + timeout
    handle: int | None = None
    while handle is None:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"拒绝快照被占用，等待超时：{lock}") from None
            time.sleep(0.01)
    try:
        yield
    finally:
        os.close(handle)
        lock.unlink(missing_ok=True)


_DECISION_LOCK_LOCAL = threading.local()
_DECISION_THREAD_GUARD = threading.Lock()
_DECISION_THREADS: dict[str, threading.RLock] = {}


@contextlib.contextmanager
def decision_lock(db: Path, *, timeout: float = 30.0, stale: float = 120.0) -> Iterator[None]:
    """Per-database reentrant OS lock; crashes release it, age never steals it.

    The persistent lock file is deliberately not unlinked: otherwise another
    process could lock a different inode at the same path. The stale argument
    remains API-compatible but never permits stealing a live owner's lock.
    """
    key = os.path.normcase(str(db.resolve()))
    with _DECISION_THREAD_GUARD:
        local_lock = _DECISION_THREADS.setdefault(key, threading.RLock())
    if not local_lock.acquire(timeout=timeout):
        raise RuntimeError("decision lock busy; no changes committed")
    depths = getattr(_DECISION_LOCK_LOCAL, "depths", {})
    _DECISION_LOCK_LOCAL.depths = depths
    handle = None
    acquired = False
    try:
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        handle = open(str(db.resolve()) + ".review.lock", "a+b")  # noqa: SIM115 - lock lifetime spans yield/finally
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while not acquired:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("decision lock busy; no changes committed") from None
                time.sleep(0.02)
        depths[key] = 1
        try:
            yield
        finally:
            depths.pop(key, None)
    finally:
        if handle is not None:
            if acquired:
                if sys.platform == "win32":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        local_lock.release()


def _read_snapshot(path: Path) -> dict[str, object]:
    """**严格读取**：缺失 → 空对象；存在但损坏/结构异常 → 抛错。

    关键区分（R9-06）：**"缺失"与"损坏"不是一回事**——损坏时既不能当空集，
    也不能覆盖原件（否则负责人已记录的撤回会被静默抹掉）。
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    data = json.loads(raw.decode("utf-8"))  # 损坏 → ValueError，交由调用方显式处理
    if not isinstance(data, dict):
        raise ValueError(f"拒绝快照结构异常（应为对象）：{path}")
    return {str(key): value for key, value in data.items()}


def _write_snapshot(path: Path, data: dict[str, object]) -> None:
    """**原子替换**写入（同目录临时文件 + `os.replace`），不会留下半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def record_rejection(db: Path, value: str, *, source: str = "exclude", operator: str = "") -> None:
    """Commit one explicit rejection to SQLite, then export its derived snapshot."""
    from scripts.image_decision_authority import Decision, apply_decisions, versions

    value = value.lower()
    expected = versions(db, {value})
    apply_decisions(db, [Decision(value, "rejected", source, operator)], expected_versions=expected)


def record_approval(db: Path, value: str, *, source: str = "review", operator: str = "") -> None:
    """Commit one explicit reapproval; ordinary seed imports cannot call this implicitly."""
    from scripts.image_decision_authority import Decision, apply_decisions, versions

    value = value.lower()
    expected = versions(db, {value})
    apply_decisions(db, [Decision(value, "allowed", source, operator)], expected_versions=expected)


def _legacy_candidate_rejections_state(db: Path) -> tuple[set[str], str]:
    """→ (当前被撤回的哈希集合, 读取状态：``ok`` / ``missing`` / ``corrupt``)。

    "损坏/不可读"必须由调用方**显式报告**（记录不可用 ≠ 没有拒绝记录）。
    """
    path = rejection_snapshot_path(db)
    if not path.is_file():
        return set(), "missing"
    try:
        data = _read_snapshot(path)
    except (OSError, ValueError):
        return set(), "corrupt"
    rejected = {
        key
        for key, entry in data.items()
        if not isinstance(entry, dict) or str(entry.get("state", "rejected")) != "approved"
    }
    # R9-05-R/R9-06-R：**DB 是权威**（JSON 仅作派生/导出）——库里 `enabled=1` 的哈希即使
    # JSON 里还标着"撤回"，也视为已被**更新的明确重新批准**覆盖，避免双存储长期互相矛盾
    # （并发的撤回与重批都以"最后一次落库的决定"为准）。
    masked: set[str] = set()
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            masked = {
                str(row[0]).lower()
                for row in con.execute("select phash from image_allowlist where enabled=1")
            }
        finally:
            con.close()
    except sqlite3.Error:
        masked = set()
    return {key.lower() for key in rejected} - masked, "ok"


def load_rejections_state(db: Path) -> tuple[set[str], str]:
    """Current decisions come only from initialized DB authority."""
    from scripts.image_decision_authority import read_authority

    rows = read_authority(db)
    return {key for key, row in rows.items() if row["decision_state"] == "rejected"}, "ok"


def load_rejections(db: Path) -> set[str]:
    return load_rejections_state(db)[0]


def candidate_rejections(db: Path) -> set[str]:
    """Read-only legacy candidate filtering; never certifies approval or permits writes."""
    from scripts.image_decision_authority import AuthorityError

    try:
        return load_rejections(db)
    except AuthorityError as exc:
        if not str(exc).startswith("AUTHORITY_UNAVAILABLE"):
            raise
        return _legacy_candidate_rejections_state(db)[0]


def import_seeds(
    *,
    db: Path,
    seeds: list[tuple[Path, str, str]],
    dry_run: bool,
    operator: str,
    excluded: set[str] | None = None,
    reapproved: set[str] | None = None,
) -> tuple[int, int, int, int]:
    """Import a batch through one authority transaction; reject is persistent."""
    from scripts.image_decision_authority import Decision, apply_decisions, read_authority

    rows = read_authority(db)
    excluded = {v.lower() for v in (excluded or set())}
    reapproved = {v.lower() for v in (reapproved or set())}
    decisions = {v: Decision(v, "rejected", "exclude", operator) for v in excluded}
    added = duplicate = failed = skipped = 0
    seen: set[str] = set()
    for seed in seeds:
        path, source, note = seed[:3]
        computed = str(seed[3]).lower() if len(seed) > 3 and seed[3] else ""
        phash = None if computed else dhash64_file(path)
        if not computed and phash is None:
            failed += 1
            continue
        value = computed or to_hex(phash)
        row = rows.get(value)
        if value in excluded or (
            row and row["decision_state"] == "rejected" and value not in reapproved
        ):
            skipped += 1
            continue
        if row or value in seen:
            duplicate += 1
        else:
            added += 1
        if not row or value in reapproved:
            decisions[value] = Decision(value, "allowed", source, operator, f"{source}:{note}"[:64])
        seen.add(value)
    if not dry_run:
        expected = {
            key: int(rows[key]["decision_version"]) if key in rows else 0 for key in decisions
        }
        apply_decisions(db, list(decisions.values()), expected_versions=expected)
    return added, duplicate, failed, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="图片哈希白名单种子导入")
    parser.add_argument("--db", default=str(ROOT / "data" / "moderation.db"))
    parser.add_argument("--media-dir", default=str(ROOT / "data" / "media"))
    parser.add_argument(
        "--samples-dir", default=str(ROOT / "docs" / "evidence" / "allowlist-samples")
    )
    parser.add_argument("--from-history", action="store_true", help="同时导入历史放行图")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--operator", default="human:seed")
    parser.add_argument(
        "--exclude-file",
        default=str(ROOT / "docs" / "evidence" / "image-review" / "exclude_hashes.txt"),
        help="负责人审核后排除的哈希清单（每行一个十六进制值，# 开头为注释）",
    )
    args = parser.parse_args(argv)

    excluded = load_excluded(Path(args.exclude_file))
    print(f"排除清单：{len(excluded)} 条（{args.exclude_file}）")
    seeds = scan_samples(Path(args.samples_dir))
    print(f"负责人样本：{len(seeds)} 张")
    if args.from_history:
        history = scan_history(Path(args.db), Path(args.media_dir))
        print(f"历史放行图（原图仍在盘）：{len(history)} 张")
        seeds += history
    added, duplicate, failed, skipped = import_seeds(
        db=Path(args.db),
        seeds=seeds,
        dry_run=args.dry_run,
        operator=args.operator,
        excluded=excluded,
    )
    print(
        f"SEED_DONE added={added} duplicate={duplicate} failed={failed} "
        f"excluded={skipped} dry_run={args.dry_run}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
