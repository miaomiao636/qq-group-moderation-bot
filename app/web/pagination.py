"""Bounded, read-only pagination shared by admin lists."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from urllib.parse import urlencode

from starlette.requests import Request

PAGE_SIZES = (20, 50)


@dataclass(frozen=True)
class Page:
    total: int
    number: int
    size: int

    @classmethod
    def from_request(cls, total: int, number: int, size: int) -> Page:
        size = size if size in PAGE_SIZES else PAGE_SIZES[0]
        pages = max(1, (total + size - 1) // size)
        return cls(total, min(max(1, number), pages), size)

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.size - 1) // self.size)

    @property
    def offset(self) -> int:
        return (self.number - 1) * self.size


def page_controls(
    request: Request,
    page: Page,
    *,
    label: str,
    fragment: str,
    page_key: str = "page",
    size_key: str = "page_size",
) -> str:
    """Preserve unrelated query parameters; links/forms never submit mutation fields."""
    params = dict(request.query_params)
    params.pop("notice", None)
    params.pop(page_key, None)
    params.pop(size_key, None)
    path = request.url.path

    def link(number: int, title: str) -> str:
        query = urlencode({**params, page_key: number, size_key: page.size})
        href = escape(f"{path}?{query}#{fragment}", quote=True)
        return f'<a class=btn href="{href}">{escape(title)}</a>'

    links = [link(page.number - 1, "上一页")] if page.number > 1 else []
    numbers = sorted(
        {1, page.pages, *range(max(1, page.number - 2), min(page.pages, page.number + 2) + 1)}
    )
    previous = 0
    for number in numbers:
        if previous and number - previous > 1:
            links.append("<span aria-hidden=true>…</span>")
        links.append(
            f'<strong class="btn current-page" aria-current=page>{number}</strong>'
            if number == page.number
            else link(number, str(number))
        )
        previous = number
    if page.number < page.pages:
        links.append(link(page.number + 1, "下一页"))
    fields = "".join(
        f'<input type=hidden name="{escape(key, quote=True)}" value="{escape(value, quote=True)}">'
        for key, value in params.items()
    )
    options = "".join(
        f'<option value="{size}" {"selected" if size == page.size else ""}>{size} 条</option>'
        for size in PAGE_SIZES
    )
    return (
        f'<nav class=pagination aria-label="{escape(label, quote=True)}">'
        f"<p class=muted>共 {page.total} 条，第 {page.number}/{page.pages} 页</p>"
        f"<div class=page-links>{''.join(links)}</div>"
        f'<form method=get action="{escape(path, quote=True)}#{escape(fragment, quote=True)}">'
        f'{fields}<label>每页 <select name="{size_key}">{options}</select></label> '
        f'<label>跳至 <input type=number name="{page_key}" min=1 max="{page.pages}" '
        f'value="{page.number}" style="width:72px"> 页</label> '
        "<button class=btn>应用</button></form></nav>"
    )
