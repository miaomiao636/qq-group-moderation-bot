"""Model-based navigation contracts; these are NOT phone acceptance evidence."""

import json
import threading

from app.phone_inspector.device import DeviceIdentity
from app.phone_inspector.engine import Scanner
from app.phone_inspector.pages import MemberRow, Page, Rect
from app.phone_inspector.storage import Store

BACK, OPEN, CONFIRM = Rect(1, 1, 30, 30), Rect(10, 40, 90, 80), Rect(10, 80, 90, 100)


class FakePhone:
    def __init__(self, pages, warnings=()):
        self.pages = pages
        self.warnings = set(warnings)
        self.index = 0
        self.qq = ""
        self.state = "members"
        self.stop = threading.Event()
        self.identity = DeviceIdentity("fake", 999, "9.3.60")
        self.actions = []
        self.unknown = False

    def snapshot(self):
        if self.state == "group":
            return Page(
                kind="group",
                group_id="12345678",
                group_name="合成群",
                member_count=len({q for page in self.pages for q in page}),
                members_button=OPEN,
            )
        if self.state == "members":
            rows = tuple(
                MemberRow(
                    "同名成员",
                    Rect(0, 100 + i * 100, 900, 190 + i * 100),
                    Rect(100, 120 + i * 100, 300, 170 + i * 100),
                )
                for i in range(len(self.pages[self.index]))
            )
            return Page(
                kind="members",
                rows=rows,
                viewport=Rect(0, 80, 900, 900),
                back=BACK,
                at_top=self.index == 0,
            )
        if self.unknown:
            return Page()
        if self.state == "warning":
            return Page(kind="warning", confirm=CONFIRM, raw=b"synthetic warning")
        return Page(kind="profile", qq=self.qq, back=BACK, raw=b"synthetic profile")

    def tap(self, page, target):
        self.actions.append((page.kind, target))
        if target == BACK:
            self.state = "group" if self.state == "members" else "members"
        elif target == OPEN:
            self.state = "members"
        elif target == CONFIRM:
            self.state = "profile"
        else:
            row_index = [r.target for r in page.rows].index(target)
            self.qq = self.pages[self.index][row_index]
            self.state = "warning" if self.qq in self.warnings else "profile"

    def scroll(self, page, *, toward_bottom):
        self.index = (
            min(self.index + 1, len(self.pages) - 1) if toward_bottom else max(0, self.index - 1)
        )

    def delay(self, seconds=1):
        pass

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\nsynthetic"


def test_identical_row_names_do_not_mean_same_accounts_or_end_of_list(tmp_path):
    phone = FakePhone([["12345601", "12345602"], ["12345603", "12345604"]], {"12345603"})
    store = Store(tmp_path / "task", create=True)
    result = Scanner(phone, store).run()
    assert result["latest_pass"]["status"] == "END_REACHED"
    assert result["checked"] == 4
    assert result["warnings"] == 1
    assert store.observations(warnings_only=True)[0]["qq"] == "12345603"
    assert phone.state == "members"
    store.close()


def test_limit_then_resume_merges_results_and_rechecks_current_list(tmp_path):
    phone = FakePhone([["12345601", "12345602"]], {"12345601"})
    store = Store(tmp_path / "task", create=True)
    result = Scanner(phone, store).run(limit=1)
    assert result["latest_pass"]["status"] == "PAUSED"
    assert result["checked"] == 1
    store.close()
    store = Store(tmp_path / "task")
    result = Scanner(phone, store).run()
    assert result["checked"] == 2 and result["warnings"] == 1
    assert store.db.execute("SELECT count(*) FROM passes").fetchone()[0] == 2
    store.close()


def test_unknown_page_stops_without_guessing_qq_or_dismissing_unknown_dialog(tmp_path):
    phone = FakePhone([["12345601"]])
    phone.unknown = True
    store = Store(tmp_path / "task", create=True)
    result = Scanner(phone, store).run()
    assert result["latest_pass"]["status"] == "PAUSED"
    assert result["checked"] == 0
    assert result["unresolved"] == 1
    assert not any(kind == "unknown" for kind, _ in phone.actions)
    store.close()


def test_resume_records_each_execution_instead_of_reusing_first_sha(tmp_path):
    phone = FakePhone([["12345601"]])
    store = Store(tmp_path / "task", create=True)
    Scanner(phone, store, stamp={"execution_sha": "first"}).run(limit=1)
    Scanner(phone, store, stamp={"execution_sha": "second"}).run(limit=1)
    manifest = tmp_path / "task" / "passes" / "2.json"
    assert manifest.is_file()
    assert json.loads(manifest.read_text(encoding="utf-8"))["execution_sha"] == "second"
    store.close()


def test_full_cover_is_scrolled_before_reading_identity(tmp_path):
    class CoverPhone(FakePhone):
        revealed = False

        def snapshot(self):
            page = super().snapshot()
            if page.kind == "profile" and not self.revealed:
                return Page(kind="profile_cover", back=BACK, viewport=Rect(0, 100, 900, 900))
            return page

        def reveal_profile(self, page):
            assert page.kind == "profile_cover"
            self.revealed = True

    phone = CoverPhone([["12345601"]])
    store = Store(tmp_path / "task", create=True)
    result = Scanner(phone, store).run(limit=1)
    assert result["checked"] == 1
    assert result["unresolved"] == 0
    assert phone.state == "members"
    store.close()
