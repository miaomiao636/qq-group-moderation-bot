"""Explicit fixture migration for approved CAMPUS-SCOPE-20260922 policy.

Old positive source fixtures are translated to the newly confirmed campus
contract. Invalid prefixes and empty text stay invalid. New legacy/generic-code
negative tests construct raw evidence directly and never use this adapter.
"""


def campus_evidence(evidence: str) -> str:
    for prefix in ("校园墙白名单|文案:", "小程序码通过|文案:"):
        if evidence.startswith(prefix):
            text = evidence[len(prefix) :].strip()
            return "校园墙白名单|文案:万能校园墙 " + text if text else evidence
    if evidence == "合成普通图":
        return "校园墙白名单|文案:万能校园墙 合成普通帖子"
    return evidence
