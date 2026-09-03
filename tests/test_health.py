"""健康检查与基础脚手架测试。"""

from __future__ import annotations

from app.main import create_app
from fastapi.testclient import TestClient


def test_healthz() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["env"] == "test"
    assert body["mode"] == "SAFE"
