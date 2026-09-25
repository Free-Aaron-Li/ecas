# -*- coding: utf-8 -*-
"""换算自检（纯标准库，Mac 上就能跑）：python3 test_convert.py

覆盖：量程锚点、现场实测值、补偿口径（默认加性 +0.3）、断线/超量程边界。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import convert as cv          # noqa: E402
from config import OFFSET_CONC, OFFSET_MODE, BREAK_LOW, OVER_HIGH   # noqa: E402

CASES = [
    # raw,    mA,      未补偿,   浓度,     状态
    (4000,    4.000,   0.000,    0.300,    "正常"),      # 活零：4 mA = 0 浓度
    (8000,    8.000,   25.000,   25.300,   "正常"),
    (12000,   12.000,  50.000,   50.300,   "正常"),      # 锚点
    (12935,   12.935,  55.844,   56.144,   "正常"),      # 现场实测（12.935 mA）
    (15202,   15.202,  70.013,   70.312,   "正常"),      # 回灌数据里挑的高值
    (20000,   20.000,  100.000,  100.300,  "正常"),      # 满量程
    (0,       0.000,   -25.000,  -24.700,  "断线"),      # 0 mA = 断线/故障
    (3599,    3.599,   -2.506,   -2.206,   "断线"),      # 断线判据边界下侧
    (3600,    3.600,   -2.500,   -2.200,   "偏低"),      # 3.6 mA：故障线之上、活零点之下
    (3900,    3.900,   -0.625,   -0.325,   "偏低"),      # 活零点下方一小段
    (3999,    3.999,   -0.006,   0.294,    "偏低"),
    (20500,   20.500,  103.125,  103.425,  "正常"),      # 超量程判据边界上侧（不到 20.5mA 都算正常）
    (20501,   20.501,  103.131,  103.431,  "超量程"),
    (65535,   65.535,  384.594,  384.894,  "超量程"),
]


def main():
    bad = 0
    print(f"{'raw':>6} {'mA':>8} {'未补偿':>9} {'浓度':>9}  状态")
    for raw, ma, crawl, conc, st in CASES:
        got = cv.convert(raw)
        ok = (abs(got["ma"] - ma) < 1e-9
              and abs(got["conc_raw"] - crawl) < 1e-6
              and abs(got["conc"] - conc) < 1e-6
              and got["status"] == st)
        if not ok:
            bad += 1
        print(f"{raw:>6} {got['ma']:>8.3f} {got['conc_raw']:>9.3f} {got['conc']:>9.3f}  "
              f"{got['status']}{'' if ok else '   ← ❌ 期望 %s/%s/%s/%s' % (ma, crawl, conc, st)}")

    # 口径自证：默认必须是「加」（加性），不是「乘」
    if OFFSET_MODE == "add":
        assert abs(cv.raw_to_conc(12000) - (cv.raw_to_conc_raw(12000) + OFFSET_CONC)) < 1e-9
        assert abs(cv.raw_to_conc(12000) - cv.raw_to_conc_raw(12000) * (1 + OFFSET_CONC / 100)) > 0.1, \
            "补偿口径疑似变成了乘性 —— 09-26 定案是加性 +0.3 个点"
    assert cv.apply_offset(100.0) != 100.0, "补偿未生效"
    assert cv.raw_to_conc(12000) == cv.apply_offset(cv.raw_to_conc_raw(12000))
    assert cv.raw_to_status(BREAK_LOW - 1) == "断线" and cv.raw_to_status(BREAK_LOW) == "偏低"
    assert cv.raw_to_status(3999) == "偏低" and cv.raw_to_status(4000) == "正常"
    assert cv.raw_to_status(OVER_HIGH) == "正常" and cv.raw_to_status(OVER_HIGH + 1) == "超量程"

    print()
    if bad:
        print(f"❌ {bad} 条不符合预期")
        return 1
    print(f"✅ {len(CASES)} 条全部通过 ｜ 补偿口径 = {OFFSET_MODE} +{OFFSET_CONC}（09-26 定案）"
          f"｜ 断线 raw<{BREAK_LOW} ｜ 偏低 到 4000 ｜ 超量程 raw>{OVER_HIGH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
