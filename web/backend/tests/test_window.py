"""时间窗（预设「最近 Nh」与自定义 `start`/`end`）的测试。

看板上「自定义时间点」走的就是这套参数，所以这里盯死三件事：
1. 预设只给 `hours` 时终点≈现在（窗口是滑动的，不能固定在某个时刻）；
2. 自定义 `start`/`end` 是**绝对**窗口 —— 返回的 start/end 与请求一致，取数也真按它过滤；
3. 明显的错参数要 400（起点晚于终点、跨度过大、格式不对），而不是悄悄给个别的窗口。

需要本地 `ecas_dev` 在跑（库不在时整套跳过）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.settings import get_settings

TOKEN = "window-token"


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


def _iso(dt: datetime) -> str:
    """按前端真实做法转义（`+00:00` 里的 `+` 不转义会被解成空格）。"""
    return quote(dt.astimezone(timezone.utc).isoformat(timespec="seconds"), safe="")


def test_预设区间是滑动窗口(client: TestClient, with_token: str) -> None:
    body = client.get(f"/api/summary?hours=24&token={with_token}").json()
    assert body["hours"] == 24
    start, end = datetime.fromisoformat(body["start"]), datetime.fromisoformat(body["end"])
    assert timedelta(hours=23, minutes=59) <= end - start <= timedelta(hours=24, minutes=1)
    # 终点≈现在（滑动窗口），给 5 分钟宽松
    assert abs((datetime.now(timezone.utc) - end).total_seconds()) < 300


def test_自定义区间是绝对窗口(client: TestClient, with_token: str) -> None:
    latest = client.get(f"/api/readings?hours=720&limit=1&token={with_token}").json()
    assert latest["items"], "本地库里应有数据，否则本测试无意义"
    ts = datetime.fromisoformat(latest["items"][0]["ts"])
    start, end = ts - timedelta(minutes=30), ts + timedelta(minutes=1)

    body = client.get(
        f"/api/readings?start={_iso(start)}&end={_iso(end)}&limit=5000&token={with_token}"
    ).json()
    got_start, got_end = datetime.fromisoformat(body["start"]), datetime.fromisoformat(body["end"])
    assert abs(got_start - start) < timedelta(seconds=1) and abs(got_end - end) < timedelta(seconds=1)
    assert body["hours"] == 1, "跨度取整后应给界面一个可显示的小时数"
    assert body["count"] >= 1
    for item in body["items"]:
        assert start <= datetime.fromisoformat(item["ts"]) < end, "取数必须落在自定义窗口内"

    # 同一段用 summary/export/alarms 也认（四个接口同源）
    summary = client.get(f"/api/summary?start={_iso(start)}&end={_iso(end)}&token={with_token}").json()
    assert summary["n"] == body["count"]
    export = client.get(f"/api/export.csv?start={_iso(start)}&end={_iso(end)}&token={with_token}")
    assert export.status_code == 200
    assert "区间:" in export.text and "小时" in export.text
    assert client.get(f"/api/alarms?start={_iso(start)}&end={_iso(end)}&token={with_token}").status_code == 200


def test_历史时段不会被拉到最新(client: TestClient, with_token: str) -> None:
    """挑一段早于库里最早数据的时间：应该 0 行，而不是「因为窗口漂了」给到最新数据。"""
    oldest = client.get(f"/api/readings?hours=720&limit=5000&token={with_token}").json()["items"]
    if not oldest:
        pytest.skip("库里没有数据")
    first_ts = min(datetime.fromisoformat(r["ts"]) for r in oldest)
    start, end = first_ts - timedelta(days=10), first_ts - timedelta(days=9)
    body = client.get(f"/api/readings?start={_iso(start)}&end={_iso(end)}&token={with_token}").json()
    assert body["count"] == 0


def test_导出文件名带自定义起止(client: TestClient, with_token: str) -> None:
    now = datetime.now(timezone.utc)
    resp = client.get(
        f"/api/export.csv?start={_iso(now - timedelta(hours=6))}&end={_iso(now)}&token={with_token}"
    )
    disposition = resp.headers["content-disposition"]
    disposition.encode("ascii")  # HTTP 头只能是 ASCII
    assert "~" in disposition and "h_" not in disposition, "自定义区间的文件名要写明起止，不是「Nh」"


def test_错参数报_400(client: TestClient, with_token: str) -> None:
    now = datetime.now(timezone.utc)
    # 起点晚于终点
    bad = client.get(f"/api/summary?start={_iso(now)}&end={_iso(now - timedelta(hours=1))}&token={with_token}")
    assert bad.status_code == 400 and "起点" in bad.json()["detail"]
    # 跨度超过 720 小时
    long = client.get(
        f"/api/summary?start={_iso(now - timedelta(days=40))}&end={_iso(now)}&token={with_token}"
    )
    assert long.status_code == 400 and "720" in long.json()["detail"]
    # 时间格式不对
    assert client.get(f"/api/summary?start=昨天&token={with_token}").status_code == 400


def test_未转义的加号也认(client: TestClient, with_token: str) -> None:
    """`curl` 手敲 `?start=2026-09-27T08:00+08:00` 时 `+` 会变成空格 —— 照样解析。"""
    body = client.get(
        f"/api/summary?start=2026-09-27T08:00+08:00&end=2026-09-27T09:00+08:00&token={with_token}"
    ).json()
    assert body["hours"] == 1


def test_不带时区的按本地时间解释(client: TestClient, with_token: str) -> None:
    """手工调接口时常见：`2026-09-27T08:00` 没有时区 ⇒ 按服务器本地时间，不报错。"""
    body = client.get(f"/api/summary?start=2026-09-27T08:00&end=2026-09-27T09:00&token={with_token}").json()
    assert body["hours"] == 1
    assert datetime.fromisoformat(body["start"]).utcoffset() is not None
