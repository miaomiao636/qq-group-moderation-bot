"""把审核清单渲染成**一张可勾选的网页**（图片内嵌，一次性审核用）。

用法：
    uv run python scripts/image_review_sheet.py            # 取最新批次
    uv run python scripts/image_review_sheet.py --batch <目录>

产出：同目录 `REVIEW.html`；用本地静态服务器打开即可（图片按相对路径引用）：
    python -m http.server 8099 --directory <批次目录>
"""

from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "docs" / "evidence" / "image-review"

ROW = re.compile(
    r"^\| (\d\d) \| (.*?) \| `(.*?)` \| (.*?) \| (.*?) \| (.*?) \| (.*?) \| (.*?) \| (.*?) \| (.*?) \|"
)

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>图片审核表 · {batch}</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 15px/1.5 system-ui, "Segoe UI", sans-serif; margin: 0 auto; max-width: 1200px; padding: 16px; }}
 h1 {{ font-size: 20px; margin: 8px 0 4px; }}
 .meta {{ color: #777; font-size: 13px; margin-bottom: 12px; }}
 .stat {{ background: #f6f6f6; border-radius: 8px; padding: 10px 12px; margin-bottom: 14px; font-size: 14px; }}
 @media (prefers-color-scheme: dark) {{ .stat {{ background: #232323; }} }}
 form {{ display: grid; gap: 12px; }}
 .card {{ display: grid; grid-template-columns: 220px 1fr; gap: 14px; border: 1px solid #ddd;
          border-radius: 10px; padding: 10px; align-items: start; }}
 @media (prefers-color-scheme: dark) {{ .card {{ border-color: #3a3a3a; }} }}
 .card img {{ width: 100%; height: auto; border-radius: 8px; background: #fafafa; cursor: zoom-in; }}
 .no {{ font-weight: 700; font-size: 16px; }}
 .status {{ display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; border: 1px solid #999; }}
 .s0 {{ background: #ffe3e3; border-color: #e25555; color: #b10000; }}
 .s1 {{ background: #fff4d6; border-color: #d9a300; color: #7a5a00; }}
 .s2 {{ background: #e8f0ff; border-color: #6b93d6; color: #23456b; }}
 .s3 {{ background: #eee; }}
 .kv {{ color: #666; font-size: 13px; }} .kv b {{ color: inherit; }}
 .pick {{ margin-top: 8px; font-size: 14px; }}
 .pick label {{ margin-right: 14px; }}
 #out {{ width: 100%; height: 90px; font-family: ui-monospace, monospace; font-size: 13px; }}
 button {{ padding: 6px 12px; border-radius: 8px; border: 1px solid #888; background: #f2f2f2; cursor: pointer; }}
 #lightbox {{ position: fixed; inset: 0; background: rgba(0,0,0,.85); display: none; align-items: center; justify-content: center; }}
 #lightbox img {{ max-width: 96vw; max-height: 96vh; }}
</style></head><body>
<h1>图片审核表 · {batch}</h1>
<div class="meta">逐张选「放行 / 撤回」，然后点底部按钮生成结论文本，粘回给我即可。图片在原目录内，点图可放大。</div>
<div class="stat">{stat}</div>
<form id="f">{cards}</form>
<h2 style="font-size:16px;margin:18px 0 6px">结论（可复制）</h2>
<textarea id="out" readonly placeholder="选完上面的按钮后，点这里生成"></textarea>
<button type="button" onclick="collect()">生成结论文本</button>
<div id="lightbox" onclick="this.style.display='none'"><img id="lightimg"></div>
<script>
function zoom(src) {{ document.getElementById('lightimg').src = src;
  document.getElementById('lightbox').style.display = 'flex'; }}
function collect() {{
  const out = [];
  document.querySelectorAll('.card').forEach(c => {{
    const v = document.querySelector(`input[name="${{c.dataset.no}}"]:checked`);
    out.push(c.dataset.no + ' ' + (v ? v.value : '未定'));
  }});
  document.getElementById('out').value = out.join('；');
}}
</script></body></html>
"""


def _card(no: str, status: str, image: str, source: str, fields: dict[str, str]) -> str:
    cls = {"**会改变判定**": "s0", "无法判定": "s1", "候选（未在生效名单）": "s2"}.get(status, "s3")
    return f"""<div class="card" data-no="{no}">
  <div><img src="{html.escape(image)}" alt="{no}" onclick="zoom(this.src)"></div>
  <div>
    <div><span class="no">#{no}</span> <span class="status {cls}">{html.escape(status)}</span></div>
    <div class="kv">来源：<b>{html.escape(source)}</b></div>
    <div class="kv">距离 {fields["dist"]} ｜ 命中 {fields["count"]} 次 ｜ 其中会改变判定 <b>{fields["wc"]}</b></div>
    <div class="kv">原判定：{html.escape(fields["verdicts"])} ｜ 类别：{html.escape(fields["cats"])}</div>
    <div class="kv">样例：{html.escape(fields["samples"])[:200]}</div>
    <div class="pick">
      <label><input type="radio" name="{no}" value="放行"> 放行</label>
      <label><input type="radio" name="{no}" value="撤回"> 撤回</label>
      <label><input type="radio" name="{no}" value="再看看"> 再看看</label>
    </div>
  </div>
</div>"""


def build(batch: Path, exclude: Path | None = None) -> Path:
    text = (batch / "IMAGE_REVIEW.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    skip: set[str] = set()
    if exclude is not None:
        # 只保留"没在已审批次里出现过"的图（按文件 SHA-256 判定），避免重复审核
        for line in (exclude / "IMAGE_REVIEW.md").read_text(encoding="utf-8").splitlines():
            m = ROW.match(line)
            if m:
                cells = [c.strip() for c in line.split("|")]
                if len(cells) > 5:
                    # 第 5 列 = 文件 SHA-256（第 4 列是"来源"）
                    skip.add(cells[5].strip("`"))
        lines = [
            line
            for line in lines
            if not (ROW.match(line) and [c.strip() for c in line.split("|")][5].strip("`") in skip)
        ]
    header = [ln for ln in lines if "待审核图片" in ln or "集合模式" in ln or "动图范围" in ln]
    cards: list[str] = []
    for line in lines:
        m = ROW.match(line)
        if not m:
            continue
        no, status, image, source, sha, dhash, seed, dist, count, rest = m.groups()
        rest_cells = [c.strip() for c in line.split("|")]
        wc = rest_cells[10] if len(rest_cells) > 10 else ""
        cards.append(
            _card(
                no,
                status.strip(),
                image,
                source,
                {
                    "dist": dist.strip(),
                    "count": count.strip(),
                    "wc": wc.replace("*", ""),
                    "verdicts": rest_cells[11] if len(rest_cells) > 11 else "",
                    "cats": rest_cells[12] if len(rest_cells) > 12 else "",
                    "samples": rest_cells[13] if len(rest_cells) > 13 else "",
                },
            )
        )
    out = batch / ("REVIEW_NEW.html" if exclude is not None else "REVIEW.html")
    out.write_text(
        PAGE.format(
            batch=batch.name,
            stat="<br>".join(html.escape(ln.lstrip("- ")) for ln in header) or "—",
            cards="\n".join(cards) or "<p>本批没有需要审核的图片。</p>",
        ),
        encoding="utf-8",
    )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成可勾选的图片审核网页")
    parser.add_argument("--batch", default=None, help="批次目录；缺省=最新批次")
    parser.add_argument(
        "--exclude-batch",
        default=None,
        help="已审批次目录：排除其中已出现过的图（按文件 SHA-256），只渲染新增",
    )
    args = parser.parse_args(argv)
    batch = (
        Path(args.batch)
        if args.batch
        else max(DEFAULT_ROOT.glob("batch-*"), key=lambda p: p.stat().st_mtime)
    )
    exclude = Path(args.exclude_batch) if args.exclude_batch else None
    out = build(batch, exclude)
    count = out.read_text(encoding="utf-8").count('class="card"')
    print(f"REVIEW_SHEET_OK {out} （{count} 张{'新增' if exclude else ''}）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
