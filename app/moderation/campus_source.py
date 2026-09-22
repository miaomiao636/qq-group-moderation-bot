"""CAMPUS-SCOPE-20260922: visual source evidence, not decoded mini-app identity.

Unknown/legacy metadata never grants immunity. This is visual confirmation of
the approved brand, not authentication against WeChat or proof against forgery.
"""

from typing import Any

CAMPUS_WALL_SOURCE = "万能校园墙"
CAMPUS_EVIDENCE_PREFIX = "校园墙白名单|文案:"


def confirmed_campus_source(result: dict[str, Any]) -> bool:
    """Require explicit source and visible brand in the very same vision result."""
    evidence = result.get("evidence")
    return (
        result.get("source") == "vision"
        and result.get("campus_wall_source") == CAMPUS_WALL_SOURCE
        and isinstance(evidence, str)
        and evidence.strip().startswith(CAMPUS_EVIDENCE_PREFIX)
        and CAMPUS_WALL_SOURCE in evidence.strip()[len(CAMPUS_EVIDENCE_PREFIX) :]
    )
