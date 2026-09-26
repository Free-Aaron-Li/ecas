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


class Params(BaseModel):
    """当前口径（口径变了，前端不用改代码）。"""

    model: str
    range: dict[str, float]
    offset_mode: str
    offset_conc: float
    offset_pct: float
    thresholds: dict[str, int]
    stale_seconds: int
    formula: dict[str, str]


class AuthInfo(BaseModel):
    """值守门状态：前端据此决定要不要弹令牌输入（这个接口本身不需要令牌）。"""

    service: str = "ecas-backend"
    auth_required: bool


def to_reading(row: dict[str, Any], s: Any) -> Reading:
    """库行（ts/model/raw）+ 换算 → Reading。"""
    return Reading(ts=row["ts"], model=row["model"], **convert(int(row["raw"]), s))
