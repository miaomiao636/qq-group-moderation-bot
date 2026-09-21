"""Internal synthetic contracts; no reviewer probes or real member data."""

import xml.etree.ElementTree as ET

import pytest
from app.phone_inspector.pages import WARNING_TEXT, InspectionError, parse_page


def xml(*items: tuple[str, str, str], rotation: str = "0") -> bytes:
    root = ET.Element("hierarchy", rotation=rotation)
    for resource, text, bounds in items:
        ET.SubElement(
            root,
            "node",
            {
                "package": "com.tencent.mobileqq",
                "resource-id": "com.tencent.mobileqq:id/" + resource,
                "text": text,
                "bounds": bounds,
                "enabled": "true",
                "clickable": "true",
            },
        )
    return ET.tostring(root, encoding="utf-8")


def test_exact_native_warning_is_observable_but_does_not_supply_qq():
    page = parse_page(
        xml(
            ("dialogText", WARNING_TEXT, "[100,300][900,600]"),
            ("dialogRightBtn", "确认", "[100,600][900,700]"),
        )
    )
    assert page.kind == "warning"
    assert page.qq == ""


def test_profile_identity_accepts_native_thin_space():
    page = parse_page(xml(("gmx", "QQ:\u200912345601", "[300,400][700,450]")))
    assert page.kind == "profile"
    assert page.qq == "12345601"


def test_unknown_dialog_is_not_normal_profile():
    page = parse_page(
        xml(
            ("gmx", "QQ:12345601", "[300,400][700,450]"),
            ("dialogText", "网络异常，请重试", "[100,600][900,800]"),
        )
    )
    assert page.kind == "unknown"


def test_warning_like_nickname_is_not_warning():
    assert parse_page(xml(("tv_name", WARNING_TEXT, "[100,100][800,160]"))).kind == "unknown"


def test_duplicate_identity_is_rejected():
    with pytest.raises(InspectionError):
        parse_page(
            xml(
                ("gmx", "QQ:12345601", "[300,400][700,450]"),
                ("gmx", "QQ:12345602", "[300,500][700,550]"),
            )
        )


@pytest.mark.parametrize(
    "raw", [b"bad", b'<!DOCTYPE x [<!ENTITY a "x">]><hierarchy/>', xml(rotation="1")]
)
def test_invalid_or_rotated_xml_pauses(raw):
    with pytest.raises(InspectionError):
        parse_page(raw)


def test_long_member_name_still_has_safe_tap_target_left_of_add_button():
    root = ET.fromstring(xml(("ivTitleName", "群聊成员", "[400,100][600,160]")))
    viewport = ET.SubElement(
        root,
        "node",
        {
            "package": "com.tencent.mobileqq",
            "resource-id": "com.tencent.mobileqq:id/k05",
            "bounds": "[0,200][1080,2200]",
        },
    )
    row = ET.SubElement(
        viewport,
        "node",
        {
            "package": "com.tencent.mobileqq",
            "resource-id": "com.tencent.mobileqq:id/jzt",
            "enabled": "true",
            "bounds": "[0,400][1080,550]",
        },
    )
    ET.SubElement(
        row,
        "node",
        {
            "package": "com.tencent.mobileqq",
            "resource-id": "com.tencent.mobileqq:id/tv_name",
            "text": "非常长的合成成员名称",
            "bounds": "[319,430][950,500]",
        },
    )
    page = parse_page(ET.tostring(root))
    assert len(page.rows) == 1
    assert page.rows[0].target.right < 900


def test_nonstandard_dialog_does_not_use_underlying_qq_as_normal_profile():
    raw = xml(
        ("gmx", "QQ:12345601", "[300,400][700,450]"),
        ("dialog_message", "安全验证", "[100,600][900,800]"),
    )
    assert parse_page(raw).kind == "unknown"
