"""接口契约（Pydantic 模型）。

前端只消费这里定义的形状；换算字段一律由后端给出，前端不重复实现口径。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

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
    limit: int
    offset: int
    count: int
    items: list[Reading]


class Summary(BaseModel):
    """区间统计：均值/极值只统计**有效区间**（4000~20500），断线行不参与。"""

    hours: int
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
