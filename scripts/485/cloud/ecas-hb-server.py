#!/usr/bin/env python3
"""云侧心跳接收（2026-11-10）：本地服务器每 60s POST 一次健康心跳。

只监听叠加网地址（10.10.10.123），只接受 10.10.10.0/24 来源；落盘 heartbeat.json
（含服务器收到的时刻 recv_ts，避免依赖本地时钟）供 ecas-gateway-flip.sh 判活。
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

BIND = os.environ.get("HB_BIND", "10.10.10.123")
PORT = int(os.environ.get("HB_PORT", "18889"))
DIR = os.environ.get("HB_DIR", "/var/lib/ecas/fallback")
STATE = os.path.join(DIR, "heartbeat.json")
LOG = "/var/log/ecas/heartbeat.jsonl"


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
    server_version = "ecas-hb/1"
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
        self.send_response(404); self.end_headers()

    def do_POST(self):
        src = self.client_address[0]
        if not (src.startswith("10.10.10.") or src.startswith("127.")):
            self.send_response(403); self.end_headers(); return
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 8192:
            self.send_response(400); self.end_headers(); return
        try:
            rec = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            self.send_response(400); self.end_headers(); return
        rec["recv_ts"] = time.time()
        rec["src"] = src
        write(rec)
        self._ok({"ok": True})


if __name__ == "__main__":
    os.makedirs(DIR, exist_ok=True)
    HTTPServer((BIND, PORT), H).serve_forever()
