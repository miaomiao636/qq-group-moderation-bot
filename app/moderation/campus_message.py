"""The additional window kind is an ordinary text/static-image combination only."""

from typing import Any

from app.core.contracts import StandardMessage

_STATIC_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/bmp"})


def plain_text_image_shape(segments: Any, media_kinds: Any) -> bool:
    """Validate persisted or normalized structure without retaining body text or URLs."""
    if not isinstance(segments, list) or not isinstance(media_kinds, list) or not media_kinds:
        return False
    if any(not isinstance(kind, str) or kind not in _STATIC_IMAGE_TYPES for kind in media_kinds):
        return False
    indices: set[int] = set()
    has_text = False
    for segment in segments:
        if not isinstance(segment, dict):
            return False
        kind, index = segment.get("kind"), segment.get("attachment_index")
        if kind == "text":
            if index is not None:
                return False
            has_text = True
        elif kind == "image":
            if type(index) is not int or not 0 <= index < len(media_kinds):
                return False
            indices.add(index)
        else:
            return False
    return has_text and indices == set(range(len(media_kinds)))


def is_plain_text_image_message(msg: StandardMessage) -> bool:
    return (
        msg.kind == "mixed"
        and msg.share_card is None
        and plain_text_image_shape(
            [{"kind": s.kind, "attachment_index": s.attachment_index} for s in msg.segments],
            [a.content_type for a in msg.attachments],
        )
    )


def is_window_image_message(msg: StandardMessage) -> bool:
    # Keep legacy image fixtures/records compatible; mixed requires full structure.
    return msg.kind == "image" or is_plain_text_image_message(msg)
