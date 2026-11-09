#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ecas_capture.py 的离线测试（不需要设备、不需要 MQTT）。

用现场真实抓到的帧（2026-11-02 Modbus Poll 读数）与手册附录 C 的示例构造用例，
验证解码、帧长自识别、异常/坏帧处理与判定逻辑。

跑法：
    python3 scripts/485/tests/test_ecas_capture.py
"""

import importlib.util
import struct
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

CST = timezone(timedelta(hours=8))
TOOL = Path(__file__).resolve().parents[1] / "ecas_capture.py"
spec = importlib.util.spec_from_file_location("ecas_capture", TOOL)
mod = importlib.util.module_from_spec(spec)
sys.modules["ecas_capture"] = mod
spec.loader.exec_module(mod)


def tcp_frame(station: int, fc: int, data: bytes) -> str:
    """按 DTU 的 Modbus TCP 转换规则拼帧：6B MBAP + 站号 + 功能码 + 字节数 + 数据。"""
    body = bytes([station, fc, len(data)]) + data
    return (struct.pack(">HHH", 0, 0, len(body)) + body).hex()


def regs_to_data(regs):
    return b"".join(struct.pack(">H", r & 0xFFFF) for r in regs)


def now():
    return datetime(2026, 11, 2, 16, 20, tzinfo=CST)


class TestConcentration(unittest.TestCase):
    """站号 1 / FC04：raw → mA → 浓度。"""

    def test_real_reading(self):
        # 现场实测 raw=12333 → 12.333 mA → 未补偿 52.081 → 加性 +0.3 ⇒ 52.381
        fr = mod.parse_payload(tcp_frame(1, 4, regs_to_data([12333])), now())
        self.assertTrue(fr.ok, fr.note)
        self.assertEqual(fr.station, 1)
        self.assertEqual(fr.values["raw"], 12333)
        self.assertAlmostEqual(fr.values["mA"], 12.333, places=3)
        self.assertAlmostEqual(fr.values["conc"], 52.381, places=3)
        self.assertEqual(fr.values["status"], "正常")

    def test_wire_break_low(self):
        # raw < 3600（<3.6mA）= 回路断线，不是"浓度 0"
        fr = mod.parse_payload(tcp_frame(1, 4, regs_to_data([1200])), now())
        self.assertEqual(fr.values["status"], "断线/仪表故障")

    def test_over_range(self):
        fr = mod.parse_payload(tcp_frame(1, 4, regs_to_data([20501])), now())
        self.assertEqual(fr.values["status"], "超量程")

    def test_wrong_function_code(self):
        fr = mod.parse_payload(tcp_frame(1, 3, regs_to_data([12333])), now())
        self.assertFalse(fr.ok)
        self.assertIn("FC04", fr.note)


class TestFlowMeter(unittest.TestCase):
    """站号 8 / FC03：按寄存器个数自动选择手册里对应的块。"""

    # 现场实测（2026-11-02 截图）：0/1 流速 3319，2/3 瞬时 21116，4 百分比 6598，
    # 6/7 正向累积 5181，8/9 反向 0，12 报警 0，13 流向 1（正向）
    BLOCK_0_13 = [0, 3319, 0, 21116, 6598, 0, 0, 5181, 0, 0, 0, 0, 0, 1]
    BLOCK_0_9 = [0, 3319, 0, 21116, 6598, 0, 0, 5181, 0, 0]

    def test_block_0_13_frame_is_37b(self):
        fr = mod.parse_payload(tcp_frame(8, 3, regs_to_data(self.BLOCK_0_13)), now())
        self.assertTrue(fr.ok, fr.note)
        self.assertEqual(fr.length, 37)          # 6 + 1 + 1 + 1 + 28
        self.assertEqual(fr.values["block"], "0~13")
        self.assertAlmostEqual(fr.values["velocity_m_s"], 3.319, places=3)
        self.assertAlmostEqual(fr.values["flow"], 211.16, places=2)
        self.assertAlmostEqual(fr.values["flow_pct"], 65.98, places=2)
        self.assertEqual(fr.values["total_fwd"], 5181)
        self.assertEqual(fr.values["direction"], "正向")
        self.assertEqual(fr.values["alarms"], [])

    def test_block_0_9_frame_is_29b(self):
        fr = mod.parse_payload(tcp_frame(8, 3, regs_to_data(self.BLOCK_0_9)), now())
        self.assertTrue(fr.ok, fr.note)
        self.assertEqual(fr.length, 29)
        self.assertEqual(fr.values["block"], "0~9")
        self.assertEqual(fr.values["total_fwd_low16"], 5181)
        self.assertIn("回绕", fr.values["warn"])

    def test_block_90_107_float(self):
        # 手册 C.6 示例：正向累积 1.51243 m³、流量 35、流速、百分比...
        def as_regs(v):
            return list(struct.unpack(">HH", struct.pack(">f", v)))

        regs: list[int] = []
        for v in (1.51243, 0.0, 1.51243):         # 90 正累积 / 92 反累积 / 94 总累积（各 2 个寄存器）
            regs += as_regs(v)
        regs += [0, 0]                            # 96/97 累积清零（只读不写）
        regs += as_regs(35.0)                     # 98 流量
        regs += as_regs(3.319)                    # 100 流速
        regs += as_regs(65.98)                    # 102 流量百分比
        regs += [0]                               # 104 空管百分比
        regs += [3]                               # 105 流量单位 3 = m3/h
        regs += [0, 0]                            # 106 空管报警 / 107 励磁报警
        self.assertEqual(len(regs), 18)
        fr = mod.parse_payload(tcp_frame(8, 3, regs_to_data(regs)), now())
        self.assertTrue(fr.ok, fr.note)
        self.assertEqual(fr.values["block"], "90~107")
        self.assertAlmostEqual(fr.values["total_fwd_f"], 1.512, places=3)
        self.assertAlmostEqual(fr.values["flow_f"], 35.0, places=3)
        self.assertEqual(fr.values["flow_unit"], "m3/h")   # 105 = 3

    def test_alarm_bits(self):
        blk = list(self.BLOCK_0_13)
        blk[12] = 0b0101        # bit1 上限 + bit3 空管
        fr = mod.parse_payload(tcp_frame(8, 3, regs_to_data(blk)), now())
        self.assertEqual(fr.values["alarms"], ["上限报警", "空管报警"])

    def test_total_is_32bit_no_wrap(self):
        # 累积超过 65535 必须靠高字接住（320 m³/h 时 16 位约 8.5 天回绕）
        blk = list(self.BLOCK_0_13)
        blk[6], blk[7] = 1, 0        # 高字 1 → 65536
        fr = mod.parse_payload(tcp_frame(8, 3, regs_to_data(blk)), now())
        self.assertEqual(fr.values["total_fwd"], 65536)


class TestBadFrames(unittest.TestCase):

    def test_exception_response(self):
        # 异常应答没有字节数域：MBAP + 站号 + 0x83 + 错误码(0x03 = 非法数据值)
        payload = (struct.pack(">HHH", 0, 0, 3) + bytes([8, 0x83, 0x03])).hex()
        fr = mod.parse_payload(payload, now())
        self.assertFalse(fr.ok)
        self.assertIn("非法数据值", fr.note)

    def test_length_mismatch(self):
        good = tcp_frame(8, 3, regs_to_data(TestFlowMeter.BLOCK_0_13))
        fr = mod.parse_payload(good[:-6], now())      # 截掉尾巴
        self.assertFalse(fr.ok)
        self.assertIn("帧长不符", fr.note)

    def test_odd_hex(self):
        self.assertFalse(mod.parse_payload("abc", now()).ok)

    def test_unknown_station(self):
        fr = mod.parse_payload(tcp_frame(9, 3, regs_to_data([1, 2])), now())
        self.assertTrue(fr.ok)
        self.assertIn("未知站号", fr.note)


class TestJudgement(unittest.TestCase):
    """汇总判定：两站都在才算通过。"""

    def collector(self):
        return mod.Collector(SimpleNamespace(out=None, quiet=True))

    def feed(self, col, station, fc, regs):
        col.emit(mod.parse_payload(tcp_frame(station, fc, regs_to_data(regs)), now()))

    def test_pass_when_both_present(self):
        col = self.collector()
        for _ in range(3):
            self.feed(col, 1, 4, [12333])
            self.feed(col, 8, 3, TestFlowMeter.BLOCK_0_13)
        ok, _ = col.summary(300)
        self.assertTrue(ok)

    def test_fail_when_flow_missing(self):
        col = self.collector()
        for _ in range(3):
            self.feed(col, 1, 4, [12333])
        ok, lines = col.summary(300)
        self.assertFalse(ok)
        self.assertTrue(any("没有流量帧" in l for l in lines))

    def test_fail_when_nothing(self):
        ok, lines = self.collector().summary(60)
        self.assertFalse(ok)
        self.assertTrue(any("一帧都没收到" in l for l in lines))


if __name__ == "__main__":
    unittest.main(verbosity=2)
