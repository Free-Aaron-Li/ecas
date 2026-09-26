"""原始值 → 电流 → 浓度 的换算与补偿。

口径与云端 `scripts/485/app/convert.py` 逐行对齐（含四档状态与银行家舍入的行为），
差异只有一处：这里用 psycopg 3，云端旧脚本用 psycopg2（待后续统一）。

规则：
    未补偿浓度 = (raw - i_lo*1000) * (c_hi - c_lo) / ((i_hi - i_lo)*1000) + c_lo
    最终浓度   = 未补偿浓度 + offset_conc          （offset_mode == "add"，现役）
               或 未补偿浓度 * (1 + offset_pct/100) （offset_mode == "mul"，备选）
"""

from __future__ import annotations

from typing import Any

#: 四档状态（参照 NAMUR NE43：3.6 mA 以下=断线，3.6~4 mA=活零点以下，20.5 mA 以上=超量程）
BREAK = "断线"
LOW = "偏低"
NORMAL = "正常"
OVER = "超量程"


def raw_to_ma(raw: int | float) -> float:
    """原始值 ÷ 1000 = 电流（mA）。"""
    return round(float(raw) / 1000.0, 3)


def raw_to_conc_raw(raw: int | float, s: Any) -> float:
    """未补偿浓度（不叠加任何偏移）。

    入参统一 `float()` 化：PostgreSQL 的 `AVG(int)` 回来是 `Decimal`，
    不转换会报 `unsupported operand type(s) for -: 'decimal.Decimal' and 'float'`。
    """
    value = float(raw)
    span = (s.i_hi - s.i_lo) * 1000.0
    return round((value - s.i_lo * 1000.0) * (s.c_hi - s.c_lo) / span + s.c_lo, 3)


def apply_offset(conc: float, s: Any) -> float:
    """唯一一处补偿逻辑：add 加性偏移 / mul 乘性百分比。"""
    if getattr(s, "offset_mode", "add") == "mul":
        return conc * (1.0 + s.offset_pct / 100.0)
    return conc + s.offset_conc


def raw_to_conc(raw: int, s: Any) -> float:
    """最终浓度（已补偿）。"""
    return round(apply_offset(raw_to_conc_raw(raw, s), s), 3)


def raw_to_status(raw: int, s: Any) -> str:
    """四档状态判定。"""
    if raw < 3600:
        return BREAK
    if raw < 4000:
        return LOW
    if raw > s.over_high:
        return OVER
    return NORMAL


def conc_to_raw(conc: float, s: Any) -> float:
    """浓度的反解：浓度 → 原始值。

    报警上下限是**浓度**上的限制，但库里只有 raw（口径可能变），
    所以 SQL 过滤与图表画线都先经过这里翻译成 raw，保证与正向换算同一处实现。
    """
    if getattr(s, "offset_mode", "add") == "mul":
        conc_raw = conc / (1.0 + s.offset_pct / 100.0)
    else:
        conc_raw = conc - s.offset_conc
    span = (s.i_hi - s.i_lo) * 1000.0
    return s.i_lo * 1000.0 + (conc_raw - s.c_lo) * span / (s.c_hi - s.c_lo)


def convert(raw: int, s: Any) -> dict[str, Any]:
    """一次算全：给前端/接口统一的字段。"""
    return {
        "raw": raw,
        "ma": raw_to_ma(raw),
        "conc_raw": raw_to_conc_raw(raw, s),
        "conc": raw_to_conc(raw, s),
        "status": raw_to_status(raw, s),
    }


def convert_row(row: dict[str, Any], s: Any) -> dict[str, Any]:
    """库里的行（ts/model/raw）→ 带换算结果的字典。"""
    out = dict(row)
    out.update(convert(int(row["raw"]), s))
    return out


def resolve_alarms(s: Any, alarm_low: float | None = None, alarm_high: float | None = None) -> dict[str, Any]:
    """把「报警上下限」定下来，并翻译成 raw 判据 —— **唯一一处**。

    优先级：请求里带的（看板上的自定义值）> 服务端配置（`ECAS_ALARM_LOW/HIGH`）。
    前端不自己换算浓度→raw，它只把自己填的上下限传回来，由这里翻译，
    这样「画线用的数」和「SQL 统计用的数」永远是同一个。
    """
    low = s.alarm_low if alarm_low is None else float(alarm_low)
    high = s.alarm_high if alarm_high is None else float(alarm_high)
    return {
        "low": low,
        "high": high,
        "low_raw": round(conc_to_raw(low, s)),
        "high_raw": round(conc_to_raw(high, s)),
        "custom": low != s.alarm_low or high != s.alarm_high,
        "default_low": s.alarm_low,
        "default_high": s.alarm_high,
    }


def params(s: Any, alarm_low: float | None = None, alarm_high: float | None = None) -> dict[str, Any]:
    """把当前口径完整暴露给前端（前端不自己算，避免两处口径）。"""
    if getattr(s, "offset_mode", "add") == "mul":
        formula_conc = "conc_raw * (1 + offset_pct/100)"
    else:
        formula_conc = "conc_raw + offset_conc"
    return {
        "model": s.model_name,
        "range": {"i_lo": s.i_lo, "i_hi": s.i_hi, "c_lo": s.c_lo, "c_hi": s.c_hi},
        "offset_mode": s.offset_mode,
        "offset_conc": s.offset_conc,
        "offset_pct": s.offset_pct,
        "thresholds": {"break_low": s.break_low, "over_high": s.over_high},
        #: 上下限翻译成 raw（SQL 判据 / 图表阈值线的唯一来源）
        "alarms": resolve_alarms(s, alarm_low, alarm_high),
        "stale_seconds": s.stale_seconds,
        "export_limit": s.export_rows_max,
        "formula": {
            "ma": "raw / 1000",
            "conc_raw": f"(raw - {s.i_lo * 1000:.0f}) * ({s.c_hi} - {s.c_lo}) / (({s.i_hi} - {s.i_lo}) * 1000) + {s.c_lo}",
            "conc": formula_conc,
        },
    }
