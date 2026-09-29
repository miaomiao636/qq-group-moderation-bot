# ruff: noqa: E402, I001, F401, F811, SIM105
# Reviewer round-8 probe pack (8299ce8), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""8299ce8 synthetic-only checks for the new human-image review write path."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from PIL import Image

from app.moderation.image_hash import dhash64_file, to_hex
from scripts import apply_review_decisions as apply_tool
from scripts import image_allowlist_replay as replay_tool
from scripts import image_allowlist_seed as seed_tool
from scripts import image_review_sheet as sheet_tool
from tests.test_r132_review_image_tool_set_consistency import (
    add_decision,
    enabled_hashes,
    make_image,
    sandbox,
)


def make_batch(tmp_path, name="batch-synthetic", no="01", verdict="放行"):
    batch = tmp_path / name
    batch.mkdir()
    picture = batch / "reviewed.png"
    value = make_image(picture)
    digest = hashlib.sha256(picture.read_bytes()).hexdigest()
    row = (
        f"| {no} | 候选（未在生效名单） | `reviewed.png` | candidate | {digest[:12]} | "
        f"{to_hex(value)} | - | - | 1 | **0** | record_only:1 | ad:1 | synthetic | |\n"
    )
    (batch / "IMAGE_REVIEW.md").write_text("# Synthetic review\n" + row, encoding="utf-8")
    set_decision(batch, no, verdict)
    return batch, picture, value


def set_decision(batch, no, verdict):
    (batch / "DECISIONS.json").write_text(
        json.dumps({"decisions": {no: verdict}}, ensure_ascii=False), encoding="utf-8"
    )


def apply(batch, db):
    return apply_tool.main(["--batch", str(batch), "--db", str(db)])


def test_apply_does_not_authorize_replaced_file(sandbox, tmp_path):
    db, _, _, _ = sandbox
    batch, picture, reviewed = make_batch(tmp_path)
    Image.new("L", (64, 64), 0).save(picture)
    replacement = dhash64_file(picture)
    assert replacement is not None and replacement != reviewed
    result = apply(batch, db)
    assert to_hex(replacement) not in enabled_hashes(db), (
        "A different image replaced the reviewed bytes, but its new hash was enabled."
    )
    assert result != 0


def test_reject_existing_enabled_hash_actually_disables_it(sandbox, tmp_path):
    db, _, _, _ = sandbox
    batch, _, value = make_batch(tmp_path)
    assert apply(batch, db) == 0
    assert to_hex(value) in enabled_hashes(db)
    set_decision(batch, "01", "撤回")
    assert apply(batch, db) == 0
    assert to_hex(value) not in enabled_hashes(db)
    assert to_hex(value) in seed_tool.load_rejections(db)


def test_explicit_reapproval_reenables_previous_disabled_hash(sandbox, tmp_path):
    db, _, _, _ = sandbox
    batch, _, value = make_batch(tmp_path)
    assert apply(batch, db) == 0
    set_decision(batch, "01", "撤回")
    assert apply(batch, db) == 0
    set_decision(batch, "01", "放行")
    assert apply(batch, db) == 0
    assert to_hex(value) in enabled_hashes(db), "IMPORT_OK was reported but row remained disabled."


def test_generic_seed_import_cannot_silently_reverse_human_rejection(sandbox, tmp_path):
    db, _, _, _ = sandbox
    batch, picture, value = make_batch(tmp_path, verdict="撤回")
    assert apply(batch, db) == 0
    assert to_hex(value) in seed_tool.load_rejections(db)
    seed_tool.import_seeds(
        db=db,
        seeds=[(picture, "sample", "ordinary-seed-retry")],
        dry_run=False,
        operator="ordinary-seed",
    )
    assert to_hex(value) not in enabled_hashes(db), "Ordinary seed bypassed the persisted refusal."


def test_missing_reviewed_file_is_explicit_failure(sandbox, tmp_path):
    db, _, _, _ = sandbox
    batch, picture, _ = make_batch(tmp_path)
    picture.unlink()  # Synthetic temporary fixture only.
    assert apply(batch, db) != 0, "Skipped reviewed image still yielded IMPORT_OK and exit 0."


@pytest.mark.parametrize("stage", ["render", "apply"])
def test_three_digit_review_number_is_rendered_and_applied(sandbox, tmp_path, stage):
    db, _, _, _ = sandbox
    batch, _, value = make_batch(tmp_path, no="100")
    if stage == "render":
        output = sheet_tool.build(batch)
        assert 'data-no="100"' in output.read_text(encoding="utf-8")
    else:
        assert apply(batch, db) == 0
        assert to_hex(value) in enabled_hashes(db)


def test_corrupt_rejection_snapshot_not_silently_replaced(sandbox):
    db, _, _, _ = sandbox
    snapshot = seed_tool.rejection_snapshot_path(db)
    before = b'{"0000000000000001": '
    snapshot.write_bytes(before)
    try:
        seed_tool.record_rejection(db, "0000000000000002", operator="synthetic")
    except (OSError, ValueError, RuntimeError):
        pass
    assert snapshot.read_bytes() == before, "Malformed refusal history was silently overwritten."


def test_concurrent_rejections_preserve_both_writers(sandbox, monkeypatch):
    db, _, _, _ = sandbox
    snapshot = seed_tool.rejection_snapshot_path(db)
    snapshot.write_text("{}", encoding="utf-8")
    original_read = Path.read_text
    barrier = Barrier(2)

    def simultaneous_read(path, *args, **kwargs):
        data = original_read(path, *args, **kwargs)
        if path == snapshot:
            barrier.wait(timeout=5)
        return data

    with monkeypatch.context() as controlled:
        controlled.setattr(Path, "read_text", simultaneous_read)
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [
                pool.submit(seed_tool.record_rejection, db, value, operator="synthetic")
                for value in ("0000000000000001", "0000000000000002")
            ]
            for task in tasks:
                task.result(timeout=10)
    assert seed_tool.load_rejections(db) == {"0000000000000001", "0000000000000002"}


def test_missing_table_candidate_replay_honors_rejection_snapshot(sandbox):
    db, media, samples, _ = sandbox
    value = make_image(samples / "rejected.png")
    make_image(media / "rejected.png")
    add_decision(db, "rejected.png")
    seed_tool.record_rejection(db, to_hex(value), operator="synthetic")
    with sqlite3.connect(db) as con:
        con.execute("DROP TABLE image_allowlist")
    actual = {to_hex(value) for value, _ in replay_tool.load_whitelist(samples, db, media)}
    assert to_hex(value) not in actual, "Missing-table replay ignored the shared refusal snapshot."
