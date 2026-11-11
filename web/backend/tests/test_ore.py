"""综合干矿量的纯逻辑测试：公式、传输延时、逐点配对、抽样与汇总。

这些用例守的是「报表 / 接口 / 看板三处口径必须逐字一致」：
* `dry_ore_mass` ↔ 前端 `api.ts` 的 `dryOreMass`
* `pipe_delay` ↔ `pipeDelaySeconds`
* `pair_points` ↔ `App.tsx` 的 `oreStats`（±90 s 容差、只取正常点、延时补偿）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import ore

T0 = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


# ------------------------------------------------------------------ 公式


def test_干矿量公式_与前端逐行一致() -> None:
    # c = 0.2 ⇒ Q = 100 / (1/3.44 + 0.8/0.2) = 100 / 4.290698… = 23.3062…
    assert ore.dry_ore_mass(100.0, 20.0) == pytest.approx(23.3062, abs=1e-4)
    # 100% 浓度被夹到 0.999：分母 = 1/3.44 + 0.001/0.999
    assert ore.dry_ore_mass(10.0, 100.0) == pytest.approx(10.0 / (1 / 3.44 + 0.001 / 0.999), rel=1e-9)
    # 0% 浓度会除零，夹到 0.1% 后仍是有限值
    assert ore.dry_ore_mass(10.0, 0.0) is not None
    assert ore.dry_ore_mass(None, 20.0) is None
    assert ore.dry_ore_mass(10.0, None) is None


def test_传输延时等于管长除以流速() -> None:
    assert ore.pipe_delay(1.0) == pytest.approx(2.5)
    assert ore.pipe_delay(2.5) == pytest.approx(1.0)
    assert ore.pipe_delay(0.0) is None
    assert ore.pipe_delay(-1.0) is None
    assert ore.pipe_delay(None) is None


# ------------------------------------------------------------------ 配对


def test_只配对正常点且按延时错开流量时间轴() -> None:
    readings = [
        (at(0), 20.0, "正常"),
        (at(60), 20.0, "正常"),
        (at(120), 20.0, "断线"),          # 外推浓度无物理意义，必须跳过
        (at(180), 20.0, "超量程"),
    ]
    # 流速 1 m/s ⇒ Δt=2.5 s；错开 2.5 s 后仍落在同一个流量点上
    flows = [
        (at(0), 100.0, 1.0),
        (at(60), 200.0, 1.0),
        (at(120), 300.0, 1.0),
        (at(180), 400.0, 1.0),
    ]
    points = ore.pair_points(readings, flows)

    assert [p.ts for p in points] == [at(0), at(60)]
    assert points[0].flow_m3h == 100.0
    assert points[1].flow_m3h == 200.0
    assert points[0].delay_s == pytest.approx(2.5)
    assert points[0].q_tph == pytest.approx(ore.dry_ore_mass(100.0, 20.0))


def test_超出九十秒容差就不配() -> None:
    readings = [(at(0), 20.0, "正常"), (at(1000), 20.0, "正常")]
    flows = [(at(0), 100.0, 1.0), (at(2000), 100.0, 1.0)]

    points = ore.pair_points(readings, flows)

    assert len(points) == 1, "只有第一个浓度点在 ±90 s 内找得到流量"
    assert points[0].ts == at(0)


def test_流速为零时退化为同时刻配对() -> None:
    readings = [(at(0), 20.0, "正常")]
    flows = [(at(0), 100.0, 0.0)]

    points = ore.pair_points(readings, flows)

    assert len(points) == 1
    assert points[0].flow_ts == at(0)
    assert points[0].delay_s == 0.0


def test_没有流量点时不产出配对() -> None:
    assert ore.pair_points([(at(0), 20.0, "正常")], []) == ()


# ------------------------------------------------------------------ 汇总


def _points(n: int, q: float = 10.0) -> tuple[ore.OrePoint, ...]:
    return tuple(
        ore.OrePoint(
            ts=at(i),
            conc=20.0,
            flow_ts=at(i),
            flow_m3h=100.0,
            velocity_mps=1.0,
            delay_s=2.5,
            q_tph=q,
        )
        for i in range(n)
    )


def test_汇总取算术平均并按区间时长外推累计() -> None:
    summary = ore.summarize(_points(4, q=10.0), hours=12.0)

    assert summary.n == 4
    assert summary.avg_tph == pytest.approx(10.0)
    assert summary.total_t == pytest.approx(120.0)
    assert summary.avg_flow_m3h == pytest.approx(100.0)
    assert summary.avg_conc == pytest.approx(20.0)
    assert summary.first_ts == at(0) and summary.last_ts == at(3)


def test_少于两个配对不给均值() -> None:
    for n in (0, 1):
        summary = ore.summarize(_points(n), hours=12.0)
        assert summary.n == n
        assert summary.avg_tph is None
        assert summary.total_t is None


def test_时长非正时累计为零而不是负数() -> None:
    summary = ore.summarize(_points(2, q=10.0), hours=0.0)
    assert summary.total_t == 0.0


# ------------------------------------------------------------------ 抽样


def test_行数不超过上限时原样返回() -> None:
    items = [(at(i), i) for i in range(5)]
    assert ore.sample_centered(items, start=at(0), end=at(5), limit=5) == items


def test_抽样重放_width_bucket_的桶内居中行() -> None:
    """3 个桶装 10 个点：桶1取第 2 个、桶2取第 5 个、桶3取第 8 个（0-based）。"""
    items = [(at(i), i) for i in range(10)]
    sampled = ore.sample_centered(items, start=at(0), end=at(10), limit=3)

    assert [value for _, value in sampled] == [1, 5, 8]


def test_抽样结果保持时间升序() -> None:
    items = [(at(i), i) for i in range(100)]
    sampled = ore.sample_centered(items, start=at(0), end=at(100), limit=7)
    times = [ts for ts, _ in sampled]
    assert times == sorted(times)
    assert len(sampled) <= 7
