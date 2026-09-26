"""Persist and revalidate the same local campus policy used for the source image.

Raw model categories remain evidence. Only a completed local policy exemption
can make raw ad/fraud results eligible for a later message's window.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.moderation.ai import AIModerationResult, _miniprogram_qr_allow
from app.moderation.campus_source import confirmed_campus_qr, confirmed_campus_source
from app.moderation.decision import MINIPROGRAM_QR_ALLOW_RULE_ID, ModerationDecision

POLICY_VERSION = "campus-source-v1"
_POLICY_KEYS = ("primary_direct_threshold", "secondary_review_low", "secondary_review_high")


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _validated_policy(value: Any) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != set(_POLICY_KEYS):
        raise ValueError("missing actual review policy")
    if any(type(v) not in (int, float) or not 0 <= v <= 1 for v in value.values()):
        raise ValueError("invalid actual review policy")
    return {k: float(value[k]) for k in _POLICY_KEYS}


def make_campus_source_policy(
    local: ModerationDecision,
    final: ModerationDecision,
    results: list[AIModerationResult],
    review_policy: dict[str, float],
) -> dict[str, Any] | None:
    """Called locally after the image decision; never accepts an AI-issued permit."""
    if final.verdict != "allow" or not any(
        h.rule_id == MINIPROGRAM_QR_ALLOW_RULE_ID for h in final.rule_hits
    ):
        return None
    try:
        policy = _validated_policy(review_policy)
    except ValueError:
        return None  # Unknown policy prevents a receipt, not message processing.
    if _miniprogram_qr_allow(local, results, **policy) is None:
        return None
    # Store only decision evidence required by the shared policy, not identities.
    local_evidence = local.model_dump(
        include={"verdict", "category", "confidence", "rule_hits", "reason"}
    )
    return {
        "version": POLICY_VERSION,
        "local_evidence": local_evidence,
        "review_policy": policy,
        "results_sha256": _fingerprint([r.model_dump() for r in results]),
    }


def sources_from_campus_policy(detail: dict[str, Any]) -> list[tuple[str, str, bool]]:
    """Re-run the original local policy with full evidence and original thresholds."""
    try:
        context = detail["campus_source_policy"]
        if not isinstance(context, dict) or context.get("version") != POLICY_VERSION:
            return []
        if detail.get("processing") or detail.get("evidence_vetoes"):
            return []
        if not any(
            isinstance(h, dict) and h.get("rule_id") == MINIPROGRAM_QR_ALLOW_RULE_ID
            for h in detail.get("rule_hits", [])
        ):
            return []
        raw = detail["ai_results"]
        if not isinstance(raw, list) or context["results_sha256"] != _fingerprint(raw):
            return []
        results = [AIModerationResult.model_validate(r) for r in raw]
        policy = _validated_policy(context["review_policy"])
        if policy != _validated_policy(detail["review_policy"]):
            return []
        local = ModerationDecision(message_id="campus-source", **context["local_evidence"])
        if _miniprogram_qr_allow(local, results, **policy) is None:
            return []
        # A template allowed for content still cannot become a flood/severe source.
        if any(r.category in ("porn", "violence", "flood", "other") for r in results):
            return []
        return [
            (
                r.evidence.strip() or "认可校园墙分享模板",
                "校园墙白名单",
                confirmed_campus_qr(r.model_dump()),
            )
            for r in results
            if confirmed_campus_source(r.model_dump())
        ]
    except (KeyError, TypeError, ValueError):
        # Incomplete, old, or corrupt records cannot grant an exemption.
        return []
