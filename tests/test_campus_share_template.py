"""Approved share-card template: content evidence and source eligibility are separate."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from app.moderation.ai import AIProviderError, merge_ai_evidence, provider_payload_to_result
from app.moderation.campus_source import confirmed_campus_source
from app.moderation.wall_pair import _confirmed_sources

from tests.test_campus_source_policy import local


def card(**changes):
    value = dict(
        template_id="approved_share_card_v1",
        status="matched",
        logo_matches=True,
        footer_text="我正在看这个，觉得不错！\n长按识别小程序，一起看吧~",
        layout_matches=True,
        footer_miniprogram_code=True,
    )
    value.update(changes)
    return value


def result(template=None, *, category="ad", source="vision", **changes):
    payload = dict(
        category=category,
        confidence=0.99,
        needs_review=False,
        evidence="正文为合成兼职宣传，内嵌普通个人方码；右下为认可分享卡页脚。",
        has_miniprogram_code=True,
        campus_wall_source=None,
        campus_share_card=card() if template is None else template,
    )
    payload.update(changes)
    return provider_payload_to_result(
        payload,
        model_id="synthetic-template",
        prompt_version="test",
        provider="synthetic",
        source=source,
    )


@pytest.mark.parametrize("category", [None, "ad", "fraud"])
def test_template_allows_without_inventing_brand_and_preserves_raw_category(category):
    observed = result(category=category)
    assert observed.category == category
    assert observed.campus_wall_source is None
    assert confirmed_campus_source(observed.model_dump())
    decision = merge_ai_evidence(local(category or "ad"), [observed])
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []
    assert observed.category == category


@pytest.mark.parametrize(
    "changes",
    [
        {"logo_matches": False},
        {"layout_matches": False},
        {"footer_miniprogram_code": False},
        {"footer_text": "长按识别小程序，一起看吧~"},
        {"footer_text": "我正在看这个，觉得不错！"},
        {"status": "uncertain"},
    ],
)
def test_incomplete_template_is_manual_not_a_source(changes):
    observed = result(card(**changes))
    assert observed.needs_review is True
    assert not confirmed_campus_source(observed.model_dump())
    decision = merge_ai_evidence(
        local().model_copy(update={"verdict": "allow", "category": None}), [observed]
    )
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []
    assert _confirmed_sources({"ai_results": [observed.model_dump()]}) == []


@pytest.mark.parametrize("field", ["logo_matches", "layout_matches", "footer_miniprogram_code"])
@pytest.mark.parametrize("value", ["true", 1, None, []])
def test_template_boolean_is_strict(field, value):
    with pytest.raises(AIProviderError):
        result(card(**{field: value}))


@pytest.mark.parametrize(
    "template", [True, [], {"template_id": "unapproved"}, card(status="yes"), card(extra="x")]
)
def test_invalid_template_contract_rejected(template):
    with pytest.raises(AIProviderError):
        result(template)


def test_footer_punctuation_and_spaces_can_vary():
    observed = result(card(footer_text=" 我正在看这个, 觉得不错! \n长按识别小程序，一起看吧～ "))
    assert confirmed_campus_source(observed.model_dump())


def test_text_channel_cannot_claim_template():
    observed = result(source="text")
    assert not confirmed_campus_source(observed.model_dump())
    assert observed.model_dump().get("campus_share_card") is None
    assert merge_ai_evidence(local(), [observed]).verdict == "violation_high"


def test_clear_other_template_uses_normal_review():
    observed = result(
        card(status="not_matched", logo_matches=False, layout_matches=False, footer_text="")
    )
    assert observed.needs_review is False
    assert merge_ai_evidence(local(), [observed]).verdict == "violation_high"


@pytest.mark.parametrize("category", ["porn", "violence"])
def test_template_does_not_override_severe_content(category):
    decision = merge_ai_evidence(local(category), [result(category=category)])
    assert decision.verdict == "violation_high"
    assert "recall" in decision.recommended_actions


def test_template_cannot_launder_another_attachment():
    other = result(
        card(status="not_matched", logo_matches=False, layout_matches=False, footer_text="")
    )
    assert merge_ai_evidence(local(), [result(), other]).verdict == "violation_high"


def test_parts_from_different_attachments_cannot_be_combined():
    a = result(card(logo_matches=False))
    b = result(card(footer_text=""))
    assert merge_ai_evidence(local(), [a, b]).verdict != "allow"


def test_raw_ad_without_local_policy_receipt_still_is_not_window_source():
    assert _confirmed_sources({"ai_results": [result().model_dump()]}) == []


@pytest.mark.parametrize("status", ["matched", "uncertain"])
@pytest.mark.parametrize(
    "rule,category",
    [
        ("R_GROUP_CARD", "ad"),
        ("R_FORWARD_RECORD", "ad"),
        ("R005", "flood"),
        ("synthetic-severe", "porn"),
        ("synthetic-severe", "violence"),
    ],
)
def test_template_never_overrides_secondary_hard_or_severe_rule(status, rule, category):
    from app.moderation.decision import RuleHit

    original = local().model_copy(
        update={"rule_hits": [RuleHit(rule_id=rule, rule_name="synthetic", category=category)]}
    )
    decision = merge_ai_evidence(original, [result(card(status=status))])
    assert decision.verdict == "violation_high"
    assert "recall" in decision.recommended_actions


def test_matched_template_cannot_suppress_flood_category():
    decision = merge_ai_evidence(local("flood"), [result(category="flood")])
    assert decision.verdict == "violation_high"
    assert "recall" in decision.recommended_actions


def test_missing_review_policy_does_not_break_source_processing():
    from app.moderation.campus_policy import make_campus_source_policy

    original = local()
    results = [result()]
    final = merge_ai_evidence(original, results)
    assert final.verdict == "allow"
    assert make_campus_source_policy(original, final, results, {}) is None


def test_footer_and_global_code_flags_conflict_requires_manual_review():
    observed = result(has_miniprogram_code=False)
    assert observed.needs_review
    assert not confirmed_campus_source(observed.model_dump())


async def source_row(monkeypatch, tmp_path, *, category="ad"):
    from app.runtime import pipeline

    from tests.test_r132_source_message_order import reload_persisted
    from tests.test_r132_window_evidence import SyntheticModels, event, execute, ident

    group, user = ident(), ident()
    now = datetime.now(UTC).replace(microsecond=0)
    (tmp_path / "source.png").touch()
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    row = await execute(
        event(
            group, user, now, [{"type": "image", "data": {"file": "source.png"}}], ("source.png",)
        ),
        SyntheticModels([result(category=category)]),
    )
    row = await reload_persisted(row)
    return row, group, user, now


@pytest.mark.parametrize("category", ["ad", "fraud"])
@pytest.mark.parametrize("kind", ["text", "image"])
async def test_template_picture_and_followup_share_persisted_eligibility(
    monkeypatch, tmp_path, category, kind
):
    from app.moderation.ai import AIModerationResult

    from tests.test_r132_source_message_order import reload_persisted
    from tests.test_r132_window_evidence import SyntheticModels, event, execute

    row, group, user, now = await source_row(monkeypatch, tmp_path, category=category)
    assert row.verdict == "allow"
    detail = json.loads(row.detail_json)
    assert detail["ai_results"][0]["category"] == category
    assert detail.get("campus_source_policy")
    assert _confirmed_sources(detail)
    (tmp_path / "next.png").touch()
    segments = (
        [{"type": kind, "data": {"text": "合成后续推广"}}]
        if kind == "text"
        else [{"type": "image", "data": {"file": "next.png"}}]
    )
    current = await execute(
        event(
            group,
            user,
            now + timedelta(seconds=30),
            segments,
            () if kind == "text" else ("next.png",),
        ),
        SyntheticModels(
            [
                AIModerationResult(
                    source="text" if kind == "text" else "vision",
                    category=category,
                    confidence=0.99,
                    needs_review=False,
                    model_id="synthetic-current",
                )
            ]
        ),
    )
    current = await reload_persisted(current)
    assert current.verdict == "record_only"
    assert json.loads(current.detail_json)["recommended_actions"] == []
    assert "豁免" in current.reason


@pytest.mark.parametrize(
    "tamper", ["receipt", "raw_category", "needs_review", "veto", "processing", "policy"]
)
async def test_source_receipt_is_bound_to_full_evidence(monkeypatch, tmp_path, tamper):
    row, *_ = await source_row(monkeypatch, tmp_path)
    detail = json.loads(row.detail_json)
    assert _confirmed_sources(detail)
    changed = copy.deepcopy(detail)
    if tamper == "receipt":
        changed.pop("campus_source_policy")
    elif tamper == "raw_category":
        changed["ai_results"][0]["category"] = "porn"
    elif tamper == "needs_review":
        changed["ai_results"][0]["needs_review"] = True
    elif tamper == "veto":
        changed["evidence_vetoes"] = ["missing attachment"]
    elif tamper == "processing":
        changed["processing"] = True
    else:
        changed["review_policy"]["primary_direct_threshold"] = 0.01
    assert _confirmed_sources(changed) == []


async def test_window_exempt_picture_does_not_extend_window(monkeypatch, tmp_path):
    from app.moderation.ai import AIModerationResult

    from tests.test_r132_source_message_order import reload_persisted
    from tests.test_r132_window_evidence import SyntheticModels, event, execute

    row, group, user, now = await source_row(monkeypatch, tmp_path)
    assert row.verdict == "allow"
    (tmp_path / "ordinary.png").touch()
    middle = await execute(
        event(
            group,
            user,
            now + timedelta(seconds=100),
            [{"type": "image", "data": {"file": "ordinary.png"}}],
            ("ordinary.png",),
        ),
        SyntheticModels(
            [
                AIModerationResult(
                    source="vision",
                    category="ad",
                    confidence=0.99,
                    needs_review=False,
                    model_id="synthetic-ordinary",
                )
            ]
        ),
    )
    middle = await reload_persisted(middle)
    assert middle.verdict == "record_only"
    assert not _confirmed_sources(json.loads(middle.detail_json))
    last = await execute(
        event(
            group,
            user,
            now + timedelta(seconds=130),
            [{"type": "text", "data": {"text": "合成后续推广"}}],
        ),
        SyntheticModels(
            [
                AIModerationResult(
                    source="text",
                    category="ad",
                    confidence=0.99,
                    needs_review=False,
                    model_id="synthetic-last",
                )
            ]
        ),
    )
    last = await reload_persisted(last)
    assert last.verdict == "violation_high"
    assert "recall" in json.loads(last.detail_json)["recommended_actions"]
