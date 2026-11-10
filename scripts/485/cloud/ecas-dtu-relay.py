#!/usr/bin/env python3
"""云侧 DTU 采集入口（2026-11-10）：平时纯透传，本地离线时接管并落 spool。

背景：DTU 4G 只做 TCP 透明传输，连云 124.222.122.146:8883；云侧 iptables DNAT 原本
直接转给本地 10.10.10.11:8883。本地整机宕机（如 2026-11-10 13:48）时没人收数据、
也没有任何缓冲，现场数据真丢。本进程改成 DNAT 的落点：

    正常：DTU ──▶ 本进程:18883 ──▶ 10.10.10.11:8883（本地 rtu-master，与现状等价）
    本地离线：DTU ──▶ 本进程:18883 ──▶ 127.0.0.1:18884（云侧采集器，只落 JSONL spool）

判定：每 PROBE_INTERVAL 秒 TCP 探测本地 8883；连续 FAIL_N 次失败 → 切采集；
连续 OK_N 次成功 → 切回透传。切换时断开现有上游连接，让 DTU 重连后按新目标落地。
状态变化写 /var/log/ecas/fallback-relay.jsonl。
RELAY_FORCE_CAPTURE=1 可强制采集（演练用，不需要动本地）。
"""
import asyncio
import json
import os
import sys
import time

LISTEN_HOST = os.environ.get("RELAY_LISTEN_HOST", "10.10.10.123")
LISTEN_PORT = int(os.environ.get("RELAY_LISTEN_PORT", "18883"))
LOCAL_HOST = os.environ.get("RELAY_LOCAL_HOST", "10.10.10.11")
LOCAL_PORT = int(os.environ.get("RELAY_LOCAL_PORT", "8883"))
CAP_HOST = os.environ.get("RELAY_CAP_HOST", "127.0.0.1")
CAP_PORT = int(os.environ.get("RELAY_CAP_PORT", "18884"))
PROBE_INTERVAL = float(os.environ.get("RELAY_PROBE_INTERVAL", "3"))
FAIL_N = int(os.environ.get("RELAY_FAIL_N", "3"))
OK_N = int(os.environ.get("RELAY_OK_N", "2"))
FORCE_CAPTURE = os.environ.get("RELAY_FORCE_CAPTURE", "0") == "1"
LOG = os.environ.get("RELAY_LOG", "/var/log/ecas/fallback-relay.jsonl")

state = "capture" if FORCE_CAPTURE else "live"
conns = set()


def log(event, **kw):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, "state": state}
    rec.update(kw)
    line = json.dumps(rec, ensure_ascii=False)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def target():
    return (CAP_HOST, CAP_PORT) if state == "capture" else (LOCAL_HOST, LOCAL_PORT)


async def pump(src, dst):
    try:
        while True:
            data = await src.read(4096)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


async def handle(reader, writer):
    peer = writer.get_extra_info("peername")
    host, port = target()
    try:
        ur, uw = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=4)
    except Exception as exc:
        log("upstream_fail", peer=str(peer), target="%s:%d" % (host, port),
            err=type(exc).__name__)
        writer.close()
        return
    pair = (writer, uw)
    conns.add(pair)
    log("conn", peer=str(peer), target="%s:%d" % (host, port))
    try:
        await asyncio.gather(pump(reader, uw), pump(ur, writer))
    finally:
        conns.discard(pair)
        for w in (writer, uw):
            try:
                w.close()
            except Exception:
                pass


async def drop_all():
    for a, b in list(conns):
        for w in (a, b):
            try:
                w.close()
            except Exception:
                pass


async def probe():
    global state
    fails = oks = 0
    while True:
        await asyncio.sleep(PROBE_INTERVAL)
        if FORCE_CAPTURE:
            continue
        ok = True
        try:
            _, w = await asyncio.wait_for(
                asyncio.open_connection(LOCAL_HOST, LOCAL_PORT), timeout=2.0)
            w.close()
        except Exception:
            ok = False
        if ok:
            fails, oks = 0, oks + 1
            if state == "capture" and oks >= OK_N:
                state = "live"
                log("switch", reason="local_back", probe_ok=oks)
                await drop_all()
        else:
            oks, fails = 0, fails + 1
            if state == "live" and fails >= FAIL_N:
                state = "capture"
                log("switch", reason="local_down", probe_fail=fails)
                await drop_all()


async def main():
    server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT, backlog=16)
    log("start", listen="%s:%d" % (LISTEN_HOST, LISTEN_PORT),
        local="%s:%d" % (LOCAL_HOST, LOCAL_PORT),
        capture="%s:%d" % (CAP_HOST, CAP_PORT), force_capture=FORCE_CAPTURE)
    async with server:
        await asyncio.gather(server.serve_forever(), probe())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
