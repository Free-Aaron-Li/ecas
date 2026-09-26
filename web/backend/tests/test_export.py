"""`/api/export.csv` 与报警阈值的测试（需要本地 `ecas_dev` 在跑；库不在时自动跳过）。

覆盖：
* 导出要令牌（401），带 `?token=` 能下；
* 文件头有 UTF-8 BOM（否则 Excel 打开中文状态列乱码）；
* `#` 注释头写明了**当时**的口径、补偿、报警阈值与行数（口径可追溯）；
* 列名与行数对得上 `/api/readings`，单元格值与换算函数一致；
* 报警上下限经 `conc_to_raw()` 反解后与正向换算互逆（±1 计数）。
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

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

    # 用 with 进 lifespan：这样 app.state.pool 才存在，且模块结束才关池子
    with TestClient(app) as c:
        yield c


@pytest.fixture
def with_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(client.app.state.settings, "auth_token", TOKEN)
    return TOKEN


def fetch_csv(client: TestClient, hours: int = 720, token: str = "", limit: int = 20000) -> str:
    query = f"hours={hours}&limit={limit}"
    if token:
        query += f"&token={token}"
    resp = client.get(f"/api/export.csv?{query}")
    assert resp.status_code == 200, resp.text
    return resp.text


def test_导出需要令牌(client: TestClient, with_token: str) -> None:
    resp = client.get("/api/export.csv?hours=720")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    # 带令牌能下：证明 401 是令牌造成，不是别的
    assert client.get(f"/api/export.csv?hours=720&token={with_token}").status_code == 200


def test_文件名与类型(client: TestClient, with_token: str) -> None:
    resp = client.get(f"/api/export.csv?hours=24&token={with_token}")
    assert resp.headers["content-type"].startswith("text/csv")
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment; filename=")
    assert disposition.endswith('.csv"')
    # 文件名里不能出现非 ASCII（HTTP 头按协议只能是 ASCII）
    disposition.encode("ascii")


def test_带_BOM_且注释头写明口径(client: TestClient, with_token: str) -> None:
    body = fetch_csv(client, token=with_token)
    assert body.startswith("\ufeff"), "缺 UTF-8 BOM，Excel 打开中文会乱码"

    params = convert.params(get_settings())
    lines = [ln for ln in body.lstrip("\ufeff").splitlines() if ln]
    comments = [ln for ln in lines if ln.startswith("#")]
    assert comments, "缺 # 注释头"
    joined = "\n".join(comments)
    assert "口径:" in joined and params["formula"]["conc"] in joined
    assert f"offset_conc={get_settings().offset_conc}" in joined
    assert f"下限 {params['alarms']['low']}" in joined
    assert f"raw < {params['alarms']['low_raw']}" in joined
    assert f"raw > {params['alarms']['high_raw']}" in joined
    assert "断线/超量程行的 conc 是数学外推值" in joined


def _data_rows(body: str) -> list[list[str]]:
    rows = list(csv.reader(io.StringIO(body.lstrip("\ufeff"))))
    return [r for r in rows if r and not r[0].startswith("#")]


def test_列名与行数与_readings_一致(client: TestClient, with_token: str) -> None:
    body = fetch_csv(client, token=with_token)
    rows = _data_rows(body)
    header, data = rows[0], rows[1:]
    assert header == ["model", "ts", "raw", "ma", "conc_raw", "conc", "status"]

    page = client.get(f"/api/readings?hours=720&limit=5000&token={with_token}").json()
    assert len(data) == page["count"], "导出行数应等于同区间的读数行数"

    # 每个单元格都能被换算函数复现（抽查前 5 行）
    settings = get_settings()
    dicts = [dict(zip(header, r)) for r in data[:5]]
    for cell in dicts:
        expect = convert.convert(int(cell["raw"]), settings)
        assert float(cell["ma"]) == pytest.approx(expect["ma"], abs=1e-6)
        assert float(cell["conc_raw"]) == pytest.approx(expect["conc_raw"], abs=1e-6)
        assert float(cell["conc"]) == pytest.approx(expect["conc"], abs=1e-6)
        assert cell["status"] == expect["status"]


def test_时间正序(client: TestClient, with_token: str) -> None:
    body = fetch_csv(client, token=with_token)
    stamps = [r[1] for r in _data_rows(body)[1:]]
    assert stamps == sorted(stamps), "CSV 应按时间正序，便于用 Excel 直接画图"


def test_limit_夹住(client: TestClient, with_token: str) -> None:
    resp = client.get(f"/api/export.csv?hours=720&limit=999999&token={with_token}")
    assert resp.status_code == 200
    assert resp.text.count("\n") >= 2, "越界的 limit 应夹住而不是报错"
    assert get_settings().export_rows_max == 20000


def test_报警上下限反解(client: TestClient, with_token: str) -> None:
    settings = get_settings()
    params = client.get(f"/api/params?token={with_token}").json()
    alarms = params["alarms"]
    assert alarms["low"] == settings.alarm_low and alarms["high"] == settings.alarm_high
    # 反解必须与正向换算互逆（±1 计数）
    for key in ("low", "high"):
        back = convert.raw_to_conc(round(convert.conc_to_raw(alarms[key], settings)), settings)
        assert back == pytest.approx(alarms[key], abs=0.01), f"{key} 反解不互逆"
    # 现役占位值 40/60 且补偿为加性 +0.3 ⇒ raw = 4000 + (conc - 0.3) * 160
    assert alarms["low_raw"] == 10352  # 4000 + (40 - 0.3) * 160
    assert alarms["high_raw"] == 13552  # 4000 + (60 - 0.3) * 160


def test_汇总带越限计数(client: TestClient, with_token: str) -> None:
    summary = client.get(f"/api/summary?hours=720&token={with_token}").json()
    assert "n_below_alarm" in summary and "n_above_alarm" in summary
    # 越限只能在有效区间里数
    assert summary["n_below_alarm"] + summary["n_above_alarm"] <= summary["n_valid"]
