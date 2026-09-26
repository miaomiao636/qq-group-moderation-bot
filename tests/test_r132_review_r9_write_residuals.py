# ruff: noqa: E402, I001, F401, F811, SIM105, S101
# A2 REGISTERED ADAPTATION: original bytes are sealed under docs/evidence/authority-a2-20260922/legacy-probes/.
# Historical nodeids are retained; current contracts and every changed AST node are registered in docs/2026-09-22-authority-a2-adaptations.md.
# Reviewer round-9 probe pack (dbd80a5); promoted into the repo suite.
# Registered adaptation (only one, scheduler only):
#   `test_concurrent_reject_and_reapprove_share_one_authoritative_state` used to run
#   the second operation WHILE the first was paused inside its snapshot publish. With
#   the cross-process decision lock (reviewer batch-12 option B) that interleaving is
#   UNREACHABLE: the second operation is reliably BLOCKED until the first finishes.
#   The test now asserts blocking + sequential completion; its FINAL assertion
#   ("two successful operations must not leave the hash enabled and rejected at the
#   same time") is unchanged. See docs/2026-09-20-r132-round14-review-submission.md.
# Everything else in this file is verbatim.
"""dbd80a5: synthetic-only residual checks for reviewer R9-04 through R9-07."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from PIL import Image

from app.moderation.image_hash import to_hex
from scripts import apply_review_decisions as apply_tool
from scripts import image_decision_authority as authority
from scripts import image_allowlist_seed as seed_tool
from tests.test_r132_review_image_tool_set_consistency import enabled_hashes, sandbox
from tests.test_r132_review_review_write_contract import apply, make_batch, set_decision


def replace_identity(batch, sha=None, dhash=None):
    manifest = batch / "IMAGE_REVIEW.md"
    lines = manifest.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("| 01 |"):
            cells = line.split("|")
            if sha is not None:
                cells[5] = f" {sha} "
            if dhash is not None:
                cells[6] = f" {dhash} "
            lines[i] = "|".join(cells)
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize("marker", ["", "-"])
def test_missing_identity_cannot_reenable_replaced_file(sandbox, tmp_path, marker):
    db, _, _, _ = sandbox
    batch, picture, _ = make_batch(tmp_path)
    replace_identity(batch, sha=marker, dhash=marker)
    Image.new("L", (64, 64), 0).save(picture)
    result = apply(batch, db)
    assert enabled_hashes(db) == set(), "Missing identity checks were silently bypassed."
    assert result != 0


def test_sha_prefix_contract_is_not_arbitrary_length(sandbox, tmp_path):
    db, _, _, _ = sandbox
    batch, picture, _ = make_batch(tmp_path)
    replace_identity(batch, sha=hashlib.sha256(picture.read_bytes()).hexdigest()[:1])
    result = apply(batch, db)
    assert result != 0 and enabled_hashes(db) == set(), "One hex digit was accepted as SHA binding."


@pytest.mark.parametrize("sha_length", [12, 64])
def test_valid_sha_forms_use_the_same_read_bytes(sandbox, tmp_path, monkeypatch, sha_length):
    db, _, _, _ = sandbox
    batch, picture, value = make_batch(tmp_path)
    replace_identity(batch, sha=hashlib.sha256(picture.read_bytes()).hexdigest()[:sha_length])
    original = Path.read_bytes
    reads = 0

    def single_read(path):
        nonlocal reads
        if path == picture:
            reads += 1
            assert reads == 1, "Approved image was reread after validation."
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", single_read)
    assert apply(batch, db) == 0
    assert reads == 1 and enabled_hashes(db) == {to_hex(value)}


def test_missing_decision_blocks_all_writes(sandbox, tmp_path):
    db, _, _, _ = sandbox
    batch, _, _ = make_batch(tmp_path)
    (batch / "DECISIONS.json").write_text('{"decisions": {}}', encoding="utf-8")
    assert apply(batch, db) != 0
    assert enabled_hashes(db) == set()
    assert not seed_tool.rejection_snapshot_path(db).exists()


def test_conflicting_canonical_decision_numbers_are_rejected(sandbox, tmp_path):
    db, _, _, _ = sandbox
    batch, _, _ = make_batch(tmp_path)
    (batch / "DECISIONS.json").write_text(
        json.dumps({"decisions": {"01": "撤回", "1": "放行"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = apply(batch, db)
    assert result != 0 and enabled_hashes(db) == set(), "Conflicting aliases silently last-wins."


def test_corrupt_snapshot_blocks_generic_import(sandbox, tmp_path):
    db, _, _, _ = sandbox
    _, picture, value = make_batch(tmp_path)
    snapshot = seed_tool.rejection_snapshot_path(db)
    raw = b'{"broken":'
    snapshot.write_bytes(raw)
    assert authority.export_state(db) == "corrupt"
    seed_tool.import_seeds(
        db=db, seeds=[(picture, "sample", "synthetic")], dry_run=False, operator="synthetic"
    )
    assert to_hex(value) in enabled_hashes(db)
    assert authority.export_state(db) == "ok"
    assert [p.read_bytes() for p in snapshot.parent.glob(snapshot.name + ".invalid-*")] == [raw]


@pytest.mark.parametrize("fault", ["corrupt_snapshot", "snapshot_write_error"])
def test_snapshot_failure_cannot_leave_committed_approval(sandbox, tmp_path, monkeypatch, fault):
    db, _, _, _ = sandbox
    batch, _, value = make_batch(tmp_path)
    snapshot = seed_tool.rejection_snapshot_path(db)
    if fault == "corrupt_snapshot":
        raw = b'{"broken":'
        snapshot.write_bytes(raw)
        assert apply(batch, db) == 0
        assert [p.read_bytes() for p in snapshot.parent.glob(snapshot.name + ".invalid-*")] == [raw]
    else:

        def fail(*args, **kwargs):
            raise PermissionError("synthetic snapshot write denied")

        monkeypatch.setattr(seed_tool, "_write_snapshot", fail)
        assert apply(batch, db) == 5
    assert to_hex(value) in enabled_hashes(db)
    row = authority.read_authority(db)[to_hex(value)]
    assert (row["decision_state"], row["decision_version"]) == ("allowed", 1)


def test_concurrent_reject_and_reapprove_share_one_authoritative_state(
    sandbox, tmp_path, monkeypatch
):
    db, _, _, _ = sandbox
    approve_batch, _, value = make_batch(tmp_path, name="batch-approve")
    reject_batch, _, same = make_batch(tmp_path, name="batch-reject", verdict="撤回")
    assert same == value and apply(approve_batch, db) == 0
    rejecting_committed = Event()
    finish_rejection = Event()
    original = authority.export_snapshot

    def paused_rejection(*args, **kwargs):
        rejecting_committed.set()  # Called only after import_seeds committed enabled=0.
        assert finish_rejection.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(authority, "export_snapshot", paused_rejection)
    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(apply, reject_batch, db)
        second = None
        try:
            assert rejecting_committed.wait(10)
            # 主审第十二轮（方案 B，已登记适配）：审核已全链串行化（前态读取 → DB 变更 →
            # 快照发布 → 补偿），"B 暂停在发布中、A 同时完整执行"的交错**不可达**。
            # 这里验证第二操作被**可靠阻塞**，待第一操作完成后再顺序执行。
            second = pool.submit(apply, approve_batch, db)
            time.sleep(0.5)
            assert not second.done(), "second operation must wait for the decision lock"
        finally:
            finish_rejection.set()
        assert task.result(timeout=30) == 0
        assert second is not None and second.result(timeout=30) == 0
    assert not (enabled_hashes(db) & seed_tool.load_rejections(db)), (
        "Two successful operations left the same hash enabled and currently rejected."
    )


def test_snapshot_mutex_preserves_cross_process_writers(sandbox):
    db, _, _, _ = sandbox
    script = (
        "import sys; from pathlib import Path; "
        "from scripts.image_allowlist_seed import record_rejection; "
        "db=Path(sys.argv[1]); start=int(sys.argv[2]); "
        "[(record_rejection(db, f'{n:016x}', operator='synthetic-child')) "
        "for n in range(start,start+5)]"
    )
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(db), str(start)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for start in (1, 6)
    ]
    for child in children:
        stdout, stderr = child.communicate(timeout=20)
        assert child.returncode == 0, stdout + stderr
    assert seed_tool.load_rejections(db) == {f"{n:016x}" for n in range(1, 11)}
