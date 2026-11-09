"""流量计（FMC400）解码与 `/api/flow/*` 的测试（需要本地 `ecas_dev` 在跑；库不在时自动跳过）。

测试数据的做法：用**很久以前**的固定时刻（2000-01-01）插 4 行 `flow_reading`，
查询全部走显式 `start`/`end`，与现网数据互不干扰；测完按同一条时间窗删干净。

覆盖：
* 现场帧解码（流速 3.319 m/s / 瞬时流量 211.16 m³/h / 百分比 65.98% / 累积 5181 m³）；
* 负流量（0xFFFF 高字）→ 反流；空管百分比 100 → 空管；报警位 0x0004 → 报警；
* 寄存器不足 14 个必须报错（宁可不显示，也不拼出假流量）；
* `/api/flow/params`、`/api/flow/latest`、`/api/flow/readings`（含 sample=auto）、
  `/api/flow/summary`（含累积量增量）的字段与口径。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import flow
from app.settings import get_settings

#: 测试行的固定时刻（与现网数据不重叠）
BASE = datetime(2000, 1, 1, 0, 0, tzinfo=timezone.utc)
WIN_START = BASE.isoformat()
WIN_END = (BASE + timedelta(days=1)).isoformat()


def _db_up() -> bool:
    try:
        import psycopg

        with psycopg.connect(get_settings().dsn, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
            conn.execute("SELECT 1 FROM flow_reading LIMIT 1")
        return True
    except Exception:  # 库没起 / 表没建都算跳过
        return False


pytestmark = pytest.mark.skipif(not _db_up(), reason="本地 ecas_dev 或 flow_reading 未就绪")


def _row(ts_offset_hours: int, regs: list[int]) -> tuple[datetime, list[int]]:
    return BASE + timedelta(hours=ts_offset_hours), regs


#: 4 行测试数据：正常 / 反流 / 空管 / 报警。累积量 5000 → 5030（区间增量应为 30）。
ROWS = [
    _row(0, [0, 3319, 0, 21116, 6598, 0, 0, 5000, 0, 0, 0, 0, 0, 1]),
    _row(6, [0, 3319, 65535, 64302, 6598, 0, 0, 5010, 0, 0, 0, 0, 0, 0]),
    _row(12, [0, 0, 0, 0, 0, 10000, 0, 5020, 0, 0, 0, 0, 0, 0]),
    _row(18, [0, 3319, 0, 10000, 3125, 0, 0, 5030, 0, 0, 0, 0, 4, 1]),
]


@pytest.fixture(scope="module")
def client() -> TestClient:
    from app.main import app

    with TestClient(app) as c:
        c.app.state.settings.alarm_scan = False
        _cleanup()
        _seed()
        yield c
        _cleanup()


def _seed() -> None:
    import psycopg

    with psycopg.connect(get_settings().dsn) as conn:
        for ts, regs in ROWS:
            conn.execute(
                "INSERT INTO flow_reading (model, ts, regs) VALUES (%s, %s, %s::integer[]) "
                "ON CONFLICT (model, ts) DO NOTHING",
                (flow.MODEL, ts, regs),
            )


def _cleanup() -> None:
    import psycopg

    with psycopg.connect(get_settings().dsn) as conn:
        conn.execute(
            "DELETE FROM flow_reading WHERE model = %s AND ts >= %s AND ts < %s",
            (flow.MODEL, BASE, BASE + timedelta(days=1)),
        )


# ---------------------------------------------------------------- 解码（纯函数，不需要库）


def test_现场帧解码() -> None:
    """现场实测：流速 3.319 m/s、瞬时流量 211.16 m³/h、百分比 65.98%、累积 5181 m³。"""
    regs = [0, 3319, 0, 21116, 6598, 0, 0, 5181, 0, 0, 0, 0, 0, 1]
    d = flow.decode(regs)
    assert d["velocity_mps"] == 3.319
    assert d["flow_m3h"] == 211.16
    assert d["percent"] == 65.98
    assert d["empty_pipe_pct"] == 0.0
    assert d["total_m3"] == 5181
    assert d["alarm"] == 0
    assert d["alarm_labels"] == []
    assert d["status"] == flow.NORMAL


def test_负流量判反流() -> None:
    """0xFFFF 高字 = 负 32 位 ⇒ -12.34 m³/h，状态「反流」。"""
    d = flow.decode([0, 3319, 65535, 64302, 6598, 0, 0, 5000, 0, 0, 0, 0, 0, 0])
    assert d["flow_m3h"] == -12.34
    assert d["status"] == flow.REVERSE


def test_空管优先于报警() -> None:
    """空管百分比 100 时流量读数没有物理意义 ⇒ 先报「空管」，哪怕报警位也置了。"""
    d = flow.decode([0, 0, 0, 0, 0, 10000, 0, 5000, 0, 0, 0, 0, 4, 0])
    assert d["empty_pipe_pct"] == 100.0
    assert d["status"] == flow.EMPTY_PIPE


def test_报警位翻译() -> None:
    d = flow.decode([0, 3319, 0, 10000, 3125, 0, 0, 5030, 0, 0, 0, 0, 4, 1])
    assert d["alarm"] == 4
    assert d["alarm_labels"] == ["空管报警"]
    assert d["status"] == flow.ALARM
    # 认不出的位要报十六进制，不能瞎猜
    assert flow.alarm_labels(0x0010) == ["未知位 0x0010"]


def test_寄存器不足必须报错() -> None:
    with pytest.raises(ValueError):
        flow.decode([0, 3319])


# ---------------------------------------------------------------- 接口（需要库）


def test_flow_params(client: TestClient) -> None:
    body = client.get("/api/flow/params").json()
    assert body["model"] == "FMC400"
    assert body["register_count"] == 14
    assert body["units"]["flow"] == "m³/h"
    assert len(body["registers"]) == 8
    assert body["formula"]["flow"] == "s32(r2,r3) / 100"


def test_flow_latest(client: TestClient) -> None:
    body = client.get("/api/flow/latest").json()
    assert body["model"] == "FMC400"
    assert body["available"] is True
    assert body["rows_total"] >= len(ROWS)
    assert body["reading"]["regs"] and len(body["reading"]["regs"]) == 14


def test_flow_readings_按时间倒序且解码完整(client: TestClient) -> None:
    body = client.get(
        "/api/flow/readings", params={"start": WIN_START, "end": WIN_END, "limit": 10}
    ).json()
    assert body["count"] == len(ROWS)
    assert body["rows_total"] == len(ROWS)
    assert body["sampled"] is False
    items = body["items"]
    # 时间戳按服务器时区（Asia/Shanghai）渲染，比对瞬时值而不是字符串前缀
    ts = [datetime.fromisoformat(item["ts"]) for item in items]
    assert ts == [BASE + timedelta(hours=h) for h in (18, 12, 6, 0)]
    # 最新在前：第 4 行（报警，18:00）
    assert items[0]["status"] == flow.ALARM
    # 最旧在后：第 1 行（正常，00:00）
    assert items[-1]["flow_m3h"] == 211.16
    assert items[-1]["velocity_mps"] == 3.319
    assert items[-1]["total_m3"] == 5000
    assert items[-1]["direction_label"] == "正向"


def test_flow_readings_整窗抽样(client: TestClient) -> None:
    """limit 小于窗口行数时 `sample=auto` 必须整窗均匀取真实行。"""
    body = client.get(
        "/api/flow/readings",
        params={"start": WIN_START, "end": WIN_END, "limit": 2, "sample": "auto"},
    ).json()
    assert body["sampled"] is True
    assert body["rows_total"] == len(ROWS)
    assert body["count"] == 2
    # 两个 12 小时桶各出一条**真实行**，且一个落在前半窗、一个落在后半窗
    stamps = sorted(datetime.fromisoformat(item["ts"]) for item in body["items"])
    known = {BASE + timedelta(hours=h) for h in (0, 6, 12, 18)}
    assert all(stamp in known for stamp in stamps)
    assert stamps[0] < BASE + timedelta(hours=12) <= stamps[-1]


def test_flow_summary_区间统计与累积增量(client: TestClient) -> None:
    body = client.get(
        "/api/flow/summary", params={"start": WIN_START, "end": WIN_END}
    ).json()
    assert body["n"] == len(ROWS)
    assert body["n_empty_pipe"] == 1
    assert body["n_reverse"] == 1
    assert body["n_alarm"] == 1
    # (211.16 - 12.34 + 0 + 100) / 4 = 74.705 → 74.71
    assert body["flow_avg"] == pytest.approx(74.71, abs=0.01)
    assert body["flow_max"] == 211.16
    assert body["flow_min"] == -12.34
    # 满量程百分比：(65.98 + 65.98 + 0 + 31.25) / 4 = 40.8025
    assert body["percent_avg"] == pytest.approx(40.8, abs=0.01)
    assert body["total_first"] == 5000
    assert body["total_last"] == 5030
    assert body["total_delta"] == 30


def test_flow_summary_空窗口(client: TestClient) -> None:
    """没有数据的区间返回 0/None，而不是 500（看板在设备离线时要能正常打开）。"""
    body = client.get(
        "/api/flow/summary", params={"start": "2000-02-01T00:00:00Z", "end": "2000-02-02T00:00:00Z"}
    ).json()
    assert body["n"] == 0
    assert body["flow_avg"] is None
    assert body["total_delta"] is None
