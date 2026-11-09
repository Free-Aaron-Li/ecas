"""`/api/export.xlsx` 的测试（需要本地 `ecas_dev` 在跑；库不在时自动跳过）。

这是先生 2026-11-04 要的「多一个选择」：同一区间、同一口径，但**不带仪表原始计数**。
所以这里既验「能下、是 xlsx」，也验「表里确实没有 raw」以及「数值与网页换算一致」。
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import convert
from app.settings import get_settings

TOKEN = "export-token"


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


@pytest.fixture
def with_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(client.app.state.settings, "auth_token", TOKEN)
    return TOKEN


def fixed_window(hours: int = 6) -> dict[str, str]:
    """绝对时间窗：导出与 `/api/readings` 用**同一对起止**，避免滑动窗口在两次请求间漂移。"""
    end = datetime.now().astimezone()
    start = end - timedelta(hours=hours)
    return {
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
    }


def _sheet(payload: bytes):
    wb = load_workbook(io.BytesIO(payload))
    assert wb.sheetnames == ["浓度数据"]
    return wb["浓度数据"]


def _header_row(ws) -> int:
    """表头行号：第一列是「时间」、第三列是「浓度(%)」的那一行。"""
    for row in ws.iter_rows():
        values = [c.value for c in row[:4]]
        if values[:4] == ["时间", "浓度(%)", "状态", "浓度越限"]:
            return row[0].row
    raise AssertionError("找不到数据表头")


def _data_items(ws) -> list[tuple]:
    head = _header_row(ws)
    return [
        tuple(c.value for c in row[:4])
        for row in ws.iter_rows(min_row=head + 1)
        if row[0].value not in (None, "")
    ]


def test_导出需要令牌(client: TestClient, with_token: str) -> None:
    resp = client.get("/api/export.xlsx?hours=720")
    assert resp.status_code == 401
    assert client.get(f"/api/export.xlsx?hours=720&token={with_token}").status_code == 200


def test_文件名与类型(client: TestClient, with_token: str) -> None:
    resp = client.get(f"/api/export.xlsx?hours=24&token={with_token}")
    assert resp.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment; filename=")
    assert disposition.endswith('.xlsx"')
    assert disposition.encode("ascii")  # HTTP 头只能是 ASCII


def test_表里没有原始数据也没有采集细节(client: TestClient, with_token: str) -> None:
    ws = _sheet(_export_fixed(client, with_token, fixed_window()))
    everything = " ".join(str(c.value) for row in ws.iter_rows() for c in row if c.value is not None)
    # 列名与内容都不该出现 raw/原始值/站号/链路（先生要求「不带原始数据」）
    for banned in ("原始值", "raw", "电流", "站号", "DTU", "JSONL", "MQTT", "寄存器"):
        assert banned not in everything, f"xlsx 里不该出现 {banned!r}"
    # 标题区仍要说清这份表是什么（型号 + 区间）
    assert "浓度监测" in everything
    assert "区间" in everything


def _export_fixed(client: TestClient, token: str, window: dict[str, str]) -> bytes:
    resp = client.get("/api/export.xlsx", params={**window, "token": token})
    assert resp.status_code == 200, resp.text
    return resp.content


def test_行数与数值与数据库换算一致(client: TestClient, with_token: str) -> None:
    """逐行核对：表格里的 (时间/浓度/状态) 必须等于**库里 raw 的换算结果**。

    不能拿 `/api/readings` 的 ts 当键去对：采集端补传时会出现**同一 ts 多条 raw**
    （PK 是 `(model, ts, raw)`），按 ts 建字典会串行。这里直接查库、用同一个
    `convert.convert()` 复算，再与表格逐行比对。
    """
    import psycopg

    from app.main import SQL_EXPORT

    window = fixed_window()
    items = _data_items(_sheet(_export_fixed(client, with_token, window)))
    settings = get_settings()
    with psycopg.connect(settings.dsn) as conn:
        rows = conn.execute(
            SQL_EXPORT,
            {
                "start": datetime.fromisoformat(window["start"]),
                "end": datetime.fromisoformat(window["end"]),
                "limit": 50000,
            },
        ).fetchall()

    tz = settings.display_tz
    expected = []
    for ts, _model, raw in rows:
        reading = convert.convert(int(raw), settings)
        expected.append(
            (
                ts.astimezone(tz).replace(tzinfo=None),
                round(float(reading["conc"]), 3),
                reading["status"],
            )
        )

    assert len(items) == len(expected), "导出行数应等于同区间的库内行数"
    assert [row[:3] for row in items] == expected, "表格数值/时间/状态必须与库内换算逐行一致"

    # 越限列：只有「正常」行才标，且必须与生效阈值一致
    low, high = settings.alarm_low, settings.alarm_high
    for ts, conc, status, exceed in items:
        if status != "正常":
            assert exceed in (None, "")
        elif conc > high:
            assert exceed == "高于上限"
        elif conc < low:
            assert exceed == "低于下限"
        else:
            assert exceed in (None, "")


def test_时间正序(client: TestClient, with_token: str) -> None:
    stamps = [row[0] for row in _data_items(_sheet(_export_fixed(client, with_token, fixed_window())))]
    assert stamps == sorted(stamps), "表格应按时间正序，便于直接用 Excel 画图"


def test_超上限报_400_而不是静默丢尾巴(
    client: TestClient, with_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client.app.state.settings, "export_rows_max", 3)
    resp = client.get(f"/api/export.xlsx?hours=720&token={with_token}")
    assert resp.status_code == 400
    assert "超过导出上限" in resp.json()["detail"]
