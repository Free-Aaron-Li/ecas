"""报警引擎与 `/api/alarms*` 的测试（需要本地 `ecas_dev` 在跑；库不在时自动跳过）。

测试数据的做法：**另起一个型号**（`DPRO-66-TEST`）往里插行 —— 引擎按型号过滤
（`SQL_RECENT` / `SQL_LAST_TS` 都带 `model`），所以测试与现网快照互不干扰，
测完把测试型号的行、事件、阈值覆盖全删掉。

覆盖：
* 断线 / 超量程 / 浓度高于上限 / 浓度低于下限 四种规则的开与关；
* 断线行**不会**被误判成"浓度偏低"（外推值不算数）；
* `no_data` 的开启，以及"数据断流时冻结其它事件"；
* 幂等：同一批数据扫两遍，不会产生第二条事件、也不会重复 touch；
* `PUT/DELETE /api/alarms/config` 的自定义与校验（`low >= high` → 400，越界 → 422），
  自定义值会真的改变判定，并且落库（重启/换浏览器还在）；
* `GET /api/alarms` 的字段、"进行中"计数、`POST /api/alarms/scan`；
* 报警接口同样受令牌保护。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

#: 测试期间关掉后台扫描器：`get_settings()` 是 lru_cache，所以必须在**第一次读到配置之前**
#: 设好环境变量（本模块按字母序第一个被收集）。手动 `POST /api/alarms/scan` 照样能测。
os.environ.setdefault("ECAS_ALARM_SCAN", "0")

from app import alarms, convert  # noqa: E402
from app.settings import get_settings  # noqa: E402

TOKEN = "alarm-token"
TEST_MODEL = "DPRO-66-TEST"
#: 插进去的行用这个相对时间（秒），要稳稳落在 `stale_seconds` 之内、且在 6 小时回看窗口内
AGO = 60


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
        # 后台扫描器会周期性评估真型号，测试期间关掉，避免它干扰断言
        c.app.state.settings.alarm_scan = False
        yield c


@pytest.fixture
def env(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """把引擎指向测试型号；测前测后都清干净。"""
    settings = client.app.state.settings
    monkeypatch.setattr(settings, "model_name", TEST_MODEL)
    _cleanup(client)
    yield settings
    _cleanup(client)


def _cleanup(client: TestClient) -> None:
    pool = client.app.state.pool
    with pool.connection() as conn:
        conn.execute("DELETE FROM raw_reading WHERE model = %s", (TEST_MODEL,))
        conn.execute("DELETE FROM alarm_event WHERE model = %s", (TEST_MODEL,))
        conn.execute("DELETE FROM alarm_config WHERE model = %s", (TEST_MODEL,))


def _insert(client: TestClient, raws: list[int], start_ago: int = AGO, step: int = 10) -> None:
    """按时间正序插入若干行（最后一行的 ts = now - start_ago 秒，往前的间隔 step 秒）。"""
    pool = client.app.state.pool
    now = datetime.now(timezone.utc)
    with pool.connection() as conn:
        for i, raw in enumerate(raws):
            ts = now - timedelta(seconds=start_ago + step * (len(raws) - 1 - i))
            conn.execute(
                "INSERT INTO raw_reading (model, ts, raw) VALUES (%s, %s, %s)",
                (TEST_MODEL, ts, raw),
            )


def _open_kinds(client: TestClient) -> set[str]:
    pool = client.app.state.pool
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT kind FROM alarm_event WHERE model = %s AND ended_ts IS NULL", (TEST_MODEL,)
        ).fetchall()
    return {row["kind"] for row in rows}


def _one_row(
    client: TestClient,
    kind: str,
    expr: str = "started_ts",
    key: str | None = None,
    open_only: bool = False,
):
    """取该种类事件的一个字段（只有一个种类时用它比读整行更好写）。"""
    pool = client.app.state.pool
    key = key or expr
    sql = f"SELECT {expr} AS {key} FROM alarm_event WHERE model = %s AND kind = %s"
    if open_only:
        sql += " AND ended_ts IS NULL"
    with pool.connection() as conn:
        return conn.execute(sql, (TEST_MODEL, kind)).fetchone()[key]


# ── 规则本身 ───────────────────────────────────────────────────────────────
def test_连续段折叠() -> None:
    """`_runs()` 是规则判定的地基：连续 True 折成闭区间段。"""
    assert alarms._runs([False, False]) == []
    assert alarms._runs([True, True, True]) == [(0, 2)]
    assert alarms._runs([True, False, True, True]) == [(0, 0), (2, 3)]
    assert alarms._runs([False, True, True, False]) == [(1, 2)]


def test_高浓度开事件(client: TestClient, env) -> None:
    """raw 14000 → 浓度 62.8 > 上限 60 ⇒ conc_high 进行中。"""
    _insert(client, [14000])
    result = alarms.evaluate(client.app.state.pool, env)
    assert result["opened"] == [alarms.CONC_HIGH]
    assert alarms.CONC_HIGH in _open_kinds(client)


def test_低浓度开事件(client: TestClient, env) -> None:
    """raw 8000 → 浓度 25.3 < 下限 40 ⇒ conc_low 进行中。"""
    _insert(client, [8000])
    assert alarms.evaluate(client.app.state.pool, env)["opened"] == [alarms.CONC_LOW]


def test_断线开事件且不算浓度偏低(client: TestClient, env) -> None:
    """raw 3000（<3.6mA）是设备断线；它的浓度是外推值，不能同时报"浓度偏低"。"""
    _insert(client, [3000])
    result = alarms.evaluate(client.app.state.pool, env)
    assert alarms.BREAK in result["opened"]
    assert alarms.CONC_LOW not in result["opened"]
    assert alarms.CONC_LOW not in _open_kinds(client)


def test_偏低短于判据时长不报事件(client: TestClient, env) -> None:
    """3.76 mA（状态「偏低」）只持续 2 分钟 ⇒ 不够 5 分钟判据，不记事件。

    偏低行同样不是有效浓度（浓度是外推值），所以也不能被误报成「浓度低于下限」。
    """
    _insert(client, [3760] * 5, step=30)  # 4 个间隔 × 30s = 120 秒
    result = alarms.evaluate(client.app.state.pool, env)
    assert alarms.UNDER_RANGE not in result["opened"]
    assert alarms.UNDER_RANGE not in _open_kinds(client)
    assert alarms.CONC_LOW not in _open_kinds(client)


def test_偏低持续超过判据时长开事件(client: TestClient, env) -> None:
    """3.76 mA 连续 6 分钟（≥5 分钟）⇒ 单开 `under_range` 事件，级别「警告」。"""
    _insert(client, [3760] * 13, step=30)  # 12 个间隔 × 30s = 360 秒
    result = alarms.evaluate(client.app.state.pool, env)
    assert result["opened"] == [alarms.UNDER_RANGE]
    assert _one_row(client, alarms.UNDER_RANGE, "level", open_only=True) == "警告"
    assert _one_row(client, alarms.UNDER_RANGE, "n_rows", open_only=True) == 13
    assert "活零点" in _one_row(client, alarms.UNDER_RANGE, "note", open_only=True)


def test_偏低恢复后关闭事件(client: TestClient, env) -> None:
    """偏低段之后回到正常区间：事件按「恢复正常那条数据」的时间收尾。"""
    _insert(client, [3760] * 13, step=30)
    alarms.evaluate(client.app.state.pool, env)
    assert alarms.UNDER_RANGE in _open_kinds(client)

    _insert(client, [9000], start_ago=0)
    result = alarms.evaluate(client.app.state.pool, env)
    assert alarms.UNDER_RANGE in result["closed"]
    assert alarms.UNDER_RANGE not in _open_kinds(client)
    assert _one_row(client, alarms.UNDER_RANGE, "ended_ts") is not None


def test_超量程开事件(client: TestClient, env) -> None:
    _insert(client, [21000])
    assert alarms.evaluate(client.app.state.pool, env)["opened"] == [alarms.OVER]


def test_数据恢复后关闭事件(client: TestClient, env) -> None:
    """先进断线、再补一条正常行：事件被关掉，结束时间取"恢复正常那条"的时间。"""
    _insert(client, [3000])
    alarms.evaluate(client.app.state.pool, env)
    assert alarms.BREAK in _open_kinds(client)

    _insert(client, [12000], start_ago=0)
    result = alarms.evaluate(client.app.state.pool, env)
    assert alarms.BREAK in result["closed"]
    assert alarms.BREAK not in _open_kinds(client)
    assert _one_row(client, alarms.BREAK, "ended_ts") is not None
    assert _one_row(client, alarms.BREAK, "n_rows") == 1  # 期间只有一条数据满足断线


def test_幂等_重复扫描不产生重复事件(client: TestClient, env) -> None:
    _insert(client, [14000])
    first = alarms.evaluate(client.app.state.pool, env)
    second = alarms.evaluate(client.app.state.pool, env)
    assert first["opened"] == [alarms.CONC_HIGH]
    # 第二遍：数据没变 ⇒ 既不新开，也不 touch（peak / n_rows 都没变）
    assert second["opened"] == []
    assert second["touched"] == []
    assert _one_row(client, alarms.CONC_HIGH, "count(*)", key="n") == 1


def test_新增越限数据会更新进行中的事件(client: TestClient, env) -> None:
    _insert(client, [14000])
    alarms.evaluate(client.app.state.pool, env)
    _insert(client, [16000], start_ago=0)  # 浓度更高 → peak 上移、行数 +1
    result = alarms.evaluate(client.app.state.pool, env)
    assert result["touched"] == [alarms.CONC_HIGH]
    assert result["opened"] == []
    assert _one_row(client, alarms.CONC_HIGH, "peak_raw", open_only=True) == 16000
    assert _one_row(client, alarms.CONC_HIGH, "n_rows", open_only=True) == 2


def test_没有任何数据时报无数据(client: TestClient, env) -> None:
    result = alarms.evaluate(client.app.state.pool, env)
    assert result["stale"] is True
    assert result["opened"] == [alarms.NO_DATA]
    assert "没有任何数据" in _one_row(client, alarms.NO_DATA, "note", open_only=True)


def test_数据太旧也报无数据(client: TestClient, env) -> None:
    _insert(client, [12000], start_ago=3600)  # 一小时前的最后一条 ⇒ 超过 600 秒判据
    result = alarms.evaluate(client.app.state.pool, env)
    assert result["stale"] is True
    assert alarms.NO_DATA in result["opened"]


def test_断流期间重复扫描只更新不新开(client: TestClient, env) -> None:
    """回归：第二遍断流扫描时 no_data 已经存在，必须走"更新 note"分支而不是抛错。

    这个 bug 只在"后台每分钟扫一次、且库里没有任何数据"时才现形（第一次插入、第二次更新），
    单跑一遍 `evaluate()` 是碰不到的 —— 现场表现是 `last_scan={"error": "'note'"}`。
    """
    settings, pool = env, client.app.state.pool
    first = alarms.evaluate(pool, settings)
    second = alarms.evaluate(pool, settings)
    assert first["opened"] == [alarms.NO_DATA]
    assert second["opened"] == []          # 不能新开第二条
    assert second["stale"] is True
    assert _one_row(client, alarms.NO_DATA, "count(*)", key="n") == 1


def test_数据断流时冻结其它事件(client: TestClient, env, monkeypatch: pytest.MonkeyPatch) -> None:
    """数据停在断线上超过 stale 秒：break 事件**不能**被当成"已恢复"关掉。"""
    settings, pool = env, client.app.state.pool
    monkeypatch.setattr(settings, "stale_seconds", 3600)  # 先按"数据还新鲜"跑一次
    _insert(client, [3000])
    alarms.evaluate(pool, settings)
    assert alarms.BREAK in _open_kinds(client)

    monkeypatch.setattr(settings, "stale_seconds", 10)  # 再让同一批数据变"断流"
    result = alarms.evaluate(pool, settings)
    assert result["stale"] is True
    assert alarms.BREAK not in result["closed"]
    assert alarms.BREAK in _open_kinds(client)  # 冻结在原地，等数据回来说话
    assert alarms.NO_DATA in result["opened"]


# ── 接口 ───────────────────────────────────────────────────────────────────
def test_报警接口需要令牌(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client.app.state.settings, "auth_token", TOKEN)
    assert client.get("/api/alarms").status_code == 401
    assert client.get(f"/api/alarms?token={TOKEN}").status_code == 200


def test_自定义上下限落库并生效(client: TestClient, env) -> None:
    settings, pool = env, client.app.state.pool
    before = alarms.effective_alarms(pool, settings)  # 没配过 ⇒ 出厂默认
    assert before["custom"] is False
    assert before["low"] == settings.alarm_low

    resp = client.put("/api/alarms/config?low=45&high=55")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["custom"] is True
    assert body["low"] == 45 and body["high"] == 55
    # 反解出的 raw 判据跟着变（45 → 4000 + (45-0.48)*160 = 11123）
    assert body["low_raw"] == 11123
    assert body["high_raw"] == 12723
    # 落库了 ⇒ 直接再读一次还是自定义值（服务重启也还在）
    assert alarms.effective_alarms(pool, settings)["low"] == 45

    # 自定义值真的改变判定：raw 11500 的浓度约 47.2，在 45~55 之内 ⇒ 不越限
    _insert(client, [11500])
    assert alarms.evaluate(pool, settings)["opened"] == []

    resp = client.delete("/api/alarms/config")
    assert resp.status_code == 200
    assert resp.json()["custom"] is False


def test_上下限校验(client: TestClient) -> None:
    assert client.put("/api/alarms/config?low=60&high=40").status_code == 400
    assert client.put("/api/alarms/config?low=40&high=40").status_code == 400
    assert client.put("/api/alarms/config?low=-1&high=50").status_code == 422  # 超出 0..100
    assert client.put("/api/alarms/config?low=101&high=102").status_code == 422


def test_事件接口字段与计数(client: TestClient, env) -> None:
    _insert(client, [14000])
    alarms.evaluate(client.app.state.pool, env)  # 直接调引擎，绕开令牌
    resp = client.get("/api/alarms?hours=1")
    assert resp.status_code == 200, resp.text
    page = resp.json()
    assert page["count"] == 1
    assert page["active"] == 1
    item = page["items"][0]
    assert item["kind"] == alarms.CONC_HIGH
    assert item["kind_label"] == "浓度高于上限"
    assert item["level"] == "警告"
    assert item["active"] is True
    assert item["ended_ts"] is None
    assert item["duration_seconds"] > 0
    assert item["peak_raw"] == 14000
    assert item["peak_conc"] == pytest.approx(convert.raw_to_conc(14000, env), abs=1e-6)


def test_断线事件的峰值不给浓度(client: TestClient, env) -> None:
    """断线行的浓度是外推值（可能为负）：事件里只给 raw，不给 conc。"""
    _insert(client, [3000])
    alarms.evaluate(client.app.state.pool, env)
    item = client.get("/api/alarms?hours=1").json()["items"][0]
    assert item["kind"] == alarms.BREAK
    assert item["peak_raw"] == 3000
    assert item["peak_conc"] is None


def test_手动扫描接口(client: TestClient, env) -> None:
    _insert(client, [21000])
    resp = client.post("/api/alarms/scan")
    assert resp.status_code == 200, resp.text
    assert resp.json()["opened"] == [alarms.OVER]
