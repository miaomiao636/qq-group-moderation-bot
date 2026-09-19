"""群人数普查（**只读**）：经 NapCat WebUI 取群列表（含人数），与本项目授权状态对照。

用例：
    uv run python scripts/group_size_survey.py --threshold 200

说明：
- 数据来源：NapCat **WebUI**（`D:\\QQ\\config\\webui.json` 里的 host/port/token）；
  OneBot 侧只开了反向 WS，没有 HTTP 接口，所以不能直接打 OneBot API；
- **只读**：只调用"列出群"接口，不改任何配置、不发任何消息、不操作任何群；
- 输出：按人数降序的表格，并标出超过阈值的群、以及本项目当前是否已对其开启真实动作
  （`provider_group_settings.action_enabled`）；同时写出 `docs/evidence/stats/groups-*.md`。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import urllib.request
from datetime import UTC, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBUI = pathlib.Path("D:/QQ/config/webui.json")
OUT_DIR = ROOT / "docs" / "evidence" / "stats"


def _webui() -> tuple[str, str, int]:
    data = json.loads(WEBUI.read_text(encoding="utf-8"))
    return (
        str(data.get("token", "")),
        str(data.get("host", "127.0.0.1")),
        int(data.get("port", 6099)),
    )


def _call(path: str, auth: dict[str, str], host: str, port: int) -> object:
    req = urllib.request.Request(f"http://{host}:{port}{path}", headers=auth)
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - 本机回环
        return json.loads(resp.read().decode("utf-8", "replace"))


def _login_token() -> str:
    """NapCat WebUI 两段式认证：用 `webui.json` 的 token 换会话令牌。"""
    token, host, port = _webui()
    body = json.dumps({"token": token}).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/api/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - 本机回环
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        for key in ("token", "access_token", "Credential", "credential"):
            if data.get(key):
                return str(data[key])
    if isinstance(data, str) and data:
        return data
    raise SystemExit(f"WebUI 登录未取得令牌：{str(payload)[:160]}")


def _groups_from_onebot_http() -> list[dict[str, object]]:
    """优先走 NapCat 的 **OneBot HTTP 服务器**（配置在 `onebot11_*.json` 的 `network.httpServers`）。

    - 只读：仅调用 `get_group_list`（返回 `group_id / group_name / member_count / max_member_count`）；
    - 令牌在脚本内读取使用，**不打印**。
    """
    failures: list[str] = []
    for cfg in sorted(pathlib.Path("D:/QQ/config").glob("onebot11_*.json")):
        try:
            data = json.loads(cfg.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{cfg.name} 解析失败：{type(exc).__name__}")
            continue
        servers = (data.get("network") or {}).get("httpServers") or []
        if isinstance(servers, dict):
            servers = [servers]
        for server in servers:
            if not isinstance(server, dict) or not server.get("enable"):
                continue
            host = str(server.get("host") or "127.0.0.1").strip() or "127.0.0.1"
            port = int(server.get("port") or 3000)
            token = str(server.get("token") or "")
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                req = urllib.request.Request(
                    f"http://{host}:{port}/get_group_list", data=b"{}", headers=headers
                )
                with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - 本机回环
                    payload = json.loads(resp.read().decode("utf-8", "replace"))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{host}:{port} 调用失败：{type(exc).__name__}: {str(exc)[:80]}")
                continue
            items = payload.get("data") if isinstance(payload, dict) else payload
            if isinstance(items, list):
                return [g for g in items if isinstance(g, dict)]
            failures.append(f"{host}:{port} 返回非列表：{str(payload)[:100]}")
    raise SystemExit(
        "OneBot HTTP 不可用：" + ("；".join(failures) or "配置里没有启用的 httpServers")
    )


def _groups() -> list[dict[str, object]]:
    try:
        return _groups_from_onebot_http()
    except SystemExit as exc:
        print(f"[提示] OneBot HTTP 未取到群列表（{exc}），改用 WebUI 尝试…")
    token, host, port = _webui()
    notes: list[str] = []
    # 先试直接令牌，再试登录换取的会话令牌（NapCat 常见两种部署）
    candidates: list[dict[str, str]] = [{"Authorization": f"Bearer {token}"}]
    try:
        jwt = _login_token()
        candidates.append({"Authorization": f"Bearer {jwt}"})
    except Exception as exc:  # noqa: BLE001
        notes.append(f"登录失败：{type(exc).__name__}: {str(exc)[:100]}")
    for auth in candidates:
        for endpoint in ("/api/GroupList", "/api/group/list", "/api/Group/List"):
            try:
                payload = _call(endpoint, auth, host, port)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{endpoint} → {type(exc).__name__}: {str(exc)[:80]}")
                continue
            data = payload.get("data") if isinstance(payload, dict) else payload
            if isinstance(data, list):
                return [g for g in data if isinstance(g, dict)]
            notes.append(f"{endpoint} → 非列表：{str(payload)[:120]}")
    raise SystemExit("NapCat WebUI 调用失败：\n  " + "\n  ".join(notes))


def _count(group: dict[str, object]) -> int:
    for key in ("memberCount", "member_count", "memberNum", "member_num", "total"):
        value = group.get(key)
        if isinstance(value, int):
            return value
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="群人数普查（只读）")
    parser.add_argument("--threshold", type=int, default=200, help="标记阈值（默认 200 人）")
    parser.add_argument("--db", default=str(ROOT / "data" / "moderation.db"))
    args = parser.parse_args(argv)

    groups = _groups()
    enabled: set[str] = set()
    try:
        con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        try:
            enabled = {
                str(row[0])
                for row in con.execute(
                    "select external_group_id from provider_group_settings where action_enabled=1"
                )
            }
        finally:
            con.close()
    except sqlite3.Error:
        pass

    rows = []
    for g in groups:
        gid = str(g.get("groupCode") or g.get("group_id") or g.get("groupId") or "")
        name = str(g.get("groupName") or g.get("group_name") or "")
        rows.append((gid, name, _count(g)))
    rows.sort(key=lambda r: -r[2])
    over = [r for r in rows if r[2] > args.threshold]

    lines = [
        f"# 群人数普查（只读，阈值 > {args.threshold} 人）",
        "",
        f"- 生成时刻（UTC）：{datetime.now(UTC).isoformat(timespec='seconds')}",
        "- 数据来源：NapCat WebUI（本机回环，只读列群）",
        f"- 群总数：**{len(rows)}**；超过 {args.threshold} 人：**{len(over)}** 个"
        f"（其中已开启真实动作：**{sum(1 for r in over if r[0] in enabled)}** 个）",
        "",
        "| 群号 | 人数 | 群名 | 本项目已开真实动作 |",
        "| --- | --- | --- | --- |",
    ]
    for gid, name, cnt in rows:
        flag = "是" if gid in enabled else "否"
        lines.append(f"| {gid} | {cnt} | {name[:40]} | {flag} |")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"groups-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"GROUP_SURVEY_OK {out}")
    print(json.dumps({"groups": len(rows), "over": len(over), "threshold": args.threshold}))
    for gid, name, cnt in rows[:25]:
        print(
            f"  {gid} | {cnt} 人 | {name[:24]} | action_enabled={'是' if gid in enabled else '否'}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
