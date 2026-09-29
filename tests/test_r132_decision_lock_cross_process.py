"""C03-R2-2（主审第十二轮，方案 B）：审核决定锁必须**跨进程**有效，且超时可见失败。

本文件是送审方对 `scripts.image_allowlist_seed.decision_lock` 的回归覆盖（不是主审交付件），
用真实子进程持锁、真实超时路径验证：

- 另一进程持锁时，本进程**拿不到**锁，等待到超时后抛 `RuntimeError`（不做任何改动）；
- 超时失败**不会**误删对方仍持有的锁；
- 对方释放后可以正常获取。

只用临时目录 + 合成空 DB 文件（锁文件名 `<db>.review.lock`），不读生产库、不写白名单。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from scripts import image_allowlist_seed as seed_tool

CHILD_HOLD = """
import pathlib
import sys
import time

from scripts.image_allowlist_seed import decision_lock

db = pathlib.Path(sys.argv[1])
release = pathlib.Path(sys.argv[2])
marked = pathlib.Path(sys.argv[3])
with decision_lock(db, timeout=5):
    marked.write_text("held", encoding="utf-8")
    for _ in range(1200):
        if release.exists():
            break
        time.sleep(0.05)
"""


def _wait_for(path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert path.exists(), f"子进程未在 {timeout}s 内标记持锁：{path}"


def test_decision_lock_blocks_other_processes_and_times_out(tmp_path):
    db = tmp_path / "synthetic.db"
    db.write_bytes(b"")
    release = tmp_path / "release"
    marked = tmp_path / "held"
    lock_path = Path(f"{db}.review.lock")

    child = subprocess.Popen(
        [sys.executable, "-c", CHILD_HOLD, str(db), str(release), str(marked)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not marked.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                break
            time.sleep(0.05)
        if not marked.exists():
            _out, err = child.communicate(timeout=10)
            pytest.fail(f"子进程未进入锁：rc={child.returncode} stderr={err}")
        started = time.monotonic()
        with (
            pytest.raises(RuntimeError, match="decision lock busy"),
            seed_tool.decision_lock(db, timeout=0.5),
        ):
            pytest.fail("另一进程持锁时不应拿到锁")
        assert time.monotonic() - started >= 0.4
        # 超时失败不得误删对方仍持有的锁（否则跨进程互斥就破了）。
        assert lock_path.exists()
    finally:
        release.write_text("1", encoding="utf-8")
        assert child.wait(timeout=30) == 0

    # 对方释放后，本进程可以正常进入。
    with seed_tool.decision_lock(db, timeout=5):
        pass
    # A2 retains the inode; OS ownership is released, never inferred from existence.
    assert lock_path.exists(), "固定锁文件应保留，避免并发进程锁住不同 inode"
