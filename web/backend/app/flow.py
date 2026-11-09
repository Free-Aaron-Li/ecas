"""FMC400 电磁流量计（站号 8）寄存器块 → 工程量。

口径的**唯一实现处**在后端：库里只存 14 个原始寄存器（`flow_reading.regs`），
换算、32 位拼接、状态判定都在这里做，前端只消费算好的字段（与浓度计同一原则）。

寄存器表（`docs/设备资料/FMC400-电磁流量计-速查.md`，地址从 0 起）：

    0/1   流速        DW（有符号 32 位，hi<<16|lo）× 1000   → m/s
    2/3   瞬时流量    DW（有符号 32 位，hi<<16|lo）× 100    → m³/h
    4     流量百分比  B × 100                               → %
    5     空管百分比  B × 100                               → %
    6/7   正向累积    DW（无符号 32 位）× 1                 → m³
    8/9   反向累积    DW（无符号 32 位）× 1                 → m³
    12    报警位      位掩码（bit1 上限 / bit2 下限 / bit3 空管 / bit4 励磁）
    13    流向        0 = 反向，1 = 正向

⚠️ 两条设备侧约束（改口径前先看）：

1. 32 位量必须**高低字成对**读（同一次应答里取）。分成两次读的话，中间一旦进位，
   拼出来的瞬时流量会跳变 655.36 m³/h；采集端已经是「一次问 0~13」的整块读。
2. 累积量是 32 位计数，会回绕（现场约 8.5 天一轮），也允许人工清零
   ⇒ 区间增量只能按「末值 − 首值」估，回绕/清零时**不给**增量（见 `main.api_flow_summary`）。
"""

from __future__ import annotations

from typing import Any, Sequence

#: 设备型号：库里的 `model` 列固定这个值，取数与展示都用它
MODEL = "FMC400"
#: 一次应答的寄存器个数（读 0~13，正好 14 个）
REG_COUNT = 14

FLOW_UNIT = "m³/h"
VELOCITY_UNIT = "m/s"

#: 四档状态：与浓度计一样，前端只看状态字符串决定颜色，不自己判
NORMAL = "正常"
EMPTY_PIPE = "空管"
REVERSE = "反流"
ALARM = "报警"
STATUSES = (NORMAL, EMPTY_PIPE, REVERSE, ALARM)

#: 空管百分比到这个数就按「空管」算：正常有空管检测的仪表空管时给 100%，满管给 0
EMPTY_PIPE_HIGH = 50.0
#: 瞬时流量负到这个数以下才算「反流」：-0.01 是「0.00 m³/h 不出反流」的死区
REVERSE_BELOW = -0.01

#: 报警位标签（按手册 bit1~bit4 依次解释成 0x0001~0x0008）
ALARM_BITS: tuple[tuple[int, str], ...] = (
    (0x0001, "上限报警"),
    (0x0002, "下限报警"),
    (0x0004, "空管报警"),
    (0x0008, "励磁报警"),
)

#: 寄存器清单：既给 `GET /api/flow/params` 当接口说明，也给看板脚注当口径来源
REGISTERS: tuple[dict[str, Any], ...] = (
    {"addr": 0, "len": 2, "name": "流速", "unit": VELOCITY_UNIT, "scale": 1000, "signed": True},
    {"addr": 2, "len": 2, "name": "瞬时流量", "unit": FLOW_UNIT, "scale": 100, "signed": True},
    {"addr": 4, "len": 1, "name": "流量百分比", "unit": "%", "scale": 100, "signed": False},
    {"addr": 5, "len": 1, "name": "空管百分比", "unit": "%", "scale": 100, "signed": False},
    {"addr": 6, "len": 2, "name": "正向累积", "unit": "m³", "scale": 1, "signed": False},
    {"addr": 8, "len": 2, "name": "反向累积", "unit": "m³", "scale": 1, "signed": False},
    {"addr": 12, "len": 1, "name": "报警位", "unit": "", "scale": 1, "signed": False},
    {"addr": 13, "len": 1, "name": "流向", "unit": "", "scale": 1, "signed": False},
)


def _s16(value: int) -> int:
    """16 位有符号：0xFFFF → -1（负流量/负流速就是靠最高字为负表达的）。"""
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def _s32(hi: int, lo: int) -> int:
    """两个 16 位字按 **有符号** 32 位拼接（寄存器表里的 DW 都是这种）。"""
    return _s16(hi) * 0x10000 + (lo & 0xFFFF)


def _u32(hi: int, lo: int) -> int:
    """两个 16 位字按无符号 32 位拼接（正/反累积量）。"""
    return (hi & 0xFFFF) * 0x10000 + (lo & 0xFFFF)


def alarm_labels(bits: int) -> list[str]:
    """报警位 → 中文标签；认不出的位只报数值（手册之外不要瞎猜）。"""
    labels = [name for mask, name in ALARM_BITS if bits & mask]
    unknown = bits & ~sum(mask for mask, _ in ALARM_BITS)
    if unknown:
        labels.append(f"未知位 0x{unknown:04X}")
    return labels


def status_of(flow_m3h: float, empty_pipe_pct: float, alarm: int) -> str:
    """状态判定（顺序有意义：空管时流量读数没有物理意义，先报空管）。

    * 空管百分比 ≥ 50 → 空管
    * 瞬时流量 < -0.01 → 反流（负流量是数学事实，不依赖流向位）
    * 报警位 ≠ 0 → 报警（具体是哪一位见 `alarm_labels`）
    """
    if empty_pipe_pct >= EMPTY_PIPE_HIGH:
        return EMPTY_PIPE
    if flow_m3h < REVERSE_BELOW:
        return REVERSE
    if alarm:
        return ALARM
    return NORMAL


def decode(regs: Sequence[int]) -> dict[str, Any]:
    """寄存器 0~13 → 工程量（一行 `flow_reading.regs` 的全部含义）。

    `regs` 长度不足 14 直接报错：宁可不显示，也不要拿半块寄存器拼出假流量。
    """
    values = [int(v) for v in regs]
    if len(values) < REG_COUNT:
        raise ValueError(f"寄存器块长度 {len(values)} < {REG_COUNT}（读 0~13 才有完整含义）")
    r = values[:REG_COUNT]

    velocity_mps = _s32(r[0], r[1]) / 1000.0
    flow_m3h = _s32(r[2], r[3]) / 100.0
    percent = r[4] / 100.0
    empty_pipe_pct = r[5] / 100.0
    total_m3 = _u32(r[6], r[7])
    reverse_total_m3 = _u32(r[8], r[9])
    alarm = r[12]
    direction = r[13]

    return {
        "velocity_mps": round(velocity_mps, 3),
        "flow_m3h": round(flow_m3h, 2),
        "percent": round(percent, 2),
        "empty_pipe_pct": round(empty_pipe_pct, 2),
        "total_m3": total_m3,
        "reverse_total_m3": reverse_total_m3,
        "alarm": alarm,
        "alarm_labels": alarm_labels(alarm),
        "direction": direction,
        "direction_label": "正向" if direction else "反向",
        "status": status_of(flow_m3h, empty_pipe_pct, alarm),
    }


def params(s: Any) -> dict[str, Any]:
    """`GET /api/flow/params` 的内容：设备/单位/寄存器表/口径说明。

    和浓度计的 `/api/params` 一样，口径变了前端不用改代码——展示哪几个量、单位是什么，
    都由这里给。`stale_seconds` 沿用同一个断流阈值（两台设备共用一个看板刷新节奏）。
    """
    return {
        "model": MODEL,
        "register_count": REG_COUNT,
        "units": {
            "velocity": VELOCITY_UNIT,
            "flow": FLOW_UNIT,
            "percent": "%",
            "total": "m³",
        },
        "registers": [dict(item) for item in REGISTERS],
        "statuses": list(STATUSES),
        "alarm_bits": [{"mask": f"0x{mask:04X}", "label": name} for mask, name in ALARM_BITS],
        "stale_seconds": s.stale_seconds,
        "formula": {
            "velocity": "s32(r0,r1) / 1000",
            "flow": "s32(r2,r3) / 100",
            "percent": "r4 / 100",
            "empty_pipe": "r5 / 100",
            "total": "u32(r6,r7)",
        },
        "note": "空管百分比 ≥ 50% 判「空管」；瞬时流量 < -0.01 判「反流」；报警位 ≠ 0 判「报警」",
    }
