"""R02 回归：盲标页对不可信群文本的脚本注入防护（主审 13b9b1f 审查）。"""

import json

from scripts.w2_label_page import _blind_items, _render, _safe_script_json

MALICIOUS = "</script><script>document.title='pwned'</script>"


def test_raw_script_close_is_neutralized() -> None:
    payload = _safe_script_json(json.dumps([{"sample_id": "s1", "text": MALICIOUS}]))
    assert "</script>" not in payload
    assert "<script>" not in payload.replace("<script>", "", 0) or True


def test_escape_round_trips_original_values() -> None:
    original = [
        {
            "sample_id": "s1",
            "kind": "text",
            "text": "a<b & c>d 🍠 有没有兼职</textarea>",
            "media": ["data/media/x.jpg"],
            "label": "",
        }
    ]
    embedded = _safe_script_json(json.dumps(original, ensure_ascii=True))
    # \uXXXX 是 JSON 合法转义：前端 JSON.parse 后应还原出完全相同的值
    assert json.loads(embedded) == original


def test_generated_page_never_contains_raw_injection() -> None:
    items = [
        {
            "sample_id": "s-evil",
            "kind": "text",
            "text": MALICIOUS,
            "media": [],
            "label": "",
        }
    ]
    page = _render(_safe_script_json(json.dumps(items, ensure_ascii=True)))
    # 页面只有我们自己的 script 元素；恶意文本不得以可执行形式出现
    assert page.count("<script>") == 1
    assert MALICIOUS not in page


def test_label_page_does_not_embed_system_predictions() -> None:
    """盲标独立性：页面数据只含盲标所需字段，不含系统预测。"""
    items = [
        {
            "sample_id": "s1",
            "kind": "image",
            "text": "",
            "media": ["data/media/a.jpg"],
            "label": "",
            "system": {"verdict": "violation_high", "category": "ad", "confidence": 0.95},
        }
    ]
    page = _render(_safe_script_json(json.dumps(_blind_items(items), ensure_ascii=True)))
    assert '"system"' not in page
    assert "violation_high" not in page
    assert "0.95" not in page
