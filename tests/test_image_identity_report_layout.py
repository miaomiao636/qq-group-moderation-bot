"""A2 identity reports retain readable Markdown headings and table rows."""

from scripts.image_decision_authority import export_snapshot

from tests import test_r132_review_identity_and_window as identity_cases

verifier = identity_cases.verifier


def test_identity_markdown_keeps_separate_heading_and_table_rows(verifier, tmp_path):
    batch, phash = identity_cases.make_batch(verifier)
    db = identity_cases.make_db(tmp_path, phash, batch)
    export_snapshot(db)
    code, report = identity_cases.run_identity(verifier, db)
    assert code == 0 and not report["mismatches"]
    markdown = next(verifier.OUT_DIR.glob("identity-*.md")).read_text(encoding="utf-8")
    lines = markdown.splitlines()
    assert lines[0].startswith("# ") and lines[1] == ""
    assert "## 逐条结果" in lines
    assert "| 哈希 | 批次 | 编号 | 文件 | 结论 | 状态 |" in lines
    assert markdown.endswith("\n")
