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


def custom_profile(nickname="测试(12345602)", identity="测试(12345602)(12345601)"):
    root = ET.fromstring(xml(("tu_", nickname, "[300,1700][700,1800]")))
    section = ET.SubElement(
        root,
        "node",
        {"package": "com.tencent.mobileqq", "resource-id": "com.tencent.mobileqq:id/ag"},
    )
    row = ET.SubElement(section, "node", {"class": "android.widget.LinearLayout"})
    ET.SubElement(
        row,
        "node",
        {"package": "com.tencent.mobileqq", "resource-id": "com.tencent.mobileqq:id/icon"},
    )
    ET.SubElement(
        row,
        "node",
        {
            "package": "com.tencent.mobileqq",
            "resource-id": "com.tencent.mobileqq:id/info",
            "text": identity,
        },
    )
    return root


def test_custom_profile_reads_identity_row_not_digits_in_nickname():
    page = parse_page(ET.tostring(custom_profile()))
    assert page.kind == "profile"
    assert page.qq == "12345601"


def test_arbitrary_info_text_or_mismatched_nickname_is_not_identity():
    assert parse_page(xml(("info", "测试(12345601)", "[100,100][800,160]"))).kind == "unknown"
    page = parse_page(ET.tostring(custom_profile(identity="其他名字(12345601)")))
    assert page.kind == "unknown"


def test_custom_profile_duplicate_identity_rows_are_rejected():
    import copy

    root = custom_profile()
    root.append(copy.deepcopy(root[1]))
    with pytest.raises(InspectionError):
        parse_page(ET.tostring(root))


def test_known_full_cover_is_scrollable_but_supplies_no_account_identity():
    root = ET.fromstring(
        xml(
            ("tu_", "合成昵称", "[300,1200][700,1300]"),
            ("dk_", "", "[0,0][1080,2128]"),
            ("g03", "", "[0,0][1080,228]"),
            ("u9d", "", "[32,130][108,206]"),
        )
    )
    root[-1].set("content-desc", "返回")
    page = parse_page(ET.tostring(root))
    assert page.kind == "profile_cover"
    assert page.qq == ""
    assert page.viewport.top >= 228
    assert page.viewport.bottom <= 2128


def test_clipped_member_without_visible_name_does_not_block_next_viewport():
    root = ET.fromstring(
        xml(
            ("ivTitleName", "群聊成员", "[400,100][600,160]"),
            ("k05", "", "[0,200][1080,2200]"),
            ("jzt", "", "[0,2180][1080,2200]"),
        )
    )
    assert parse_page(ET.tostring(root)).kind == "members"


@pytest.mark.parametrize("header,expected", [("机器人", 0), ("Q(1人)", 1)])
def test_native_robot_section_is_excluded_without_using_member_nickname(header, expected):
    root = ET.fromstring(
        xml(
            ("ivTitleName", "群聊成员", "[400,100][600,160]"),
            ("k05", "", "[0,200][1080,2200]"),
            ("k8u", header, "[0,300][1080,390]"),
            ("jzt", "", "[0,400][1080,550]"),
        )
    )
    row = root[-1]
    for resource in ("tv_name", "kab"):
        child = ET.fromstring(xml((resource, "Q群管家", "[200,430][400,500]")))[0]
        row.append(child)
    assert len(parse_page(ET.tostring(root)).rows) == expected
