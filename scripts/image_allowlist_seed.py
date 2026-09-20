"""图片哈希白名单种子导入（负责人 2026-09-19：「确定放行」的图 + 历史放行图）。

种子来源两类：
1. ``--samples-dir``（默认 ``docs/evidence/allowlist-samples``）：负责人手动确认「确定放行」的原图；
2. ``--from-history``：历史判定里 ``has_miniprogram_code=true`` 且 verdict=allow、且原图仍在
   ``data/media`` 的记录（相当于"负责人已认可的那类图"的真实样本）。

行为：算 64 位 dHash → ``INSERT OR IGNORE``（phash 唯一，重复导入不会重复入库）。
``--dry-run`` 只统计不写库。**只写白名单表，不改判定逻辑**（判定是否使用白名单由 shadow/enforce 开关决定）。
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
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
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
            rows = con.execute("select phash from image_allowlist where enabled=1").fetchall()
        except sqlite3.OperationalError as exc:
            return None, "missing_table" if "no such table" in str(exc).lower() else "bad_schema"
    finally:
        con.close()
    return {str(row[0]).lower() for row in rows}, "ok"


def effective_hashes(db: Path) -> set[str] | None:
    """**生效名单**：已导入且 ``enabled=1`` 的哈希集合（A05-R）。

    导入 / 回放 / 导出 / shadow **必须读同一集合**：有生效名单就用它；
    没有（尚未导入）时离线工具只能输出**候选**，不得声称"已批准 / 可直接启用"。
    """
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute("select phash from image_allowlist where enabled=1").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    # **表存在但为空 ≠ 表缺失**（主审探针）：空集合表示"生效名单存在且为空"（默认导入按设计什么都没加、
    # 或被负责人停用/排除），离线工具必须原样输出 ∅；只有表缺失/不可读（sqlite 错误）才返回 None
    # 表示"无法判定"，此时才退化为候选。此前 `values or None` 把两者混为一谈，于是**已被停用或被排除的
    # 样本会被重新当成候选报出来**，与"生效名单优先"的承诺不符。
    return {str(row[0]).lower() for row in rows}


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


@contextlib.contextmanager
def decision_lock(db: Path, *, timeout: float = 30.0, stale: float = 120.0) -> Iterator[None]:
    """审核决定/补偿的**跨进程串行化边界**（C03-R2-2，主审第十二轮）。

    覆盖范围（全链同一把锁）：**前态读取 → DB 变更 → 决定快照发布 → 失败补偿**。
    此前"补偿读快照 → commit"与"别处的 JSON 发布"没有共同边界，所以再多的复读也可能
    被后续成功决定穿过（主审反例：最终 JSON=rejected、DB=enabled=1）。

    - **跨进程**：`O_CREAT|O_EXCL` 锁文件（POSIX 与 Windows 同样可用），
      不只挡线程，也不依赖 SQLite 写锁；
    - **可重入**：同进程同线程嵌套调用只累加计数（`apply_review_decisions` 会在锁内
      调用本模块的 `import_seeds`），避免自锁死；
    - **超时即显式失败**：等待 `timeout` 秒后抛 `RuntimeError`（本次**不做任何改动**），
      绝不静默继续；锁文件超过 `stale` 秒视为崩溃残留并回收；
    - **锁顺序**：本锁（外）→ 拒绝快照锁 `_snapshot_lock`（内）；不得反向获取。
    """
    depth = getattr(_DECISION_LOCK_LOCAL, "depth", 0)
    if depth:
        _DECISION_LOCK_LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _DECISION_LOCK_LOCAL.depth = depth
        return
    path = Path(f"{db}.review.lock")
    deadline = time.monotonic() + timeout
    handle: int | None = None
    while handle is None:
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale:
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"审核决定锁被占用，等待 {timeout:.0f}s 超时：{path}"
                    "（另有审核/补偿进程仍持有；本次未做任何改动，可稍后重试）"
                ) from None
            time.sleep(0.02)
    _DECISION_LOCK_LOCAL.depth = 1
    try:
        with contextlib.suppress(OSError):
            os.write(handle, f"pid={os.getpid()} at={time.time():.0f}\n".encode())
        yield
    finally:
        _DECISION_LOCK_LOCAL.depth = 0
        os.close(handle)
        path.unlink(missing_ok=True)


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
    """把**已应用的拒绝**写进快照（锁内读改写 + 原子替换；保留最早来源与完整历史）。"""
    path = rejection_snapshot_path(db)
    key = str(value).lower()
    with _snapshot_lock(path):
        data = _read_snapshot(path)
        entry = data.get(key)
        if isinstance(entry, dict) and str(entry.get("state", "rejected")) == "rejected":
            return  # 已处于"撤回"状态：不覆盖最早记录
        now = datetime.now(UTC).isoformat(timespec="seconds")
        history = list(entry.get("history") or []) if isinstance(entry, dict) else []
        first = entry.get("first_rejected") if isinstance(entry, dict) else ""
        data[key] = {
            "state": "rejected",
            "source": source,
            "operator": operator,
            "at": now,
            "first_rejected": first or now,
            "history": [
                *history,
                {"state": "rejected", "at": now, "source": source, "operator": operator},
            ],
        }
        _write_snapshot(path, data)


def record_approval(db: Path, value: str, *, source: str = "review", operator: str = "") -> None:
    """**显式重新批准**（R9-05）：最新人工决定置为"放行"，**保留**此前拒绝历史。

    与"普通重复导入"区分：普通导入必须**尊重**拒绝，只有这条路能撤销拒绝。
    """
    path = rejection_snapshot_path(db)
    key = str(value).lower()
    with _snapshot_lock(path):
        data = _read_snapshot(path)
        entry = data.get(key)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        history = list(entry.get("history") or []) if isinstance(entry, dict) else []
        first = entry.get("first_rejected") if isinstance(entry, dict) else ""
        data[key] = {
            "state": "approved",
            "source": source,
            "operator": operator,
            "at": now,
            "first_rejected": first or "",
            "history": [
                *history,
                {"state": "approved", "at": now, "source": source, "operator": operator},
            ],
        }
        _write_snapshot(path, data)


def load_rejections_state(db: Path) -> tuple[set[str], str]:
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


def load_rejections(db: Path) -> set[str]:
    """读取**当前**处于"撤回"状态的哈希（最新决定为放行则不计入）。"""
    return load_rejections_state(db)[0]


def import_seeds(
    *,
    db: Path,
    seeds: list[tuple[Path, str, str]],
    dry_run: bool,
    operator: str,
    excluded: set[str] | None = None,
    reapproved: set[str] | None = None,
    pre_commit: Callable[[], None] | None = None,
    write_credential: dict[str, tuple[int, int, str] | None] | None = None,
) -> tuple[int, int, int, int]:
    """写入白名单的**唯一入口**：整个写入（含决定快照发布）都在 `decision_lock(db)` 内串行化。

    C03-R2-2（主审第十二轮）：DB 变更与决定快照发布必须与"其它审核/补偿"处于**同一个**
    串行化边界——只锁最后一段挡不住"DB 已提交、只剩 JSON 写盘"的并发审核。锁可重入，
    所以 `apply_review_decisions` 在锁内再调用本函数不会自锁。`dry_run` 只读，不加锁。
    """
    if dry_run:
        return _import_seeds_impl(
            db=db,
            seeds=seeds,
            dry_run=True,
            operator=operator,
            excluded=excluded,
            reapproved=reapproved,
            pre_commit=pre_commit,
            write_credential=write_credential,
        )
    with decision_lock(db):
        return _import_seeds_impl(
            db=db,
            seeds=seeds,
            dry_run=False,
            operator=operator,
            excluded=excluded,
            reapproved=reapproved,
            pre_commit=pre_commit,
            write_credential=write_credential,
        )


def _import_seeds_impl(
    *,
    db: Path,
    seeds: list[tuple[Path, str, str]],
    dry_run: bool,
    operator: str,
    excluded: set[str] | None = None,
    reapproved: set[str] | None = None,
    pre_commit: Callable[[], None] | None = None,
    write_credential: dict[str, tuple[int, int, str] | None] | None = None,
) -> tuple[int, int, int, int]:
    """写入白名单；返回 (新增, 已存在(重复), 计算失败, 被排除清单跳过)。

    - `seeds` 元素可为 3 元组，或 4 元组 `(path, source, note, 已验证的哈希十六进制)`——
      后者用于**批准工具**：它已在同一份字节上校验过 SHA-256/dHash，这里不再二次读盘；
    - `excluded`：负责人判"撤回"的哈希 → 停用既有行 + 写拒绝快照；
    - `reapproved`（R9-05）：**显式重新批准**——只有这些哈希可以覆盖拒绝快照并重新启用；
      普通重复导入（`excluded`/`reapproved` 都没提到、但快照里记着"撤回"的图）必须**尊重拒绝**；
    - `write_credential`（C03-R1，主审第十一轮）：在**同一个写事务内**回填每个涉及的哈希
      「写入后的完整行状态」（``(id, enabled, note)``／行不存在 → ``None``）。
      这是"这一行确实由本次操作写入"的**凭据**——提交后另起连接读到的状态可能已被
      外部或后续写入替换，不能当归属证明。
    """
    excluded = excluded or set()
    reapproved = {value.lower() for value in (reapproved or set())}
    touched: set[str] = set(excluded)
    if not dry_run:
        # R9-06-R：拒绝快照**损坏/不可读**时，任何写入前就阻断——坏状态不得被当成空集。
        _rejected_now, snapshot_state = load_rejections_state(db)
        if snapshot_state == "corrupt":
            raise ValueError(
                f"拒绝快照损坏/不可读：{rejection_snapshot_path(db)}"
                "（拒绝写入；先人工修复或隔离该文件）"
            )
    persisted_rejected = load_rejections(db)
    added = duplicate = failed = skipped = 0
    con = None if dry_run else sqlite3.connect(db)
    try:
        for seed in seeds:
            path, source, note = seed[0], seed[1], seed[2]
            precomputed = str(seed[3]).lower() if len(seed) > 3 and seed[3] else ""
            if precomputed:
                value = precomputed
            else:
                phash = dhash64_file(path)
                if phash is None:
                    failed += 1
                    print(f"  [跳过] 无法计算哈希：{path.name}")
                    continue
                value = to_hex(phash)
            touched.add(value)
            if value in persisted_rejected and value not in reapproved and value not in excluded:
                skipped += 1
                print(f"  [尊重已记录的撤回] {value}（普通导入不得静默撤销人工结论）")
                continue
            if value in excluded:
                skipped += 1
                print(f"  [排除] 负责人在审核清单中判为撤回：{value} {path.name}")
                if con is not None:
                    # A05：排除必须**真正停用**既有启用条目——只跳过 INSERT 等于没撤权
                    con.execute(
                        "update image_allowlist set enabled=0, "
                        "note = note || ';excluded:2026-09-19' "
                        "where phash = ? and enabled = 1",
                        (value,),
                    )
                    # R6-02-R：同时写**拒绝快照**（导出/回放的候选收集与它共用一份）。
                    record_rejection(db, value, source="exclude", operator=operator)
                continue
            if dry_run:
                added += 1
                print(f"  [dry-run] {value} {source} {path.name}")
                continue
            assert con is not None
            cursor = con.execute(
                "insert or ignore into image_allowlist "
                "(phash, note, source, enabled, created_at, created_by) values (?,?,?,1,?,?)",
                (value, f"{source}:{note}"[:64], source, datetime.now(UTC).isoformat(), operator),
            )
            if cursor.rowcount:
                added += 1
            else:
                duplicate += 1
                if value in reapproved:
                    # R9-05：**显式重新批准**必须把此前被撤回的行重新启用——`INSERT OR IGNORE`
                    # 只会"什么都不做"，于是"第三次放行"永远不生效。
                    con.execute(
                        "update image_allowlist set enabled=1, "
                        "note = note || ';reapproved:2026-09-19' where phash = ? and enabled = 0",
                        (value,),
                    )
                    print(f"  [重新批准] {value} 已重新启用（保留此前撤回历史）")
        if con is not None:
            # A05：即使该哈希不是本次种子（例如上一批已导入后才被判撤回），
            # 也必须按排除清单停用——保证"生效名单"与负责人结论一致。
            for value in sorted(excluded):
                con.execute(
                    "update image_allowlist set enabled=0, note = note || ';excluded:2026-09-19' "
                    "where phash = ? and enabled = 1",
                    (value,),
                )
            if write_credential is not None:
                # C03-R1：**写事务内**记录写入后的行状态（凭据），供失败补偿做归属判定。
                for value in sorted(touched):
                    row = con.execute(
                        "select id, enabled, note from image_allowlist where phash=?", (value,)
                    ).fetchone()
                    write_credential[value] = (
                        None
                        if row is None
                        else (int(row[0] or 0), int(row[1] or 0), str(row[2] or ""))
                    )
            if pre_commit is not None:
                # R9-05-R/R9-06-R：**先让"决定/拒绝快照"落盘，再提交 DB**——
                # 任一步失败都不会留下"已提交 enabled=1 但没有对应决定记录"的部分生效。
                pre_commit()
            con.commit()
    finally:
        if con is not None:
            con.close()
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
