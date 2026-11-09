"""接口冒烟测试（需要本地 `ecas_dev` 在跑；库不在时自动跳过）。

跑法：`uv run pytest`（本地库由 `docs/本地开发环境.md` 里的步骤准备）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.settings import get_settings


def _db_up() -> bool:
    try:
        import psycopg

        with psycopg.connect(get_settings().dsn, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # 库没起 / 没建库都算跳过
        return False


pytestmark = pytest.mark.skipif(not _db_up(), reason="本地 ecas_dev 未运行")


@pytest.fixture(scope="module")
def client() -> TestClient:
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.text == "ok"


def test_根路径按前端产物是否存在分流(client: TestClient) -> None:
    """`web/frontend/dist` 存在 → 直接托管看板；不存在 → 返回 JSON 提示。"""
    from app.main import DIST_DIR

    resp = client.get("/")
    assert resp.status_code == 200
    if (DIST_DIR / "index.html").exists():
        assert resp.headers["content-type"].startswith("text/html")
        # 标题 2026-11-04 起叫「浓度监测」（先生要求），不再带 ECAS 字样
        assert "浓度监测" in resp.text
    else:
        assert resp.json()["service"] == "ecas-backend"


def test_params_是现役口径(client: TestClient) -> None:
    body = client.get("/api/params").json()
    assert body["offset_mode"] == "add"
    assert body["offset_conc"] == 0.48
    assert body["thresholds"] == {"break_low": 3600, "over_high": 20500}


def test_health_有行且能给出滞后(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["rows_total"] > 0
    assert body["last_ts"] is not None
    assert body["age_seconds"] is not None
    assert body["last"]["status"] in {"正常", "偏低", "断线", "超量程"}


def test_readings_带换算字段且倒序(client: TestClient) -> None:
    body = client.get("/api/readings", params={"hours": 720, "limit": 10}).json()
    assert body["count"] == len(body["items"]) <= 10
    assert body["items"], "应该至少有一行"
    for item in body["items"]:
        assert set(item) >= {"ts", "model", "raw", "ma", "conc_raw", "conc", "status"}
        assert item["ma"] == pytest.approx(round(item["raw"] / 1000, 3))
        assert item["conc"] == pytest.approx(round(item["conc_raw"] + 0.48, 3))
    ts = [i["ts"] for i in body["items"]]
    assert ts == sorted(ts, reverse=True)


def test_readings_参数越界被夹住(client: TestClient) -> None:
    body = client.get("/api/readings", params={"hours": 99999, "limit": 999999, "offset": -5}).json()
    assert body["hours"] == 720
    assert body["limit"] == 5000
    assert body["offset"] == 0


def test_summary_只统计有效区间(client: TestClient) -> None:
    body = client.get("/api/summary", params={"hours": 720}).json()
    assert body["n"] >= body["n_valid"]
    assert body["n"] == body["n_valid"] + body["broken"] + body["low"] + body["over"]
    if body["n_valid"]:
        assert body["conc_min"] <= body["conc_avg"] <= body["conc_max"]


def test_websocket_推一条(client: TestClient) -> None:
    with client.websocket_connect("/ws/readings") as ws:
        first = ws.receive_json()
        assert first["model"] and isinstance(first["raw"], int)
        assert first["conc"] == pytest.approx(round(first["conc_raw"] + 0.48, 3))
