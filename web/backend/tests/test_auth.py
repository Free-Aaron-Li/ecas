"""令牌校验测试（不依赖数据库：全部打在 `/api/params` 上，它不查库）。

做法：先建 `TestClient`（lifespan 会把 settings 放进 `app.state`），再用 `monkeypatch`
改 `app.state.settings.auth_token` —— 被测的就是「同一个对象上的开关」，
不需要重启进程、也不需要往环境里塞东西。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

TOKEN = "s3cret-token"


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    with TestClient(app) as c:
        yield c


def _settings(client: TestClient):
    return client.app.state.settings


def test_未配置令牌时_fail_closed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09-28 安全审查 M-03：没配令牌**不放行**，而是 503 + 一句指路的话。

    （`conftest.py` 给整个测试会话设了 `ECAS_ALLOW_NO_AUTH=1`，所以这里要显式关掉它。）
    """
    monkeypatch.setattr(_settings(client), "auth_token", "")
    monkeypatch.setattr(_settings(client), "allow_no_auth", False)
    assert client.get("/api/auth").json()["auth_required"] is False  # 值守门状态本身照常可读
    resp = client.get("/api/params")
    assert resp.status_code == 503
    assert "fail-closed" in resp.json()["detail"]
    assert "ECAS_ALLOW_NO_AUTH" in resp.json()["detail"]


def test_显式声明本机开发才放行(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings(client), "auth_token", "")
    monkeypatch.setattr(_settings(client), "allow_no_auth", True)
    assert client.get("/api/params").status_code == 200


def test_文档默认关闭(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """M-01：`/docs`·`/redoc`·`/openapi.json` 默认不给（接口清单是攻击者的地图）。"""
    from app.main import _docs_kwargs

    settings = _settings(client)
    assert settings.enable_docs is False
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    # 需要时（本机调试）能被显式打开
    monkeypatch.setattr(settings, "enable_docs", True)
    assert _docs_kwargs(settings) == {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}


def test_配置令牌后必须带对(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings(client), "auth_token", TOKEN)

    assert client.get("/api/auth").json()["auth_required"] is True
    assert client.get("/api/params").status_code == 401  # 什么都不带

    # 三种带法都认
    assert client.get("/api/params", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert client.get(f"/api/params?token={TOKEN}").status_code == 200
    assert client.get("/api/params", cookies={"ecas_token": TOKEN}).status_code == 200

    # 错的令牌 / 大小写不对的 Bearer 前缀都不行
    assert client.get("/api/params", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/params?token=wrong").status_code == 401
    assert client.get("/api/params", headers={"Authorization": TOKEN}).status_code == 401


def test_401_带_WWW_Authenticate_且探针始终开放(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_settings(client), "auth_token", TOKEN)
    resp = client.get("/api/readings")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json()["detail"] == "缺少或错误的访问令牌"
    # 探针与「值守门状态」不能被令牌挡住，否则外部健康检查没法用
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/auth").status_code == 200


def test_每类api都受保护(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings(client), "auth_token", TOKEN)
    for path in ("/api/params", "/api/health", "/api/readings", "/api/summary"):
        assert client.get(path).status_code == 401, path
        assert client.get(f"{path}?token={TOKEN}").status_code == 200, path
        # 正确令牌下 /api/health 已证明能过鉴权（是否查库成功是另一回事）


def test_非ASCII令牌只走查询串(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """`Authorization` 头与 Cookie 值按协议**只能是 ASCII**（httpx/浏览器在发出前就报错），
    所以非 ASCII 令牌只能走 `?token=`（URL 会 percent-encode）。
    这条同时守住「服务端不会因为非 ASCII 令牌而 500」——`compare_digest` 要按字节比。"""
    monkeypatch.setattr(_settings(client), "auth_token", "令牌-中文")
    assert client.get("/api/params?token=令牌-中文").status_code == 200
    assert client.get("/api/params?token=wrong").status_code == 401


def test_websocket_令牌不对时拒绝握手(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings(client), "auth_token", TOKEN)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/readings") as ws:  # 不带令牌
            ws.receive_text()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/readings?token=wrong") as ws:
            ws.receive_text()
