"""换算与补偿口径的回归测试（14 例 + 补偿模式 + 契约）。

口径：未补偿浓度 = (raw - 4000) / 160；最终浓度 = 未补偿 + 0.3（offset_mode="add"）。
期望值均由 `app/config.py` 实际口径算出后写死，任何口径漂移都会让测试失败。
"""

from __future__ import annotations

import pytest

from app.convert import (
    BREAK,
    LOW,
    NORMAL,
    OVER,
    apply_offset,
    convert,
    params,
    raw_to_conc,
    raw_to_conc_raw,
    raw_to_ma,
    raw_to_status,
)
from app.settings import get_settings

S = get_settings()

#: (raw, mA, 未补偿浓度, 最终浓度, 状态)
CASES = [
    (0, 0.000, -25.000, -24.700, BREAK),
    (3599, 3.599, -2.506, -2.206, BREAK),
    (3600, 3.600, -2.500, -2.200, LOW),
    (3900, 3.900, -0.625, -0.325, LOW),
    (3999, 3.999, -0.006, 0.294, LOW),
    (4000, 4.000, 0.000, 0.300, NORMAL),
    (8000, 8.000, 25.000, 25.300, NORMAL),
    (12000, 12.000, 50.000, 50.300, NORMAL),
    (12935, 12.935, 55.844, 56.144, NORMAL),
    (15202, 15.202, 70.013, 70.313, NORMAL),
    (20000, 20.000, 100.000, 100.300, NORMAL),
    (20500, 20.500, 103.125, 103.425, NORMAL),
    (20501, 20.501, 103.131, 103.431, OVER),
    (65535, 65.535, 384.594, 384.894, OVER),
]


@pytest.mark.parametrize(("raw", "ma", "conc_raw", "conc", "status"), CASES)
def test_convert_table(raw: int, ma: float, conc_raw: float, conc: float, status: str) -> None:
    assert raw_to_ma(raw) == ma
    assert raw_to_conc_raw(raw, S) == conc_raw
    assert raw_to_conc(raw, S) == conc
    assert raw_to_status(raw, S) == status


def test_现场锚点() -> None:
    """现场速查锚点：4000→0、12000→50、20000→100（未补偿）。"""
    assert raw_to_conc_raw(4000, S) == 0.0
    assert raw_to_conc_raw(12000, S) == 50.0
    assert raw_to_conc_raw(20000, S) == 100.0


def test_补偿是加性而非乘性() -> None:
    """现役口径是 +0.3 个点；若有人改成 ×1.003，这条会失败。"""
    for raw, _, conc_raw, conc, _ in CASES:
        assert conc == pytest.approx(round(conc_raw + 0.3, 3))
    assert raw_to_conc(12935, S) == 56.144  # 12935*1.003 口径会是 56.011，明显不同


def test_mul_模式仍然可用() -> None:
    """offset_mode="mul" 时走乘性百分比（备选口径，改一行配置即切换）。"""
    mul = S.model_copy(update={"offset_mode": "mul", "offset_pct": 0.3})
    assert apply_offset(100.0, mul) == pytest.approx(100.3)
    assert raw_to_conc(12935, mul) == pytest.approx(round(55.844 * 1.003, 3))


def test_断线判据() -> None:
    """raw < 3600（<3.6 mA）判断线：报警而不是当浓度显示。"""
    assert raw_to_status(3599, S) == BREAK
    assert raw_to_status(3600, S) == LOW


def test_convert_字段齐全() -> None:
    got = convert(12935, S)
    assert got == {
        "raw": 12935,
        "ma": 12.935,
        "conc_raw": 55.844,
        "conc": 56.144,
        "status": NORMAL,
    }


def test_params_暴露现役口径() -> None:
    p = params(S)
    assert p["offset_mode"] == "add"
    assert p["offset_conc"] == 0.3
    assert p["range"] == {"i_lo": 4.0, "i_hi": 20.0, "c_lo": 0.0, "c_hi": 100.0}
    assert p["thresholds"] == {"break_low": 3600, "over_high": 20500}
    assert p["formula"]["conc"] == "conc_raw + offset_conc"


def test_环境变量可覆盖口径() -> None:
    """ECAS_ 前缀的环境变量能覆盖配置（部署换口径不必改代码）。"""
    import os

    os.environ["ECAS_OFFSET_CONC"] = "0.5"
    try:
        from app.settings import Settings

        assert Settings().offset_conc == 0.5
    finally:
        del os.environ["ECAS_OFFSET_CONC"]
