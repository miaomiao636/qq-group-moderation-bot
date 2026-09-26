"""R9-03 账号绑定回归（本仓库**补充**防线，非主审探针改写）。

主审 Windows 复核发现：外部探针用 ``str(self) == "D:/QQ/config"`` 作为注入条件，
而 Windows 上该字符串是 ``D:\\QQ\\config``（反斜杠）→ 注入失效 → 探针在 Windows 上**假通过**。
这里用**平台无关**的注入条件（``str(pathlib.Path(survey.ONEBOT_CONFIG_DIR))``）重做同一断言，
保证 Windows 本机也真正验证"普查必须绑定到配置的 self_id"这一契约。
"""

from __future__ import annotations

import io
import json
import pathlib

import pytest
from scripts import group_size_survey as survey


def _fake_configs(root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    first = root / "onebot11_10000001.json"
    second = root / "onebot11_90000009.json"
    for path, port in ((first, 3001), (second, 3009)):
        path.write_text(
            json.dumps(
                {"network": {"httpServers": [{"enable": True, "host": "127.0.0.1", "port": port}]}}
            ),
            encoding="utf-8",
        )
    return first, second


@pytest.fixture
def patched(tmp_path, monkeypatch):
    """把配置目录、HTTP 调用都替换掉：记录**实际请求过的**端口，不打开任何 socket。"""
    first, second = _fake_configs(tmp_path)
    target = str(pathlib.Path(survey.ONEBOT_CONFIG_DIR))
    original = pathlib.Path.glob
    requested: list[str] = []

    def safe_glob(self, pattern):
        if str(self) == target and pattern == "onebot11_*.json":
            return iter([first, second])
        return original(self, pattern)

    def fake_open(request, **_kwargs):
        requested.append(request.full_url)
        gid = "1001" if ":3001/" in request.full_url else "9009"
        payload = {"status": "ok", "retcode": 0, "data": [{"group_id": gid, "member_count": 300}]}
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(pathlib.Path, "glob", safe_glob)
    monkeypatch.setattr(survey.urllib.request, "urlopen", fake_open)
    return requested


def test_survey_binds_to_configured_self_id(monkeypatch, patched):
    """配了 self_id=90000009 → 只用该账号的配置（端口 3009），返回它的群列表。"""
    monkeypatch.setenv("ONEBOT_SELF_ID", "90000009")
    assert survey._groups_from_onebot_http() == [{"group_id": "9009", "member_count": 300}]
    assert patched and all(":3009/" in url for url in patched), patched


def test_survey_refuses_without_configured_self_id(monkeypatch, patched):
    """没有 self_id → 拒绝取数（fail-closed），绝不"谁先响应就用谁"。"""
    monkeypatch.setenv("ONEBOT_SELF_ID", "")
    monkeypatch.setattr(survey, "_configured", lambda key, default="": "")
    with pytest.raises(SystemExit):
        survey._groups_from_onebot_http()


def test_survey_refuses_when_account_config_missing(monkeypatch, patched):
    """self_id 没有对应配置文件 → 拒绝改用其它账号的配置。"""
    monkeypatch.setenv("ONEBOT_SELF_ID", "12345678")
    with pytest.raises(SystemExit):
        survey._groups_from_onebot_http()
