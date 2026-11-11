#!/usr/bin/env python3
"""云侧心跳接收（2026-11-10 初版；2026-11-11 修挂死）。

只监听叠加网地址（10.10.10.123），只接受 10.10.10.0/24 来源；落盘 heartbeat.json
（含服务器收到的时刻 recv_ts，避免依赖本地时钟）供 ecas-gateway-flip.sh 判活。

2026-11-11 故障：单线程 HTTPServer + 无 socket 超时 ⇒ 只要有一个客户端连上却不发完
body（本地脚本被 kill、网络半开），`rfile.read(n)` 就永久阻塞，整个接收端装死；
内核仍完成 TCP 握手（backlog），所以外部表现为"端口开着、HTTP 无响应"，本地 curl
拿到 000，心跳从 01:02 起 8 小时全丢，云端误判本地离线并切 capture。
修法：ThreadingHTTPServer + 每个连接 10s 超时 + 线程化，单个卡死连接不再拖垮全体。
"""
import json
import os
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = os.environ.get("HB_BIND", "10.10.10.123")
PORT = int(os.environ.get("HB_PORT", "18889"))
DIR = os.environ.get("HB_DIR", "/var/lib/ecas/fallback")
STATE = os.path.join(DIR, "heartbeat.json")
LOG = "/var/log/ecas/heartbeat.jsonl"
TIMEOUT = float(os.environ.get("HB_TIMEOUT", "10"))


def write(rec):
    os.makedirs(DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False)
    os.replace(tmp, STATE)
    line = json.dumps(rec, ensure_ascii=False)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class H(BaseHTTPRequestHandler):
    server_version = "ecas-hb/2"
    timeout = TIMEOUT

    def log_message(self, *a):
        pass

    def _ok(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/state"):
            try:
                with open(STATE, encoding="utf-8") as fh:
                    return self._ok(json.load(fh))
            except OSError:
                return self._ok({})
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        src = self.client_address[0]
        if not (src.startswith("10.10.10.") or src.startswith("127.")):
            self.send_response(403)
            self.end_headers()
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 8192:
            self.send_response(400)
            self.end_headers()
            return
        try:
            raw = self.rfile.read(n)
            rec = json.loads(raw.decode("utf-8"))
            if not isinstance(rec, dict):
                raise ValueError("not an object")
        except (socket.timeout, OSError, ValueError):
            self.send_response(400)
            self.end_headers()
            return
        rec["recv_ts"] = time.time()
        rec["src"] = src
        write(rec)
        self._ok({"ok": True})


if __name__ == "__main__":
    os.makedirs(DIR, exist_ok=True)
    srv = ThreadingHTTPServer((BIND, PORT), H)
    srv.daemon_threads = True
    srv.serve_forever()
