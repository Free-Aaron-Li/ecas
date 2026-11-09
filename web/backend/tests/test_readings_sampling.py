"""`/api/readings` 的整窗抽样（`sample=auto`）。

起因（2026-10-09 先生报障）：「哪怕我选择日期超过 7 天，趋势图还是最多只能显示最近 4 天内容。」
根因不是图表画错，而是接口语义：默认路径是 `ORDER BY ts DESC LIMIT n` ⇒ 窗口比 limit 长时
**只返回最新的 n 条**，图表拿它当整窗数据，自然只画到最后 n 条覆盖的那几天。

这里盯死三件事：
1. `sample=none`（默认）语义**不变** —— 还是「最新 limit 条」，分页/CLI 不受影响；
2. `sample=auto` 在窗口行数 > limit 时返回**整窗均匀**的真实读数（首尾贴住窗口两端，
   且每一行都能在原始表里找到 —— 不插值、不平均，`raw` 照样能判断线/超量程）；
3. 行数没超过 limit 时不抽样（给完整数据），响应里 `rows_total` / `sampled` /
   `bucket_seconds` 自洽，并与 `/api/summary` 的行数对得上。

需要本地 `ecas_dev` 在跑（库不在时整套跳过）。测试**只读**，不往库里写数据。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.settings import get_settings

TOKEN = "sampling-token"
#: 抽样断言用的条数上限：远小于本地库的行数（377），保证一定触发抽样
SMALL = 30


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


def _all_rows(client: TestClient, token: str) -> list[dict]:
    """整库拿一遍（limit 足够大 ⇒ 不抽样），返回时间倒序的原始行。"""
    body = client.get(f"/api/readings?hours=720&limit=5000&token={token}").json()
    if not body["items"]:
        pytest.skip("本地 ecas_dev 没有数据，抽查样无意义")
    return body["items"]


def _data_window(client: TestClient, token: str) -> tuple[datetime, datetime]:
    """数据的实际覆盖范围（首、末），再加 1 分钟余量当作绝对窗口。"""
    stamps = [datetime.fromisoformat(r["ts"]) for r in _all_rows(client, token)]
    return min(stamps) - timedelta(minutes=1), max(stamps) + timedelta(minutes=1)


def _ts(body: dict) -> list[datetime]:
    return [datetime.fromisoformat(r["ts"]) for r in body["items"]]


def test_默认语义不变_只取最新_limit_条(client: TestClient, with_token: str) -> None:
    """`sample` 缺省时行为与改动前逐字一致（老客户端/CLI 不受影响）。"""
    start, end = _data_window(client, with_token)
    body = client.get(
        f"/api/readings?start={_iso(start)}&end={_iso(end)}&limit={SMALL}&token={with_token}"
    ).json()
    assert body["sampled"] is False and body["bucket_seconds"] is None
    assert body["count"] == SMALL and body["offset"] == 0
    assert body["rows_total"] > SMALL, "窗口内总行数应远多于 limit，否则本测试无意义"
    stamps = _ts(body)
    assert stamps == sorted(stamps, reverse=True), "默认路径仍按时间倒序"
    # 最老的一条离窗口起点还差得远 —— 这正是「选 30 天只画几天」的成因
    assert stamps[-1] - start > timedelta(hours=1)


def test_auto_抽样铺满整个窗口且都是真行(client: TestClient, with_token: str) -> None:
    start, end = _data_window(client, with_token)
    body = client.get(
        f"/api/readings?start={_iso(start)}&end={_iso(end)}&limit={SMALL}"
        f"&sample=auto&token={with_token}"
    ).json()
    assert body["sampled"] is True
    assert body["count"] <= SMALL < body["rows_total"]
    assert body["bucket_seconds"] and body["bucket_seconds"] > 0
    assert body["offset"] == 0, "抽样已把整窗压成 limit 条，分页无意义 ⇒ offset 归 0"

    stamps = _ts(body)
    assert stamps == sorted(stamps, reverse=True)
    bucket = timedelta(seconds=body["bucket_seconds"])
    # 首尾都必须贴住窗口两端（各留不到两个桶的余量）⇒ 图能铺满所选区间
    assert end - stamps[0] <= bucket * 2, "最新一条应落在最后一个桶里"
    assert stamps[-1] - start <= bucket * 2, "最老一条应落在第一个桶里"

    # 抽出来的每一行都必须是库里真实存在的行（不插值、不平均）
    real = {(r["ts"], r["raw"]) for r in _all_rows(client, with_token)}
    assert {(r["ts"], r["raw"]) for r in body["items"]} <= real


def test_行数不超过_limit_时不抽样(client: TestClient, with_token: str) -> None:
    """窗口本来就装得下 ⇒ 给完整数据，不能因为「开了 auto」就抽稀。"""
    start, end = _data_window(client, with_token)
    body = client.get(
        f"/api/readings?start={_iso(start)}&end={_iso(end)}&limit=5000"
        f"&sample=auto&token={with_token}"
    ).json()
    assert body["sampled"] is False and body["bucket_seconds"] is None
    assert body["count"] == body["rows_total"]
    assert len(body["items"]) == body["rows_total"]


def test_rows_total_与_summary_同源(client: TestClient, with_token: str) -> None:
    start, end = _data_window(client, with_token)
    page = client.get(
        f"/api/readings?start={_iso(start)}&end={_iso(end)}&limit={SMALL}&sample=auto&token={with_token}"
    ).json()
    summary = client.get(f"/api/summary?start={_iso(start)}&end={_iso(end)}&token={with_token}").json()
    assert page["rows_total"] == summary["n"], "抽样不改变「窗口里有多少行」这个事实"


def test_兜底入参不炸(client: TestClient, with_token: str) -> None:
    """`sample` 只认 `auto`；别的值一律按默认处理（老前端/手敲 URL 不能 500）。"""
    body = client.get(f"/api/readings?hours=720&limit=5&sample=whatever&token={with_token}").json()
    assert body["sampled"] is False and body["count"] <= 5
