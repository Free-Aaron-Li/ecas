#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ECAS 现场采集抓包 / 双设备解码 / 接入判定   （先落盘，不入库）

用途
----
把塔石 DTU 发到 MQTT 主题的**原始帧**抓下来，按站号拆成两路（浓度计 / 电磁流量计），
解出工程量，并把每一帧原样写进 JSONL —— 硬件接入阶段先"抓到、看对不对"，
库表口径定了之后再决定怎么入库（本工具不写数据库）。

链路
----
  浓度计 ─4~20mA→ 中盛AI模块(站号1, FC04) ─┐
                                            ├─485总线─ 塔石DTU ─4G─ MQTT ─▶ 本工具
  电磁流量计(站号8, FC03) ──485总线──────────┘

帧格式（DTU「数据转换=Modbus TCP」：剥掉 RTU 的 CRC，前面补 6 字节 MBAP）
----
  浓度计 11 B : 00 00 | 00 00 | 00 05 | 01 | 04 | 02 | hi lo
  流量计 29~45B: 00 00 | 00 00 | 00 LL | 08 | 03 | NN | <NN 个字节寄存器数据>

用法
----
  1) 服务器上直接跑（口令由 systemd-creds 解密后经环境变量传入，见 capture-run.sh）：
        sudo ./capture-run.sh --duration 300
  2) 手工管道 / 回放：
        mosquitto_sub -h 127.0.0.1 -t dpro66 -F '%I|%x' | ./ecas_capture.py --pipe --duration 120
        ./ecas_capture.py --replay /root/ecas/capture/20261102-1610.jsonl
  3) 只解码一行 hex（现场贴在群里问）：
        ./ecas_capture.py --hex 00000000001708031400000cf7000...

退出码：0=判定通过  3=判定未通过（缺站/缺帧/读数异常）  2=参数或连接错误
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

SLAVE_CONC = 1          # 中盛 AI 模块
SLAVE_FLOW = 8          # 电磁流量计
MODEL_CONC = "DPRO-66"
MODEL_FLOW = "FMC400"

# 浓度换算（与 dpro66_monitor.sh 同口径；仅用于屏幕显示，原始值照样落盘）
CONC_SPAN_RAW = 16000.0     # (20mA-4mA)*1000
CONC_OFFSET = 0.3           # 09-26 先生定的加性偏移（+0.3 个浓度点）

ALARM_BITS = {1: "上限报警", 2: "下限报警", 3: "空管报警", 4: "励磁报警"}
FLOW_UNIT = {0: "L/h", 1: "L/m", 2: "L/s", 3: "m3/h", 4: "m3/m", 5: "m3/s",
             6: "kg/h", 7: "kg/m", 8: "kg/s", 9: "t/h", 10: "t/m", 11: "t/s"}
MODBUS_ERR = {0x01: "非法功能码", 0x02: "非法数据地址", 0x03: "非法数据值", 0x04: "从站故障"}


# --------------------------------------------------------------------------- 解码
def f32(hi: int, lo: int) -> float:
    """两个 16 位寄存器 → IEEE754 单精度浮点（寄存器内高位在前）。"""
    return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]


def u32(hi: int, lo: int) -> int:
    """两个 16 位寄存器 → 32 位无符号长整型（高字在前）。"""
    return (hi << 16) | lo


@dataclass
class Frame:
    """一帧 DTU 上行数据的解码结果。"""
    ts: datetime
    station: int
    fc: int
    hex: str
    length: int
    ok: bool
    note: str = ""
    values: dict = field(default_factory=dict)


def _crc16_rtu(data: bytes) -> int:
    """Modbus RTU 的 CRC16（多项式 0xA001，帧内低字节在前）。"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def split_stream(buf: bytearray) -> list[tuple[str, bytes]]:
    """从字节流里尽量切出帧；未成帧的残片留在 buf 里等后续消息。

    DTU 的「数据转换 = Modbus TCP」打开时，上行是一条一条干净的 TCP 应答帧；
    关掉（透传）时会把 485 上的**请求帧 + 应答帧（RTU 原始格式）**一起吐出来，
    还会把一个帧拆进多条 MQTT 消息（实测 2026-11-02 16:44 起就是这个状态）。
    这里统一做流式重组，两种模式都能解。

    返回 [(kind, frame)]，kind ∈ heartbeat / req_tcp / req_rtu / tcp / rtu / resync。
    """
    out: list[tuple[str, bytes]] = []
    while buf:
        # 心跳：DTU 定时发单字节 ASCII 'Q'（0x51）；'H' 兼容老固件
        if len(buf) == 1 and buf[0] in b"QqHh":
            out.append(("heartbeat", bytes(buf)))
            buf.clear()
            break
        # ① Modbus TCP（MBAP 头 6 B，PID 必须 0）
        if len(buf) >= 8:
            _tid, pid, mlen = struct.unpack(">HHH", bytes(buf[:6]))
            if pid == 0 and 2 <= mlen <= 250:
                station, fc = buf[6], buf[7]
                if 1 <= station <= 247 and (fc in (3, 4) or fc in (0x83, 0x84)):
                    total = 6 + mlen
                    if len(buf) < total:
                        break                      # 分片未到齐，等下一批字节
                    frame = bytes(buf[:total])
                    del buf[:total]
                    out.append(("req_tcp" if (mlen == 6 and not fc & 0x80) else "tcp", frame))
                    continue
        # ② Modbus RTU 应答（站 + FC + 字节数 + 数据 + CRC16）
        if len(buf) >= 5 and buf[1] in (3, 4) and 1 <= buf[0] <= 247:
            total = 3 + buf[2] + 2
            if 0 < buf[2] <= 250 and len(buf) >= total:
                if _crc16_rtu(bytes(buf[:total - 2])) == struct.unpack("<H", bytes(buf[total - 2:total]))[0]:
                    frame = bytes(buf[:total])
                    del buf[:total]
                    out.append(("rtu", frame))
                    continue
        # ③ Modbus RTU 请求（站 + FC + 地址 2 + 数量 2 + CRC16 = 8 B）
        if len(buf) >= 8 and buf[1] in (3, 4) and 1 <= buf[0] <= 247:
            if _crc16_rtu(bytes(buf[:6])) == struct.unpack("<H", bytes(buf[6:8]))[0]:
                frame = bytes(buf[:8])
                del buf[:8]
                out.append(("req_rtu", frame))
                continue
        # ④ 认不出 ⇒ 丢 1 字节重同步（透传模式下偶发半包/噪声）
        del buf[:1]
        out.append(("resync", b""))
    return out


def _decode_regs(station: int, fc: int, regs: list[int], ts: datetime,
                 hexs: str, length: int, source: str) -> Frame:
    """站号 + 功能码 + 寄存器列表 → 工程量（TCP / RTU 两种来源共用）。"""
    if station == SLAVE_CONC:
        if fc != 4:
            return Frame(ts, station, fc, hexs, length, False,
                         f"浓度计应为 FC04，实际 FC{fc:02d}（{source}）")
        if len(regs) != 1:
            return Frame(ts, station, fc, hexs, length, False,
                         f"浓度帧应有 1 个寄存器，实际 {len(regs)} 个（提示：DTU 轮询串被改成了多寄存器？）")
        rawv = regs[0]
        ma = rawv / 1000.0
        conc_unc = (rawv - 4000) / (CONC_SPAN_RAW / 100.0)  # 16000 raw ÷ 100% ⇒ 160 raw/个浓度点
        return Frame(ts, station, fc, hexs, length, True, values={
            "model": MODEL_CONC, "raw": rawv, "mA": round(ma, 3),
            "conc_raw": round(conc_unc, 3), "conc": round(conc_unc + CONC_OFFSET, 3),
            "status": ("断线/仪表故障" if rawv < 3600 else
                       "超量程" if rawv > 20500 else "正常"),
        })

    if station == SLAVE_FLOW:
        if fc != 3:
            return Frame(ts, station, fc, hexs, length, False,
                         f"流量计应为 FC03，实际 FC{fc:02d}（{source}）")
        return Frame(ts, station, fc, hexs, length, True,
                     note=decode_flow_note(len(regs)), values=decode_flow(regs))

    return Frame(ts, station, fc, hexs, length, True,
                 note=f"未知站号 {station}（浓度=1 / 流量=8，{source}）", values={"regs": regs})


def parse_tcp(frame: bytes, ts: datetime, source: str = "TCP") -> Frame:
    """解析一条完整的 Modbus TCP 帧（已含 MBAP 头）。"""
    hexs = frame.hex()
    station, fc = frame[6], frame[7]
    if fc & 0x80:
        code = frame[8] if len(frame) > 8 else 0
        return Frame(ts, station, fc, hexs, len(frame), False,
                     f"从站异常应答：{MODBUS_ERR.get(code, f'未知错误 0x{code:02X}')}")
    nbytes = frame[8]
    data = frame[9:9 + nbytes]
    if len(data) != nbytes:
        return Frame(ts, station, fc, hexs, len(frame), False,
                     f"数据域声明 {nbytes} B，实际 {len(data)} B")
    regs = [struct.unpack(">H", data[i:i + 2])[0] for i in range(0, len(data) - len(data) % 2, 2)]
    return _decode_regs(station, fc, regs, ts, hexs, len(frame), source)


def parse_rtu(frame: bytes, ts: datetime, source: str = "RTU") -> Frame:
    """解析一条完整的 Modbus RTU 应答帧（尾部 2 B CRC 已校验，解完不含 CRC）。"""
    hexs = frame.hex()
    station, fc = frame[0], frame[1]
    if fc & 0x80:
        return Frame(ts, station, fc, hexs, len(frame), False,
                     f"从站异常应答：{MODBUS_ERR.get(frame[2], f'未知错误 0x{frame[2]:02X}')}")
    data = frame[3:3 + frame[2]]
    regs = [struct.unpack(">H", data[i:i + 2])[0] for i in range(0, len(data) - len(data) % 2, 2)]
    return _decode_regs(station, fc, regs, ts, hexs, len(frame), source)


def parse_payload(payload_hex: str, ts: datetime) -> Frame:
    """解码一段 hex（--hex / 回放单条用）：内部按流式切帧，取第一条整帧。"""
    payload_hex = payload_hex.strip().lower()
    if len(payload_hex) % 2 or not payload_hex:
        return Frame(ts, -1, -1, payload_hex, 0, False, "帧长为奇数或为空")
    raw = bytes.fromhex(payload_hex)
    if len(raw) == 1 and raw[0] in b"QqHh":
        return Frame(ts, -1, -1, payload_hex, 1, True, note="心跳",
                     values={"heartbeat": raw.decode()})
    buf = bytearray(raw)
    skipped = 0
    for kind, frame in split_stream(buf):
        if kind == "heartbeat" or kind == "resync":
            skipped += 1
            continue
        if kind in ("req_tcp", "req_rtu"):
            return Frame(ts, frame[1] if kind == "req_rtu" else frame[6],
                         frame[1] if kind == "req_rtu" else frame[7],
                         frame.hex(), len(frame), True,
                         note=f"{'RTU' if kind == 'req_rtu' else 'TCP'} 请求帧（透传模式的副产品，非设备应答）",
                         values={"request": True})
        return parse_tcp(frame, ts) if kind == "tcp" else parse_rtu(frame, ts)
    return Frame(ts, -1, -1, payload_hex, len(raw), False,
                 f"切不出完整帧（{len(raw)} B，疑似跨消息分片的前半段）")


def decode_flow_note(nregs: int) -> str:
    """按应答寄存器个数反推 DTU 里配的是哪条轮询指令（帧长自识别）。"""
    return {
        10: "读到 0~9（29 B 帧）",
        14: "读到 0~13（37 B 帧，推荐）",
        18: "读到 90~107 浮点块（45 B 帧）",
        6: "读到 0~5 或不完整帧",
        3: "读到 21~23（管径/量程）",
        2: "读到 98~99（瞬时流量浮点）",
    }.get(nregs, f"读到 {nregs} 个寄存器")


def decode_flow(regs: list[int]) -> dict:
    """流量计寄存器 → 工程量。按寄存器个数自动选择手册对应块。"""
    n = len(regs)
    if n == 14:                      # 表 23：0~13
        u = u32(regs[0], regs[1]) / 1000.0
        q_raw = u32(regs[2], regs[3])
        fwd = u32(regs[6], regs[7])
        rev = u32(regs[8], regs[9])
        alarm = regs[12]
        return {
            "model": MODEL_FLOW, "block": "0~13",
            "velocity_m_s": round(u, 3),
            "flow_raw": q_raw, "flow": round(q_raw / 100.0, 2),
            "flow_pct": round(regs[4] / 100.0, 2),
            "empty_pipe_pct": round(regs[5] / 100.0, 2),
            "total_fwd": fwd, "total_rev": rev,
            "alarm_bits": alarm,
            "alarms": [n2 for b, n2 in ALARM_BITS.items() if alarm & (1 << (b - 1))],
            "direction": {0: "反向", 1: "正向"}.get(regs[13], str(regs[13])),
        }
    if n == 10:                      # 0~9：累积只有低 16 位，只能看趋势
        q_raw = u32(regs[2], regs[3])
        return {
            "model": MODEL_FLOW, "block": "0~9",
            "velocity_m_s": round(u32(regs[0], regs[1]) / 1000.0, 3),
            "flow_raw": q_raw, "flow": round(q_raw / 100.0, 2),
            "flow_pct": round(regs[4] / 100.0, 2),
            "total_fwd_low16": regs[7],
            "warn": "累积只读到低 16 位（满量程下约 8.5 天回绕）",
        }
    if n == 18:                      # 90~107 浮点块
        return {
            "model": MODEL_FLOW, "block": "90~107",
            "total_fwd_f": round(f32(regs[0], regs[1]), 3),
            "total_rev_f": round(f32(regs[2], regs[3]), 3),
            "total_all_f": round(f32(regs[4], regs[5]), 3),
            "flow_f": round(f32(regs[8], regs[9]), 2),
            "velocity_f": round(f32(regs[10], regs[11]), 3),
            "flow_pct_f": round(f32(regs[12], regs[13]), 2),
            "empty_pipe_pct": regs[14] / 100.0,
            "flow_unit": FLOW_UNIT.get(regs[15], str(regs[15])),
            "empty_alarm": regs[16], "excitation_alarm": regs[17],
        }
    if n == 6:                       # 90~95：正 / 反 / 总累积（浮点）
        return {
            "model": MODEL_FLOW, "block": "90~95",
            "total_fwd_f": round(f32(regs[0], regs[1]), 3),
            "total_rev_f": round(f32(regs[2], regs[3]), 3),
            "total_all_f": round(f32(regs[4], regs[5]), 3),
        }
    if n == 2:                       # 98~99：瞬时流量（浮点）
        return {"model": MODEL_FLOW, "block": "98~99", "flow_f": round(f32(regs[0], regs[1]), 2)}
    if n == 3:                       # 21~23：管径 + 量程高低字
        return {"model": MODEL_FLOW, "block": "21~23", "pipe_dn": regs[0],
                "range_raw": u32(regs[1], regs[2]), "range": round(u32(regs[1], regs[2]) / 100.0, 2)}
    return {"model": MODEL_FLOW, "regs": regs}


# --------------------------------------------------------------------------- 采集
class Collector:
    """订阅 → 解码 → 落盘 → 统计。"""

    def __init__(self, args):
        self.args = args
        self.frames: list[Frame] = []
        self.bad = 0
        self.buf = bytearray()      # 跨 MQTT 消息的粘包/分片重组缓冲
        self.heartbeats = 0         # DTU 心跳（单字节 'Q'）
        self.requests = 0           # 透传模式下混进来的请求帧
        self.resyncs = 0            # 认不出而丢掉的字节数
        self.out = open(args.out, "a", encoding="utf-8") if args.out else None
        self.t0 = time.monotonic()
        self.stop = False

    # --- 输出 -------------------------------------------------------------
    def emit(self, fr: Frame) -> None:
        self.frames.append(fr)
        if not fr.ok:
            self.bad += 1
        rec = {
            "ts": fr.ts.isoformat(),
            "station": fr.station,
            "fc": fr.fc,
            "frame_len": fr.length,
            "ok": fr.ok,
            "hex": fr.hex,
            "values": fr.values,
        }
        if fr.note:
            rec["note"] = fr.note
        if self.out:
            self.out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.out.flush()
        if not self.args.quiet:
            print(self.fmt(fr), flush=True)

    def fmt(self, fr: Frame) -> str:
        clock = fr.ts.strftime("%H:%M:%S")
        head = f"{clock} 站{fr.station:<2d} FC{fr.fc:02d} {fr.length:2d}B"
        if not fr.ok:
            return f"{head}  ❌ {fr.note}"
        v = fr.values
        if v.get("model") == MODEL_CONC:
            return (f"{head}  浓度 {v['conc']:7.3f} %  ({v['mA']:7.3f} mA, raw {v['raw']},"
                    f" {v['status']})")
        if v.get("model") == MODEL_FLOW:
            if v.get("block") == "90~107" and "flow_f" in v:
                return (f"{head}  流量 {v['flow_f']:8.2f} {v.get('flow_unit', '')}  流速 {v['velocity_f']:.3f} m/s"
                        f"  累积(总) {v['total_all_f']:.3f}")
            if "flow" not in v:
                return f"{head}  {v}  {fr.note}"
            fwd = v.get("total_fwd", v.get("total_fwd_low16", "?"))
            return (f"{head}  流量 {v['flow']:8.2f} m³/h  流速 {v.get('velocity_m_s', float('nan')):.3f} m/s"
                    f"  累积 {fwd}  报警 {v.get('alarms') or '无'}")
        return f"{head}  {v}  {fr.note}"

    # --- 统计 -------------------------------------------------------------
    def summary(self, duration: float) -> tuple[bool, list[str]]:
        lines: list[str] = []
        conc = [f for f in self.frames if f.ok and f.station == SLAVE_CONC]
        flow = [f for f in self.frames if f.ok and f.station == SLAVE_FLOW]
        lines.append("─" * 74)
        lines.append(f"时长 {duration:.0f}s ｜ 总帧 {len(self.frames)} ｜ 解码失败 {self.bad} ｜ "
                     f"浓度帧 {len(conc)} ｜ 流量帧 {len(flow)}")
        if self.heartbeats or self.requests or self.resyncs:
            lines.append(f"  旁路统计：心跳 {self.heartbeats} ｜ 透传请求帧 {self.requests} ｜ "
                         f"重同步丢字节 {self.resyncs}")
        for name, group in (("浓度计(站1)", conc), ("流量计(站8)", flow)):
            if not group:
                lines.append(f"  {name}: 0 帧")
                continue
            gaps = [(b.ts - a.ts).total_seconds() for a, b in zip(group, group[1:])]
            if gaps:
                gaps_sorted = sorted(gaps)
                mid = gaps_sorted[len(gaps_sorted) // 2]
                lines.append(f"  {name}: {len(group)} 帧，间隔 中位 {mid:.0f}s / "
                             f"最小 {min(gaps):.0f}s / 最大 {max(gaps):.0f}s")
        if conc:
            lines.append(f"  浓度最新：{conc[-1].values['conc']} %（{conc[-1].values['status']}）")
        if flow:
            lines.append(f"  流量最新：{flow[-1].values}")
        # 判定
        problems: list[str] = []
        if self.bad:
            problems.append(f"有 {self.bad} 帧解不出（看 ❌ 行/JSONL）")
        if not conc:
            problems.append("没有浓度帧 —— 浓度链路断了（本来在跑，先查 DTU 是否还在轮询槽 1）")
        if not flow:
            problems.append("没有流量帧 —— ①DTU 未下发 POLLSTR 槽 2 或未 AT&W/重启 "
                            "②站号不是 8 ③流量计 A/B(TX±) 接反或未接 ④波特率/校验位不一致")
        if flow and flow[-1].values.get("velocity_m_s") == 0 and flow[-1].values.get("flow") == 0:
            problems.append("流量读到 0 —— 可能空管/未满管，或读的是保留寄存器（确认配的是 0~13）")
        if len(self.frames) == 0:
            problems.append("一帧都没收到 —— 检查 mosquitto_sub 连通性、主题名、ACL 权限")
        ok = not problems
        lines.append("─" * 74)
        lines.append("✅ 判定：硬件采集通过（两站都在、帧可解）" if ok else "❌ 判定：未通过")
        for p in problems:
            lines.append(f"   · {p}")
        return ok, lines

    # --- 主循环 -----------------------------------------------------------
    def feed_line(self, line: str) -> None:
        """一行 = 一条 MQTT 消息；把 hex 追加进缓冲后按流量切帧。"""
        line = line.strip()
        if not line:
            return
        ts = datetime.now(CST)
        hex_part = ""
        if "|" in line:
            ts_part, _, hex_part = line.partition("|")
            try:
                ts = datetime.fromtimestamp(float(ts_part), CST)
            except ValueError:
                pass
        elif all(c in "0123456789abcdefABCDEF" for c in line) and len(line) % 2 == 0:
            hex_part = line
        else:
            print(f"⚠️ 忽略无法识别的行：{line[:60]}", file=sys.stderr)
            return
        try:
            self.buf += bytes.fromhex(hex_part)
        except ValueError:
            print(f"⚠️ 非法 hex：{hex_part[:60]}", file=sys.stderr)
            return
        for kind, frame in split_stream(self.buf):
            if kind == "heartbeat":
                self.heartbeats += 1
            elif kind in ("req_tcp", "req_rtu"):
                self.requests += 1
            elif kind == "resync":
                self.resyncs += 1
            elif kind == "tcp":
                self.emit(parse_tcp(frame, ts))
            else:
                self.emit(parse_rtu(frame, ts))


def build_subscriber(args) -> subprocess.Popen:
    """起 mosquitto_sub，把 payload 打印成「接收时刻|十六进制」。"""
    if not shutil.which("mosquitto_sub"):
        sys.exit("❌ 找不到 mosquitto_sub（apt install mosquitto-clients）")
    cmd = ["mosquitto_sub", "-h", args.host, "-p", str(args.port), "-t", args.topic, "-F", "%I|%x"]
    if args.user:
        cmd += ["-u", args.user]
    passwd = args.password or os.environ.get("MQTT_PASS") or ""
    if passwd:
        cmd += ["-P", passwd]          # 只经环境变量进 argv 一次，不打印
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)


def main() -> int:
    ap = argparse.ArgumentParser(description="ECAS 现场采集抓包/双设备解码/接入判定")
    ap.add_argument("--duration", type=float, default=0, help="采集时长（秒），0=一直跑")
    ap.add_argument("--out", help="JSONL 落盘路径（追加）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--topic", default="dpro66")
    ap.add_argument("--user", default=os.environ.get("MQTT_USER", ""))
    ap.add_argument("--password", default="", help="口令；更推荐用环境变量 MQTT_PASS（不进命令行）")
    ap.add_argument("--pipe", action="store_true", help="从 stdin 读「时刻|hex」或纯 hex")
    ap.add_argument("--replay", help="回放 JSONL 里抓过的 hex")
    ap.add_argument("--hex", dest="one_hex", help="只解码一帧 hex 然后退出")
    ap.add_argument("--quiet", action="store_true", help="不逐帧打印，只出汇总")
    args = ap.parse_args()

    if args.one_hex:
        fr = parse_payload(args.one_hex, datetime.now(CST))
        print(json.dumps({"ok": fr.ok, "station": fr.station, "fc": fr.fc,
                          "frame_len": fr.length, "note": fr.note, "values": fr.values},
                         ensure_ascii=False, indent=2))
        return 0 if fr.ok else 3

    col = Collector(args)

    if args.replay:
        with open(args.replay, encoding="utf-8") as fh:
            for ln in fh:
                try:
                    rec = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts") or ""
                if ts:
                    try:
                        stamp = datetime.fromisoformat(ts).timestamp()
                        ts = f"{stamp}"
                    except ValueError:
                        ts = ""
                col.feed_line(f"{ts}|{rec.get('hex', '')}" if ts else rec.get("hex", ""))
        ok, lines = col.summary(0)
        print("\n".join(lines))
        return 0 if ok else 3

    if args.pipe:
        try:
            for line in sys.stdin:
                if args.duration and time.monotonic() - col.t0 > args.duration:
                    break
                col.feed_line(line)
        except KeyboardInterrupt:
            pass
        ok, lines = col.summary(time.monotonic() - col.t0)
        print("\n".join(lines))
        return 0 if ok else 3

    proc = build_subscriber(args)
    signal.signal(signal.SIGINT, lambda *_: setattr(col, "stop", True))
    signal.signal(signal.SIGTERM, lambda *_: setattr(col, "stop", True))
    try:
        while not col.stop:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    err = proc.stderr.read().strip()
                    print(f"❌ mosquitto_sub 退出：{err or '无输出'}", file=sys.stderr)
                    break
                continue
            col.feed_line(line)
            if args.duration and time.monotonic() - col.t0 >= args.duration:
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        if col.out:
            col.out.close()

    ok, lines = col.summary(time.monotonic() - col.t0)
    print("\n".join(lines))
    if args.out:
        print(f"原始帧已落盘：{args.out}")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
