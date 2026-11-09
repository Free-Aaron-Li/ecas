"""接口契约（Pydantic 模型）。

前端只消费这里定义的形状；换算字段一律由后端给出，前端不重复实现口径。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from . import flow
from .convert import convert


class Reading(BaseModel):
    """一行读数：原始值 + 换算结果。"""

    ts: datetime
    model: str
    raw: int
    ma: float = Field(description="电流 mA = raw / 1000")
    conc_raw: float = Field(description="未补偿浓度")
    conc: float = Field(description="最终浓度（已按 offset_mode 补偿）")
    status: str = Field(description="断线 / 偏低 / 正常 / 超量程")


class ReadingsPage(BaseModel):
    hours: int
    #: 实际生效的时间窗（左闭右开）；预设区间是滑动窗口，自定义区间是绝对时刻
    start: datetime | None = None
    end: datetime | None = None
    limit: int
    offset: int
    #: 本次**返回**的行数（= `items` 长度）
    count: int
    #: 窗口内的**总**行数 —— `count < rows_total` 表示被 limit 截断或抽样过
    rows_total: int = 0
    #: 触发 `sample=auto` 抽样：`items` 是**整窗均匀**的真实读数，而不是"最新 limit 条"
    sampled: bool = False
    #: 抽样时一个时间桶的宽度（秒）= 窗口时长 / limit，前端据此说明"多久一个点"
    bucket_seconds: float | None = None
    items: list[Reading]


class Summary(BaseModel):
    """区间统计：均值/极值只统计**有效区间**（4000~20500），断线行不参与。"""

    hours: int
    start: datetime | None = None
    end: datetime | None = None
    n: int
    n_valid: int
    broken: int
    low: int
    over: int
    #: 越限行数（只在有效区间里数）：浓度低于下限 / 高于上限
    n_below_alarm: int = 0
    n_above_alarm: int = 0
    conc_avg: float | None = None
    conc_min: float | None = None
    conc_max: float | None = None
    raw_min: int | None = None
    raw_max: int | None = None


class Health(BaseModel):
    rows_total: int
    last_ts: datetime | None = None
    age_seconds: float | None = None
    stale: bool
    last: Reading | None = None


class Alarms(BaseModel):
    """浓度报警上下限 + 它们翻译成 raw 之后的判据（图表阈值线的唯一来源）。

    上下限可以在看板上自定义：`custom=true` 表示当前用的是自定义值，
    `default_low/default_high` 是服务端配置（环境变量）里的默认值，供"恢复默认"用。
    """

    low: float
    high: float
    low_raw: int
    high_raw: int
    custom: bool = False
    default_low: float | None = None
    default_high: float | None = None


class AlarmEvent(BaseModel):
    """一条报警事件（进行中时 `ended_ts` 为空、`active=true`）。"""

    id: int
    kind: str = Field(description="break / over / conc_high / conc_low / no_data")
    kind_label: str
    level: str = Field(description="严重 / 警告")
    started_ts: datetime
    ended_ts: datetime | None = None
    duration_seconds: float
    active: bool
    peak_raw: int | None = None
    peak_conc: float | None = None
    peak_ts: datetime | None = None
    n_rows: int = 0
    note: str | None = None


class AlarmsPage(BaseModel):
    hours: int
    start: datetime | None = None
    end: datetime | None = None
    count: int
    active: int = Field(description="其中还在进行中的条数")
    items: list[AlarmEvent]
    scan: dict[str, Any] | None = Field(default=None, description="最近一次扫描的结果（可选）")


class Params(BaseModel):
    """当前口径（口径变了，前端不用改代码）。"""

    model: str
    range: dict[str, float]
    offset_mode: str
    offset_conc: float
    offset_pct: float
    thresholds: dict[str, int]
    alarms: Alarms
    stale_seconds: int
    export_limit: int = 20000
    formula: dict[str, str]


class AuthInfo(BaseModel):
    """值守门状态：前端据此决定要不要弹令牌输入（这个接口本身不需要令牌）。"""

    service: str = "ecas-backend"
    auth_required: bool


def to_reading(row: dict[str, Any], s: Any) -> Reading:
    """库行（ts/model/raw）+ 换算 → Reading。"""
    return Reading(ts=row["ts"], model=row["model"], **convert(int(row["raw"]), s))


# —— 电磁流量计（FMC400）——
#
# 与浓度计同一原则：库里只存原始寄存器，换算一律在这里给出。
# 寄存器块用 `integer[]` 原样存，`flow.decode()` 负责拼 32 位、除以倍率、判状态。


class FlowReading(BaseModel):
    """一行流量读数：14 个寄存器原样 + 后端解码结果。"""

    ts: datetime
    model: str
    regs: list[int] = Field(description="寄存器 0~13 原样（便于对账：任何解码都能用原始值复核）")
    velocity_mps: float = Field(description="流速 m/s = s32(r0,r1) / 1000")
    flow_m3h: float = Field(description="瞬时流量 m³/h = s32(r2,r3) / 100（负数=反流）")
    percent: float = Field(description="流量百分比 % = r4 / 100")
    empty_pipe_pct: float = Field(description="空管百分比 % = r5 / 100（≥50 判空管）")
    total_m3: int = Field(description="正向累积 m³ = u32(r6,r7)，32 位会回绕")
    reverse_total_m3: int = Field(description="反向累积 m³ = u32(r8,r9)")
    alarm: int = Field(description="报警位原值（位掩码）")
    alarm_labels: list[str] = Field(description="报警位翻译成中文（认不出的位给十六进制）")
    direction: int = Field(description="流向原值：0=反向，1=正向")
    direction_label: str
    status: str = Field(description="正常 / 空管 / 反流 / 报警")


class FlowPage(BaseModel):
    """`/api/flow/readings` 的分页结果（形状与浓度计 ReadingsPage 对齐）。"""

    hours: int
    start: datetime | None = None
    end: datetime | None = None
    limit: int
    offset: int
    count: int
    rows_total: int = 0
    sampled: bool = False
    bucket_seconds: float | None = None
    items: list[FlowReading]


class FlowSummary(BaseModel):
    """区间统计：均值/极值只统计**本次窗口内的行**（空管/反流也计入流量均值，它们仍是读数）。"""

    hours: int
    start: datetime | None = None
    end: datetime | None = None
    n: int
    n_empty_pipe: int = 0
    n_reverse: int = 0
    n_alarm: int = 0
    flow_avg: float | None = None
    flow_min: float | None = None
    flow_max: float | None = None
    velocity_avg: float | None = None
    velocity_max: float | None = None
    empty_pipe_avg: float | None = None
    percent_avg: float | None = None
    #: 区间首/末的累积量（m³，原值）；`total_delta` 在末值 < 首值（回绕/清零）时为 null
    total_first: int | None = None
    total_last: int | None = None
    total_delta: int | None = None


class FlowLatest(BaseModel):
    """最新一条流量读数 + 新鲜度。`available=false` 表示库里还没有流量行（采集未接入）。"""

    model: str
    available: bool
    rows_total: int = 0
    last_ts: datetime | None = None
    age_seconds: float | None = None
    stale: bool = True
    reading: FlowReading | None = None


class FlowParams(BaseModel):
    """流量计口径说明（前端据此写单位、脚注；解码公式改了这里不用改前端）。"""

    model: str
    register_count: int
    units: dict[str, str]
    registers: list[dict[str, Any]]
    statuses: list[str]
    alarm_bits: list[dict[str, str]]
    stale_seconds: int
    formula: dict[str, str]
    note: str


def to_flow_reading(row: dict[str, Any]) -> FlowReading:
    """库行（ts/model/regs）+ 解码 → FlowReading。"""
    return FlowReading(
        ts=row["ts"],
        model=row["model"],
        regs=[int(v) for v in row["regs"]],
        **flow.decode(row["regs"]),
    )
