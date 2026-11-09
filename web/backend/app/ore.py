"""综合干矿量：把浓度读数与流量读数**逐点配对**后算 Q，再取平均/累计。

设计要点
--------
* **口径与看板逐字同源**：公式、真密度、管道间距、配对容差、传输延时补偿全部照
  `web/frontend/src/api.ts` 的 `dryOreMass` / `pipeDelaySeconds` 与
  `web/frontend/src/App.tsx` 的 `oreStats` 实现，改一边必须同步改另一边。
* **非线性，不能先平均再相乘**：``Q = V / (1/ρp + (1−C)/C)`` 对 C 非线性，
  「平均流量 × 平均浓度」会系统性偏小，所以必须逐点算 Q 之后再取算术平均。
* **抽样同源**：看板趋势图用 `/api/readings?sample=auto&limit=4000` 的整窗均匀抽样，
  报表要与网页数字对得上，就按同一套 `width_bucket` 规则在本地重放抽样
  （时间桶居中那条真实行），而不是拿全量 5 万行算出一个「更准但不一样」的数。
* **浓度只取「正常」点**：断线 / 偏低 / 超量程的浓度是数学外推值，没有物理意义。
* **延时补偿**：浓度计在上游、流量计在下游约 2.5 m，同一团矿浆先过浓度计，
  按实测流速把流量点的时间轴往后错 ``Δt = L / v`` 再配对（``v≤0`` 时退化为同时刻）。
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence, TypeVar

#: 铁精矿真密度 ρp（t/m³）：现场固定值，不是仪表参数（2026-11-05 先生先改 4.1 后又改回 4.3）
TRUE_DENSITY = 4.3

#: 浓度计与流量计的管道间距（m）：现场约 2~3 m，取中值 2.5
PIPE_GAP_M = 2.5

#: 浓度点与流量点的配对容差（秒）：两块表各自采样，±90 s 内才算「同一时刻」
PAIR_TOLERANCE_S = 90.0

#: 与看板趋势图相同的抽样上限（App.tsx 的 `CHART_POINTS`）；超过就整窗均匀抽样
SAMPLE_LIMIT = 4000


@dataclass(frozen=True)
class OrePoint:
    """一个「浓度点 × 补偿后的流量点」配对结果。"""

    ts: datetime
    conc: float
    #: 真正配对上的那个流量点的时刻（补偿延时之后找的；明细表里可直接对账）
    flow_ts: datetime
    flow_m3h: float
    velocity_mps: float
    delay_s: float
    q_tph: float


@dataclass(frozen=True)
class OreSummary:
    """区间干矿量汇总（对应网页顶部「综合干矿量」卡）。"""

    points: tuple[OrePoint, ...]
    hours: float
    avg_tph: float | None
    total_t: float | None
    avg_flow_m3h: float | None
    avg_conc: float | None
    first_ts: datetime | None
    last_ts: datetime | None

    @property
    def n(self) -> int:
        return len(self.points)


def dry_ore_mass(volume_m3: float | None, conc_pct: float | None) -> float | None:
    """干矿量 Q（t）＝ ``V / (1/ρp + (1−C)/C)``，V 为 m³、C 为质量浓度（%）。

    C 先换算成 0~1 的小数并夹到 ``(0, 1)`` 开区间：C=0 没有矿、C=100% 分母只剩
    ``1/ρp``，两端都是物理极限，直接代会除零。与前端 `dryOreMass` 逐行一致。
    """
    if volume_m3 is None or conc_pct is None:
        return None
    c = min(0.999, max(0.001, conc_pct / 100.0))
    return volume_m3 / (1.0 / TRUE_DENSITY + (1.0 - c) / c)


def pipe_delay(velocity_mps: float | None) -> float | None:
    """传输延时（秒）＝ ``L / v``；流速缺失或 ≤0 时返回 None（调用方退化成不补偿）。"""
    if velocity_mps is None or velocity_mps <= 0:
        return None
    return PIPE_GAP_M / velocity_mps


def pair_points(
    readings: Iterable[tuple[datetime, float, str]],
    flows: Sequence[tuple[datetime, float, float]],
    *,
    tolerance_s: float = PAIR_TOLERANCE_S,
) -> tuple[OrePoint, ...]:
    """把浓度点与流量点逐点配对，返回瞬时干矿量序列（时间正序）。

    * ``readings``：``(时间, 浓度%, 状态)``，只保留状态为「正常」的点；
    * ``flows``：``(时间, 流量 m³/h, 流速 m/s)``，内部按时间升序重排。

    配对规则（与 App.tsx `oreStats` 一致）：
    1. 先找同时刻最近的流量点（±``tolerance_s``），用它算传输延时；
    2. 有延时就把流量点的时间轴往后错 Δt 再找一次，找不到退回同时刻那个点；
    3. 用配上的流量与该浓度点算 Q；一个浓度点最多产出一个 Q。
    """
    ordered = sorted(flows, key=lambda f: f[0])
    times = [f[0] for f in ordered]

    def nearest(target: datetime) -> tuple[datetime, float, float] | None:
        if not ordered:
            return None
        i = bisect_left(times, target)
        best: tuple[datetime, float, float] | None = None
        for j in (i, i - 1):
            if 0 <= j < len(ordered):
                cand = ordered[j]
                if best is None or abs(cand[0] - target) < abs(best[0] - target):
                    best = cand
        if best is None or abs(best[0] - target).total_seconds() > tolerance_s:
            return None
        return best

    points: list[OrePoint] = []
    for ts, conc, status in readings:
        if status != "正常":
            continue
        same = nearest(ts)
        if same is None:
            continue
        delay = pipe_delay(same[2]) or 0.0
        shifted = nearest(ts + timedelta(seconds=delay)) if delay > 0 else None
        best = shifted or same
        q = dry_ore_mass(best[1], conc)
        if q is None:
            continue
        points.append(
            OrePoint(
                ts=ts,
                conc=conc,
                flow_ts=best[0],
                flow_m3h=best[1],
                velocity_mps=best[2],
                delay_s=delay,
                q_tph=q,
            )
        )
    return tuple(points)


def summarize(points: Sequence[OrePoint], *, hours: float) -> OreSummary:
    """对配对结果取平均并外推累计量：累计 ＝ 平均干矿量 × 区间时长。

    ``hours`` 由调用方给：已结束的班次 = 整班 12 h，进行中的班次 = 开始→导出时刻，
    自定义时段 = 起止跨度（终点超过此刻时截到此刻）。不用梯形积分 —— 断流空洞会让
    积分只覆盖一小段，算出来的「累计」既不是整段也不可比（先生 2026-11-05 定）。
    配对数不足 2 个时不给均值（前端同样返回 null），避免单点噪声被当成产量。
    """
    n = len(points)
    avg = sum(p.q_tph for p in points) / n if n >= 2 else None
    total = None
    if avg is not None:
        total = avg * hours if hours > 0 else 0.0
    return OreSummary(
        points=tuple(points),
        hours=hours,
        avg_tph=avg,
        total_t=total,
        avg_flow_m3h=(sum(p.flow_m3h for p in points) / n) if n else None,
        avg_conc=(sum(p.conc for p in points) / n) if n else None,
        first_ts=points[0].ts if points else None,
        last_ts=points[-1].ts if points else None,
    )


_T = TypeVar("_T")


def sample_centered(
    items: Sequence[tuple[datetime, _T]],
    *,
    start: datetime,
    end: datetime,
    limit: int = SAMPLE_LIMIT,
) -> list[tuple[datetime, _T]]:
    """重放 `/api/readings?sample=auto` 的整窗均匀抽样（时间桶居中那条真实行）。

    SQL 里用 ``width_bucket(extract(epoch FROM ts), start, end, limit)`` 把窗口等分，
    每桶按时间排序后取 ``rn = (n + 1) / 2``（整数除法 ⇒ 0-based 下标 ``(n-1)//2``）。
    这里按同一规则在内存里做一遍，保证报表里的干矿量与看板抽样图用的是**同一批点**。

    ``items`` 必须已按时间升序；行数不超过 ``limit`` 时原样返回。
    """
    if len(items) <= limit:
        return list(items)
    span = (end - start).total_seconds()
    if span <= 0:
        return list(items)
    buckets: dict[int, list[tuple[datetime, _T]]] = {}
    for item in items:
        index = int((item[0] - start).total_seconds() / span * limit) + 1
        index = max(1, min(limit, index))
        buckets.setdefault(index, []).append(item)
    sampled: list[tuple[datetime, _T]] = []
    for index in sorted(buckets):
        group = buckets[index]
        sampled.append(group[(len(group) - 1) // 2])
    return sampled


def from_rows(rows: Sequence[dict[str, Any]], convert_module: Any, settings: Any) -> list[tuple[datetime, float, str]]:
    """把报表里的原始行换成 ``(时间, 浓度, 状态)``；换算走 `app.convert`，不另立口径。"""
    out: list[tuple[datetime, float, str]] = []
    for row in rows:
        reading = convert_module.convert(int(row["raw"]), settings)
        out.append((row["ts"], float(reading["conc"]), str(reading["status"])))
    return out
