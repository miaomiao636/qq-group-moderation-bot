"""从候选集分层抽样并生成网页标注工具（本地运行）。

- 去重：图片按媒体文件名集合，文本按正文前 40 字
- 抽样优先级：violation_high 全取 → 有媒体的图 → 其余按类别随机补齐
- 产物：data/w2_label.html（图片内联相对路径，浏览器直接打开即可标注）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    _reconfigure(encoding="utf-8")


def _load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _safe_script_json(payload: str) -> str:
    """把 JSON 安全嵌入 <script>：群文本不可信，</script> 会提前闭合标签（R02）。

    \\uXXXX 是 JSON 合法转义，前端 JSON.parse 还原后值不变。
    """
    return payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _blind_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """盲标独立性：只导出标注所需字段，剥离系统预测，页面不携带任何判定。"""
    return [
        {
            "sample_id": it["sample_id"],
            "kind": it["kind"],
            "text": it["text"],
            "media": it["media"],
            "label": "",
            "truth_category": "",
        }
        for it in items
    ]


def _dedup_key(row: dict[str, Any]) -> str:
    media = row.get("media") or []
    if media:
        return "m:" + hashlib.sha1("|".join(sorted(media)).encode()).hexdigest()
    text = (row.get("text") or "").strip()[:40]
    return "t:" + hashlib.sha1(text.encode("utf-8")).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labeling", default="data/w2_cand-labeling.jsonl")
    ap.add_argument("--system", default="data/w2_cand-system.jsonl")
    ap.add_argument("--out", default="data/w2_label.html")
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--seed", type=int, default=20260910)
    ap.add_argument("--exclude", default="", help="已标注文件，排除其中 sample_id")
    args = ap.parse_args()

    labeling = _load(Path(args.labeling))
    system = {r["sample_id"]: r for r in _load(Path(args.system))}
    done_ids = {r["sample_id"] for r in _load(Path(args.exclude))} if args.exclude else set()
    labeling = [r for r in labeling if r["sample_id"] not in done_ids]
    rng = random.Random(args.seed)

    seen: set[str] = set()
    prio1: list[dict[str, Any]] = []  # violation_high
    prio2: list[dict[str, Any]] = []  # 有媒体
    prio3: list[dict[str, Any]] = []  # 其余
    for row in labeling:
        sysrow = system.get(row["sample_id"])
        if not sysrow:
            continue
        key = _dedup_key(row)
        if key in seen:
            continue
        seen.add(key)
        item = {**row, "system": sysrow}
        if sysrow["verdict"] == "violation_high":
            prio1.append(item)
        elif row.get("media"):
            prio2.append(item)
        else:
            prio3.append(item)

    rng.shuffle(prio2)
    rng.shuffle(prio3)
    picked = list(prio1)
    room = max(0, args.limit - len(picked))
    picked += prio2[:room]
    room = max(0, args.limit - len(picked))
    picked += prio3[:room]
    rng.shuffle(picked)

    # R02：群文本是不可信输入。json.dumps 不转义 </script>，直接拼进 <script> 会被
    # 提前闭合并注入脚本。统一把 < > & 转成 \uXXXX（JSON 合法转义，前端还原后值不变），
    # 同时只导出盲标所需字段，页面不再携带系统预测（保证盲标独立性）。
    picked = _blind_items(picked)
    payload = _safe_script_json(json.dumps(picked, ensure_ascii=True))
    page = _render(payload)
    Path(args.out).write_text(page, encoding="utf-8")

    kinds: dict[str, int] = {}
    for it in picked:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    print(f"候选 {len(labeling)} → 去重后选取 {len(picked)} 条 → {args.out}")
    print(f"  含图: {sum(1 for x in picked if x.get('media'))}  类型分布: {kinds}")


def _render(payload: str) -> str:
    return (
        """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>W2 样本盲标</title><style>
body{font-family:system-ui,sans-serif;margin:0;background:#111;color:#eee}
header{position:sticky;top:0;background:#1c1c1c;padding:10px 16px;border-bottom:1px solid #333;z-index:9}
.card{background:#1c1c1c;margin:12px auto;max-width:760px;border-radius:8px;padding:14px}
img{max-width:100%;max-height:460px;display:block;margin:8px 0;background:#000}
.txt{white-space:pre-wrap;background:#000;padding:8px;border-radius:4px;font-size:14px}
.btns button{margin:4px 6px 0 0;padding:8px 14px;font-size:15px;border:0;border-radius:6px;cursor:pointer}
.v{background:#c0392b;color:#fff}.n{background:#27795a;color:#fff}.f{background:#8e6d13;color:#fff}
.done{outline:2px solid #2ecc71}.sid{color:#888;font-size:12px}
.badge{background:#333;border-radius:4px;padding:1px 6px;font-size:12px;margin-left:6px}
textarea{width:100%;height:180px;background:#000;color:#0f0;font-family:monospace}
#bar{font-size:14px;color:#9c9}
</style></head><body>
<header><b>W2 样本盲标</b> <span id="bar"></span>
<button onclick="dump()" style="float:right">生成/下载结果</button>
<p style="font-size:13px;color:#aaa;margin:6px 0 0">
判断该消息在群里是否应被视为违规广告/引流/诈骗：<b>违规</b>=应处罚，<b>正常</b>=放行，
<b>误报</b>=系统判违规但实际正常。快捷键 v/n/f。</p></header>
<div id="app"></div>
<div class="card"><h3>结果（复制给我，或点右上角下载）</h3><textarea id="out"></textarea></div>
<script>
const DATA = """
        + payload
        + """;
const KEY='w2labels';
let L=JSON.parse(localStorage.getItem(KEY)||'{}');
function save(){localStorage.setItem(KEY,JSON.stringify(L));localStorage.setItem(KEY+'_cat',JSON.stringify(C));render();}
function mark(id,v){L[id]=v;save();}
function setCat(id,v){C[id]=v;save();}
function render(){
 const app=document.getElementById('app');app.innerHTML='';
 let done=0;
 DATA.forEach((d,i)=>{
  const el=document.createElement('div');el.className='card'+(L[d.sample_id]?' done':'');
  let h='<div class="sid">#'+(i+1)+' / '+DATA.length+' · '+d.sample_id+' · '+d.kind+'</div>';
  if(d.text) h+='<div class="txt">'+ESC(d.text)+'</div>';
  (d.media||[]).forEach(m=>{h+='<img src="'+m.replace(/\\\\/g,'/').replace('data/','')+'" loading="lazy">';});
  h+='<div class="catrow">真值类别(人工独立判断): '
   +'<select onchange="setCat(\\''+d.sample_id+'\\',this.value)">'
   +['','ad','fraud','porn','violence','flood','other'].map(c=>'<option'+(C[d.sample_id]===c?' selected':'')+'>'+c+'</option>').join('')+'</select>'
   +'<span class="badge">'+(C[d.sample_id]||'未选')+'</span></div>';
  h+='<div class="btns">'
   +'<button class="v" onclick="mark(\\''+d.sample_id+'\\',\\'confirmed_violation\\')">违规 (v)</button>'
   +'<button class="n" onclick="mark(\\''+d.sample_id+'\\',\\'confirmed_normal\\')">正常 (n)</button>'
   +'<button class="f" onclick="mark(\\''+d.sample_id+'\\',\\'false_positive\\')">误报 (f)</button>'
   +'<span class="badge">'+(L[d.sample_id]||'未标')+'</span></div>';
  el.innerHTML=h;app.appendChild(el);
  if(L[d.sample_id])done++;
 });
 document.getElementById('bar').textContent='已标 '+done+' / '+DATA.length;
}
function ESC(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function dump(){
 const lines=DATA.filter(d=>L[d.sample_id]).map(d=>JSON.stringify({sample_id:d.sample_id,kind:d.kind,text:d.text,media:d.media,label:L[d.sample_id],truth_category:C[d.sample_id]||''}));
 document.getElementById('out').value=lines.join('\\n');
 const blob=new Blob([lines.join('\\n')+'\\n'],{type:'text/plain'});
 const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='w2_labels.jsonl';a.click();
}
document.addEventListener('keydown',e=>{if(['v','n','f'].includes(e.key)){const cur=[...document.querySelectorAll('.card')].find(c=>!c.classList.contains('done'));if(cur){const b=cur.querySelector(e.key==='v'?'.v':e.key==='n'?'.n':'.f');b&&b.click();}}});
render();
</script></body></html>"""
    )


if __name__ == "__main__":
    main()
