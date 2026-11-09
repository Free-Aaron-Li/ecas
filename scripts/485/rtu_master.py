#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ECAS RS485 总站 —— DTU「TCP 透传」模式下的 Modbus RTU 主站

背景
----
DTU 工作在「TCP/UDP 透传」且「数据转换 Modbus TCP = 关闭」时，服务器就是
总线上的**唯一主站**：自己组请求帧、自己等应答、自己校验 CRC；DTU 只当一根
透明的网线。这样换来的是 1 秒级的采集频率（DTU 内部轮询受上报周期限制，
实测 200 s 一帧）。

两台从站
--------
站 8  FMC400 电磁流量计  FC03 读 0x0000 起 14 个寄存器（0~13）
站 1  浓度计 AI 模块     FC04 读 0x0000 起 1 个寄存器（输入寄存器，4~20 mA 映射值）

用法
----
  python3 rtu_master.py --listen 8901 --interval 1.0 --duration 0
  python3 rtu_master.py --listen 8901 --scan 1-32
  python3 rtu_master.py --listen 8901 --interval 0.5 --out /root/ecas/capture/x.jsonl

--scan 是诊断模式：逐个从站地址试 FC03/FC04 各读 1 个寄存器，谁应答就把谁
列出来（浓度计一直不应答时用它确认地址/波特率是否对）。

退出码：0 正常结束 / 2 端口被占用 / 3 参数错误
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- Modbus RTU

def crc16(data: bytes) -> bytes:
    """Modbus RTU CRC16（多项式 0xA001），返回低字节在前。"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def build_request(slave: int, fc: int, start: int, count: int) -> bytes:
    body = bytes((slave, fc)) + start.to_bytes(2, "big") + count.to_bytes(2, "big")
    return body + crc16(body)


def expected_len(count: int) -> int:
    """正常应答总长：站+FC+字节数+(2*count)+CRC2"""
    return 3 + 2 * count + 2


def u32(hi: int, lo: int) -> int:
    return (hi << 16) | lo


def s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


# ---------------------------------------------------------------- TCP 链路

class Link:
    """一条 DTU 透传 TCP 连接 + 重组缓冲。

    透传模式下 DTU 会先发注册包（ASCII 序列号）、每 120 s 发一个心跳 'Q'，
    这些都不是 Modbus 帧，必须当垃圾丢掉——_extract 就是干这个的。
    """

    def __init__(self, sock: socket.socket, remote: str):
        self.sock = sock
        self.remote = remote
        self.buf = bytearray()
        self.junk = bytearray()      # 非 Modbus 字节（注册包 / 心跳）
        self.junk_bytes = 0

    # -- 收 ---------------------------------------------------------------
    def _pump(self, timeout: float) -> bool:
        self.sock.settimeout(max(timeout, 0.001))
        try:
            chunk = self.sock.recv(4096)
        except socket.timeout:
            return False
        if not chunk:
            raise ConnectionError("DTU 关闭了连接")
        self.buf += chunk
        return True

    def _extract(self, slave: int, fc: int, count: int):
        """在缓冲里找一条合法应答。返回 (regs, err_code, consumed)。"""
        need = expected_len(count)
        i = 0
        n = len(self.buf)
        while i + 4 <= n:
            if self.buf[i] != slave:
                i += 1
                continue
            rfc = self.buf[i + 1]
            # 异常应答：FC|0x80, 字节数=1, 异常码, CRC2
            if rfc == (fc | 0x80) and i + 4 < n:
                if crc16(bytes(self.buf[i:i + 3])) == bytes(self.buf[i + 3:i + 5]):
                    err = self.buf[i + 2]
                    return None, err, i + 5
                i += 1
                continue
            if rfc != fc:
                i += 1
                continue
            if self.buf[i + 2] != 2 * count:
                i += 1
                continue
            if i + need > n:
                break                      # 还没收全，等下一批字节
            frame = bytes(self.buf[i:i + need])
            if crc16(frame[:-2]) == frame[-2:]:
                regs = [int.from_bytes(frame[3 + 2 * k:5 + 2 * k], "big") for k in range(count)]
                return regs, None, i + need
            i += 1
        # 没有整帧可用：把已确认无望的前导垃圾丢掉（保留末尾可能跨包的部分）
        keep = min(len(self.buf), need + 2)
        if len(self.buf) > keep:
            drop = len(self.buf) - keep
            self.junk += self.buf[:drop]
            self.junk_bytes += drop
            del self.buf[:drop]
        return None, None, 0

    # -- 一问一答 ---------------------------------------------------------
    def ask(self, slave: int, fc: int, start: int, count: int, timeout: float):
        """发一条请求，返回 (regs, err, rt_ms)。超时返回 (None, 'timeout', rt_ms)。"""
        req = build_request(slave, fc, start, count)
        t0 = time.monotonic()
        self.sock.sendall(req)
        deadline = t0 + timeout
        while True:
            regs, err, consumed = self._extract(slave, fc, count)
            if regs is not None or err is not None:
                del self.buf[:consumed]
                return regs, err, (time.monotonic() - t0) * 1000.0
            left = deadline - time.monotonic()
            if left <= 0:
                return None, "timeout", (time.monotonic() - t0) * 1000.0
            self._pump(min(left, 0.05))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- 解码

def decode_flow(regs: list[int]) -> dict:
    """FMC400 寄存器块（0 起 14 个）。"""
    d = {
        "velocity_mps": s16(regs[0]) * 2 ** 16 + regs[1],
        "flow_raw": s16(regs[2]) * 2 ** 16 + regs[3],
        "percent_raw": regs[4],
        "empty_percent_raw": regs[5],
        "total_raw": u32(regs[6], regs[7]),
        "total_reverse_raw": u32(regs[8], regs[9]) if len(regs) > 9 else None,
        "alarm": regs[12] if len(regs) > 12 else None,
        "direction": regs[13] if len(regs) > 13 else None,
    }
    d["velocity_mps"] = d["velocity_mps"] / 1000.0
    d["flow_m3h"] = d["flow_raw"] / 100.0
    d["percent"] = d["percent_raw"] / 100.0
    return d


# ---------------------------------------------------------------- 采集主流程

JOBS = [
    # (从站, 功能码, 起始地址, 寄存器数, 名称)
    (8, 3, 0, 14, "flow"),
    (1, 4, 0, 1, "conc"),
]


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="milliseconds")


WRITE_LOCK = threading.Lock()


def emit(out, rec, verbose=True):
    line = json.dumps(rec, ensure_ascii=False)
    with WRITE_LOCK:
        out.write(line + "\n")
        out.flush()
    if verbose:
        print(line, flush=True)


def run_collect(link: Link, args, out, stop=None) -> dict:
    stats = {name: {"ok": 0, "fail": 0, "rts": []} for *_x, name in JOBS}
    cycle = 0
    t_start = time.monotonic()
    next_tick = t_start
    dead_cycles = 0
    while args.duration <= 0 or (time.monotonic() - t_start) < args.duration:
        if stop is not None and stop():
            break
        cycle += 1
        cycle_ok = 0
        for slave, fc, start, count, name in JOBS:
            regs, err, rt = link.ask(slave, fc, start, count, args.timeout)
            st = stats[name]
            rec = {
                "ts": now_iso(), "cycle": cycle, "station": name, "slave": slave,
                "fc": fc, "ok": regs is not None, "rt_ms": round(rt, 1),
            }
            if regs is not None:
                st["ok"] += 1
                cycle_ok += 1
                st["rts"].append(rt)
                rec["regs"] = regs
                rec["hex"] = " ".join(f"{r:04x}" for r in regs)
                if name == "flow":
                    rec.update(decode_flow(regs))
                    rec["total"] = rec["total_raw"]
                else:
                    rec["raw"] = regs[0]
            else:
                st["fail"] += 1
                rec["err"] = err
            emit(out, rec, verbose=args.verbose)
        # 僵死判定：整轮一个应答都没有，连续 N 轮说明这条 TCP 已废（DTU 换 4G IP 重连、
        # 或对端卡死），主动断开让 DTU 重连——否则会一直抱着一条死线
        dead_cycles = 0 if cycle_ok else dead_cycles + 1
        if dead_cycles >= args.stall:
            raise ConnectionError(f"连续 {dead_cycles} 轮无任何应答，判定链路僵死")
        # 节奏：以上一轮起点 + interval 为下一个 tick（不累积漂移）
        next_tick += args.interval
        sleep = next_tick - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
        else:
            next_tick = time.monotonic()
    return stats


def summarize(stats: dict, elapsed: float) -> dict:
    print("\n" + "=" * 62)
    print(f"采集摘要（持续 {elapsed:.1f} s）")
    print("=" * 62)
    out = {}
    for name, st in stats.items():
        total = st["ok"] + st["fail"]
        rate = (st["ok"] / total * 100.0) if total else 0.0
        avg_rt = (sum(st["rts"]) / len(st["rts"])) if st["rts"] else 0.0
        hz = st["ok"] / elapsed if elapsed > 0 else 0.0
        print(f"{name:5s} 成功 {st['ok']:4d} / 失败 {st['fail']:4d}  "
              f"成功率 {rate:5.1f}%  平均往返 {avg_rt:6.1f} ms  有效帧率 {hz:.2f} Hz")
        out[name] = {"ok": st["ok"], "fail": st["fail"], "rate": round(rate, 1),
                     "avg_rt_ms": round(avg_rt, 1), "hz": round(hz, 3)}
    print("=" * 62 + "\n")
    return out


class Poller(threading.Thread):
    """一条连接的轮询线程。DTU 重连时主线程会 stop() 掉它，避免两条线抢总线。"""

    def __init__(self, link: "Link", args, out):
        super().__init__(daemon=True)
        self.link = link
        self.args = args
        self.out = out
        self.stop_flag = threading.Event()
        self.t_start = time.monotonic()

    def stop(self):
        self.stop_flag.set()

    def run(self):
        try:
            stats = run_collect(self.link, self.args, self.out, stop=self.stop_flag.is_set)
            summarize(stats, time.monotonic() - self.t_start)
        except (ConnectionError, OSError) as exc:
            print(f"[{now_iso()}] 连接结束：{exc}", flush=True)
        except Exception as exc:                       # 线程绝不能无声死掉
            print(f"[{now_iso()}] 轮询线程异常：{type(exc).__name__}: {exc}", flush=True)
        finally:
            self.link.close()


def serve(args) -> int:
    out = open(args.out, "a", encoding="utf-8") if args.out else sys.stdout
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((args.bind, args.listen))
    except OSError as exc:
        print(f"[fatal] 绑定 {args.bind}:{args.listen} 失败：{exc}", file=sys.stderr)
        return 2
    srv.listen(8)                  # 4G 换 IP 时 DTU 会连开好几条，backlog 要够
    print(f"[{now_iso()}] 监听 {args.bind}:{args.listen}，等待 DTU 透传连接……", flush=True)

    current = None
    while True:
        conn, peer = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        remote = f"{peer[0]}:{peer[1]}"
        print(f"[{now_iso()}] DTU 已连接 {remote}（切换为当前主连接）", flush=True)
        if current is not None:                    # 旧连接主动停掉，绝不让两条线同时问总线
            current.stop()
            current.join(timeout=2.0)
        link = Link(conn, remote)
        # DTU 刚连上时要先发注册包、再进入数据态；这期间发给它的字节它不读
        # （实测：立刻发请求 → DTU 只 ACK 不收数据，之后一直不应答）
        time.sleep(args.startup)
        try:
            link._pump(0.3)          # 吞掉注册包 8BZVA6PHA2GEFSVM
        except socket.timeout:
            pass
        except (ConnectionError, OSError) as exc:
            print(f"[{now_iso()}] 新连接握手失败：{exc}", flush=True)
            link.close()
            continue
        current = Poller(link, args, out)
        current.start()


# ---------------------------------------------------------------- 扫描诊断

def scan(args) -> int:
    lo, _, hi = args.scan.partition("-")
    lo = int(lo)
    hi = int(hi) if hi else lo
    out = open(args.out, "a", encoding="utf-8") if args.out else sys.stdout
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.listen))
    srv.listen(4)
    print(f"[{now_iso()}] 扫描模式：监听 {args.bind}:{args.listen}，等 DTU 连接……", flush=True)
    conn, peer = srv.accept()
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    link = Link(conn, f"{peer[0]}:{peer[1]}")
    print(f"[{now_iso()}] DTU 已连接 {link.remote}，开始扫描站号 {lo}~{hi}", flush=True)
    hits = []
    tick = time.monotonic()
    link._pump(0.5)          # 吃掉注册包
    combos = [(4, 1), (3, 1), (4, 2), (3, 2)]     # 浓度计可能用 FC03 或读 2 个寄存器
    for slave in range(lo, hi + 1):
        for fc, cnt in combos:
            regs, err, rt = link.ask(slave, fc, 0, cnt, args.timeout)
            rec = {"ts": now_iso(), "scan_slave": slave, "fc": fc, "count": cnt,
                   "ok": regs is not None, "err": err, "rt_ms": round(rt, 1)}
            if regs is not None:
                rec["regs"] = regs
                hits.append(rec)
                print(json.dumps(rec, ensure_ascii=False), flush=True)
            emit(out, rec, verbose=args.verbose)
    print("\n" + "=" * 62)
    print(f"扫描完成（{time.monotonic() - tick:.1f} s）：应答从站 {len(hits)} 个")
    for h in hits:
        print(f"  站 {h['scan_slave']:3d}  FC{h['fc']}  读{h.get('count',1)}  regs={h['regs']}")
    print("=" * 62 + "\n")
    link.close()
    return 0


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ECAS RS485 总站（DTU 透传模式）")
    ap.add_argument("--bind", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    ap.add_argument("--listen", type=int, default=8901, help="监听端口（默认 8901）")
    ap.add_argument("--interval", type=float, default=1.0, help="整轮周期，秒（默认 1.0）")
    ap.add_argument("--timeout", type=float, default=0.5, help="单条应答超时，秒（默认 0.5）")
    ap.add_argument("--duration", type=float, default=0, help="运行时长秒，0=直到断开/中断")
    ap.add_argument("--out", default="", help="JSONL 落盘路径（默认只打印）")
    ap.add_argument("--startup", type=float, default=0.6,
                    help="连接建立后等 DTU 就绪的秒数（实测太早发数据 DTU 不读 socket）")
    ap.add_argument("--stall", type=int, default=15,
                    help="连续多少轮无应答就判定链路僵死并断开重连（默认 15）")
    ap.add_argument("--scan", default="", help="扫描模式：站号范围，如 1-32")
    ap.add_argument("--quiet", action="store_true", help="不打印每条帧")
    args = ap.parse_args(argv)
    args.verbose = not args.quiet
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    if args.scan:
        return scan(args)
    try:
        return serve(args)
    except KeyboardInterrupt:
        print("\n[中断] 用户停止", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
