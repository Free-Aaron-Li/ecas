# -*- coding: utf-8 -*-
"""raw 原始值 → mA / 未补偿浓度 / 最终浓度 / 状态

口径（09-25 与 09-26 两次定案后的唯一实现，改口径只动 config.py）：
    mA        = raw / 1000
    未补偿     = (raw - I_LO*1000) / ((I_HI - I_LO)*1000) * (C_HI - C_LO) + C_LO
    浓度       = 未补偿 + OFFSET_CONC          ← ⭐ 加性（默认，09-26 先生定：+0.3 个点）
                 （若 OFFSET_MODE="mul" 则 = 未补偿 × (1 + OFFSET_PCT/100) ）
    状态       = 断线(raw < break_low) / 偏低(活零点以下) / 超量程(raw > over_high) / 正常

状态分档参照 NAMUR NE43 的常规做法（4~20 mA 回路）：
    raw < 3600      3.6 mA 以下   → 断线/故障（0 mA 就是断线）
    3600 ~ 4000     3.6~4.0 mA   → 偏低（活零点以下，多为标定偏差或轻微异常，别当正常浓度看）
    4000 ~ 20500    4.0~20.5 mA  → 正常
    > 20500         20.5 mA 以上 → 超量程，读数不可信
"""
from config import (I_LO, I_HI, C_LO, C_HI,
                    OFFSET_MODE, OFFSET_CONC, OFFSET_PCT,
                    BREAK_LOW, OVER_HIGH)

STATUS_OK = "正常"
STATUS_BREAK = "断线"
STATUS_LOW = "偏低"
STATUS_OVER = "超量程"


def raw_to_ma(raw):
    """原始值 → 电流 mA（三位小数）"""
    return round(raw / 1000.0, 3)


def raw_to_conc_raw(raw):
    """原始值 → 未补偿浓度（三位小数）"""
    span = (I_HI - I_LO) * 1000.0
    conc = (raw - I_LO * 1000.0) * (C_HI - C_LO) / span + C_LO
    return round(conc, 3)


def apply_offset(conc):
    """把补偿加到「未补偿浓度」上（唯一一处补偿逻辑）"""
    if OFFSET_MODE == "mul":
        return conc * (1.0 + OFFSET_PCT / 100.0)
    return conc + OFFSET_CONC


def raw_to_conc(raw):
    """原始值 → 最终浓度 = 未补偿 + 补偿（三位小数）"""
    span = (I_HI - I_LO) * 1000.0
    conc = (raw - I_LO * 1000.0) * (C_HI - C_LO) / span + C_LO
    return round(apply_offset(conc), 3)


def raw_to_status(raw):
    """raw → 状态字符串（四档，见模块头注释）"""
    if raw < BREAK_LOW:
        return STATUS_BREAK
    if raw < I_LO * 1000:
        return STATUS_LOW
    if raw > OVER_HIGH:
        return STATUS_OVER
    return STATUS_OK


def convert(raw):
    """原始值 → 一条完整记录（前端直接可用）"""
    return {
        "raw": raw,
        "ma": raw_to_ma(raw),
        "conc_raw": raw_to_conc_raw(raw),
        "conc": raw_to_conc(raw),
        "status": raw_to_status(raw),
    }


def convert_row(row):
    """给 SQL 查出来的行补上换算列（row 里必须有 raw 与 ts）"""
    out = convert(row["raw"])
    out["ts"] = row["ts"]
    return out


def params():
    """把后端持有的参数暴露出去（前端/上位机能自证口径）"""
    return {
        "i_lo": I_LO, "i_hi": I_HI, "c_lo": C_LO, "c_hi": C_HI,
        "offset_mode": OFFSET_MODE,
        "offset_conc": OFFSET_CONC,
        "offset_pct": OFFSET_PCT,
        "break_low": BREAK_LOW, "over_high": OVER_HIGH,
        "formula": {
            "ma": "raw / 1000",
            "conc_raw": "(raw - i_lo*1000) / ((i_hi - i_lo)*1000) * (c_hi - c_lo) + c_lo",
            "conc": ("conc_raw * (1 + offset_pct/100)" if OFFSET_MODE == "mul"
                     else "conc_raw + offset_conc"),
        },
    }
