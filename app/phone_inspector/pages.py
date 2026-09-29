"""Versioned parsing of visible QQ pages, without inferring account validity."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

PACKAGE = "com.tencent.mobileqq"
WARNING_TEXT = "该账号状态异常，涉嫌被多人举报或存在违规行为，暂不支持查看资料卡。"


class InspectionError(RuntimeError):
    """A readable reason to pause instead of guessing."""


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, other: "Rect") -> bool:
        return (
            self.left <= other.left < other.right <= self.right
            and self.top <= other.top < other.bottom <= self.bottom
        )


def rect(value: str) -> Rect:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value)
    if not match:
        raise InspectionError("界面坐标无法识别，请保持竖屏并重新读取。")
    result = Rect(*(int(item) for item in match.groups()))
    if result.right <= result.left or result.bottom <= result.top:
        raise InspectionError("界面控件不可见，已暂停。")
    return result


@dataclass(frozen=True)
class MemberRow:
    name: str
    bounds: Rect
    target: Rect


@dataclass(frozen=True)
class Page:
    kind: str = "unknown"
    qq: str = ""
    group_id: str = ""
    group_name: str = ""
    member_count: int | None = None
    rows: tuple[MemberRow, ...] = ()
    viewport: Rect | None = None
    back: Rect | None = None
    confirm: Rect | None = None
    members_button: Rect | None = None
    at_top: bool = False
    profile_name: str = ""
    raw: bytes = field(default=b"", repr=False)

    @property
    def fingerprint(self) -> str:
        # This checks navigation layout only. It is NEVER a member identity.
        if self.kind == "profile_cover":
            return hashlib.sha256(
                repr((self.profile_name, self.viewport, self.back)).encode()
            ).hexdigest()
        values = [(row.name, row.bounds.__dict__, row.target.__dict__) for row in self.rows]
        return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def _bounds(node: ET.Element) -> Rect:
    return rect(node.get("bounds", ""))


def parse_page(raw: bytes) -> Page:
    if not raw or len(raw) > 2_000_000 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise InspectionError("页面数据无效或过大，已暂停。")
    try:
        tree = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise InspectionError("无法读取完整手机页面，已暂停。") from exc
    if tree.tag != "hierarchy" or tree.get("rotation", "0") != "0":
        raise InspectionError("请保持手机竖屏。")
    nodes = [n for n in tree.iter("node") if n.get("package") == PACKAGE]
    if not nodes or len(nodes) > 15_000:
        raise InspectionError("没有可读取的 QQ 页面。")

    def find(resource: str) -> list[ET.Element]:
        return [n for n in nodes if n.get("resource-id") == PACKAGE + ":id/" + resource]

    def single(resource: str) -> ET.Element | None:
        found = find(resource)
        if len(found) > 1:
            raise InspectionError("页面身份字段不唯一，已暂停。")
        return found[0] if found else None

    back_nodes = [
        n
        for n in nodes
        if n.get("content-desc") == "返回" and n.get("resource-id") == PACKAGE + ":id/u9d"
    ]
    if not back_nodes:
        back_nodes = find("ivTitleBtnLeft")
    back = _bounds(back_nodes[0]) if len(back_nodes) == 1 else None
    dialogs = find("dialogText")
    if dialogs:
        confirm = single("dialogRightBtn")
        if (
            len(dialogs) == 1
            and dialogs[0].get("text") == WARNING_TEXT
            and confirm is not None
            and confirm.get("text") == "确认"
        ):
            return Page(kind="warning", confirm=_bounds(confirm), raw=raw)
        return Page(raw=raw)
    if any(
        "dialog" in n.get("resource-id", "").lower()
        or n.get("resource-id") in {"android:id/parentPanel", "android:id/alertTitle"}
        for n in tree.iter("node")
    ):
        return Page(raw=raw)
    number = single("gmx")
    if number is not None:
        match = re.fullmatch(r"QQ[:：]\s*([1-9][0-9]{4,11})", number.get("text", ""))
        if not match:
            raise InspectionError("资料页没有明确的 QQ 号码，已暂停。")
        return Page(kind="profile", qq=match[1], back=back, raw=raw)
    # QQ 9.3.60 custom-cover cards put the native QQ row in ag/icon/info.
    # Require that structure AND the displayed nickname prefix; never interpret
    # arbitrary parentheses in a nickname, signature or other info row as QQ.
    nickname, identity_section = single("tu_"), single("ag")
    if nickname is not None and identity_section is not None:
        candidates = []
        for row in identity_section:
            children = list(row)
            if (
                len(children) == 2
                and children[0].get("resource-id") == PACKAGE + ":id/icon"
                and children[1].get("resource-id") == PACKAGE + ":id/info"
            ):
                match = re.fullmatch(
                    re.escape(nickname.get("text", "")) + r"\(([1-9][0-9]{4,11})\)",
                    children[1].get("text", ""),
                )
                if match and nickname.get("text"):
                    candidates.append(match[1])
        if len(candidates) > 1:
            raise InspectionError("资料页账号字段不唯一，已暂停。")
        if candidates:
            return Page(kind="profile", qq=candidates[0], back=back, raw=raw)
    cover, toolbar = single("dk_"), single("g03")
    if (
        nickname is not None
        and nickname.get("text")
        and identity_section is None
        and cover is not None
        and toolbar is not None
        and back
    ):
        body, header = _bounds(cover), _bounds(toolbar)
        if body.contains(header) and body.bottom - header.bottom > 600:
            return Page(
                kind="profile_cover",
                profile_name=nickname.get("text", ""),
                viewport=Rect(body.left, header.bottom, body.right, body.bottom),
                back=back,
                raw=raw,
            )
    group_id, group_name = single("rj8"), single("rie")
    if group_id is not None and group_name is not None:
        gid, name = group_id.get("text", ""), group_name.get("text", "").strip()
        if not re.fullmatch(r"[1-9][0-9]{4,11}", gid) or not name or len(name) > 512:
            raise InspectionError("群身份字段无效，已暂停。")
        labels = [n for n in find("zcf") if n.get("text") == "群成员"]
        counts = [re.fullmatch(r"([0-9]+)人", n.get("text", "")) for n in find("zco")]
        values = [int(m[1]) for m in counts if m is not None]
        return Page(
            kind="group",
            group_id=gid,
            group_name=name,
            member_count=values[0] if len(values) == 1 else None,
            members_button=_bounds(labels[0]) if len(labels) == 1 else None,
            back=back,
            raw=raw,
        )
    title, viewport_node = single("ivTitleName"), single("k05")
    if title is not None and title.get("text") == "群聊成员" and viewport_node is not None:
        sort = single("ds3")
        if sort is not None and sort.get("text") != "默认排序":
            raise InspectionError("请在手机成员列表选择默认排序后继续。")
        viewport = _bounds(viewport_node)
        rows = []
        section = ""
        for row in nodes:
            if row.get("resource-id") == PACKAGE + ":id/k8u":
                section = row.get("text", "")
            if row.get("resource-id") != PACKAGE + ":id/jzt":
                continue
            bounds = _bounds(row)
            if not viewport.contains(bounds) or bounds.height < 70:
                continue  # A clipped row can have no name node until the next viewport.
            if section == "机器人" and any(
                n.get("resource-id") == PACKAGE + ":id/kab" for n in row.iter("node")
            ):
                continue  # Native robot section + robot badge, never the member's nickname.
            labels = [
                n for n in row.iter("node") if n.get("resource-id") == PACKAGE + ":id/tv_name"
            ]
            if len(labels) != 1 or row.get("enabled") != "true":
                raise InspectionError("成员行结构变化，已暂停。")
            try:
                target = _bounds(labels[0])
            except InspectionError:
                continue  # Clipped rows are inspected on the overlapping next viewport.
            if target.left >= viewport.right * 0.68:
                raise InspectionError("成员名称位置超出安全点击范围，已暂停。")
            target = Rect(
                target.left,
                target.top,
                min(target.right, int(viewport.right * 0.68)),
                target.bottom,
            )
            if (
                viewport.contains(bounds)
                and bounds.contains(target)
                and bounds.height >= 70
                and target.height >= 25
            ):
                rows.append(MemberRow(labels[0].get("text", ""), bounds, target))
        at_top = bool(find("k06")) and any(
            n.get("text", "").startswith("群主/管理员") for n in find("k8u")
        )
        return Page(
            kind="members", rows=tuple(rows), viewport=viewport, back=back, at_top=at_top, raw=raw
        )
    return Page(raw=raw)
