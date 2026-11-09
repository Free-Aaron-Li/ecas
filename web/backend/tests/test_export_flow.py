"""`/api/export/flow.csv` 与 `/api/export/flow.xlsx` 的测试（需要本地 `ecas_dev` 在跑）。

先生 2026-11-04：「流量计同样加上…导出选项」——所以这里验能下、是 xlsx/csv、
列是工程量而不是寄存器/站号，行数与库内一致，超上限报 400。
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import flow
from app.settings import get_settings

TOKEN = "flow-export-token"


def _db_up() -> bool:
    try:
        import psycopg

        with psycopg.connect(get_settings().dsn, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_up(), reason="本地 ecas_dev 未运行")


@pytest.fixture(scope="module")
def client() -> TestClient:
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def with_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(client.app.state.settings, "auth_token", TOKEN)
    return TOKEN


def fixed_window(hours: int = 6) -> dict[str, str]:
    end = datetime.now().astimezone()
    start = end - timedelta(hours=hours)
    return {
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
    }


def _xlsx(payload: bytes):
    wb = load_workbook(io.BytesIO(payload))
    assert wb.sheetnames == ["流量数据"]
    return wb["流量数据"]


def _header_row(ws) -> int:
    for row in ws.iter_rows():
        values = [c.value for c in row[:6]]
        if values[:6] == list(flow_export_headers()):
            return row[0].row
    raise AssertionError("找不到数据表头")


def flow_export_headers() -> tuple[str, ...]:
    from app.export_flow import HEADERS

    return HEADERS


def _data_items(ws) -> list[tuple]:
    head = _header_row(ws)
    return [
        tuple(c.value for c in row[:6])
        for row in ws.iter_rows(min_row=head + 1)
        if row[0].value not in (None, "")
    ]


def test_导出需要令牌(client: TestClient, with_token: str) -> None:
    assert client.get("/api/export/flow.xlsx?hours=24").status_code == 401
    assert client.get("/api/export/flow.csv?hours=24").status_code == 401
    assert client.get(f"/api/export/flow.xlsx?hours=24&token={with_token}").status_code == 200
    assert client.get(f"/api/export/flow.csv?hours=24&token={with_token}").status_code == 200


def test_文件名与类型(client: TestClient, with_token: str) -> None:
    for path, suffix, media in (
        ("/api/export/flow.xlsx", ".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("/api/export/flow.csv", ".csv", "text/csv; charset=utf-8"),
    ):
        resp = client.get(f"{path}?hours=24&token={with_token}")
        assert resp.headers["content-type"] == media
        disposition = resp.headers["content-disposition"]
        assert disposition.startswith("attachment; filename=")
        assert disposition.endswith(f'{suffix}"')
        assert disposition.encode("ascii")


def test_表里只有工程量没有采集细节(client: TestClient, with_token: str) -> None:
    ws = _xlsx(client.get("/api/export/flow.xlsx", params={**fixed_window(), "token": with_token}).content)
    everything = " ".join(str(c.value) for row in ws.iter_rows() for c in row if c.value is not None)
    # 数值里出现 "485" 这种子串是正常的（13978 之类），只看有没有采集链路的描述词
    for banned in ("寄存器", "regs", "站号", "DTU", "JSONL", "MQTT"):
        assert banned not in everything, f"流量导出里不该出现 {banned!r}"
    assert "流量监测" in everything
    assert "区间" in everything


def test_行数与数值与库内解码一致(client: TestClient, with_token: str) -> None:
    import psycopg

    from app.main import SQL_FLOW_EXPORT

    window = fixed_window()
    ws = _xlsx(client.get("/api/export/flow.xlsx", params={**window, "token": with_token}).content)
    items = _data_items(ws)

    settings = get_settings()
    with psycopg.connect(settings.dsn) as conn:
        rows = conn.execute(
            SQL_FLOW_EXPORT,
            {
                "model": flow.MODEL,
                "start": datetime.fromisoformat(window["start"]),
                "end": datetime.fromisoformat(window["end"]),
                "limit": 50000,
            },
        ).fetchall()

    tz = settings.display_tz
    expected = []
    for ts, _model, regs in rows:
        d = flow.decode(regs)
        expected.append(
            (
                ts.astimezone(tz).replace(tzinfo=None),
                round(float(d["flow_m3h"]), 2),
                round(float(d["velocity_mps"]), 3),
                int(d["total_m3"]),
                d["direction_label"],
                d["status"],
            )
        )

    assert len(items) == len(expected)
    assert items == expected, "导出的工程量必须与库内寄存器解码逐行一致"


def test_超上限报_400(client: TestClient, with_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client.app.state.settings, "export_rows_max", 1)
    resp = client.get(f"/api/export/flow.xlsx?hours=24&token={with_token}")
    assert resp.status_code == 400
    assert "超过导出上限" in resp.json()["detail"]
