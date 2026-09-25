# -*- coding: utf-8 -*-
"""ECAS 浓度计 —— 只读 HTTP 接口 + 内置看板（零第三方依赖，仅标准库 + psycopg2）

接口：
    GET /                 内置看板（浏览器直接看，30 秒自动刷新）
    GET /api/health       心跳：最后一条数据的时间、距今多少秒、是否断流
    GET /api/readings     最近 N 小时的数据（已换算：mA / 未补偿 / 浓度 / 状态）
    GET /api/summary      区间统计（条数、均值、极值、断线/超量程条数）
    GET /api/params       后端换算参数（自证口径）
    GET /healthz          纯文本 ok（给监控用）

⚠️ 只读：本服务不写数据库；凭据走 PGPASSFILE=/root/.pgpass，代码里没有口令。
"""
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2                      # noqa: E402
import psycopg2.extras               # noqa: E402
from config import DB, MODEL, LISTEN_HOST, LISTEN_PORT, STALE_SECONDS   # noqa: E402
import convert as cv                 # noqa: E402

os.environ.setdefault("PGPASSFILE", "/root/.pgpass")

MAX_LIMIT = 5000
MAX_HOURS = 24 * 30


# ─────────────────────────── 数据库 ───────────────────────────
def db():
    conn = psycopg2.connect(**DB)
    conn.set_session(readonly=True, autocommit=True)
    return conn


def query(sql, args=None, one=False):
    conn = db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args or ())
            rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows
    finally:
        conn.close()


def iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


# ─────────────────────────── 三个数据接口 ───────────────────────────
def api_health():
    row = query(
        "SELECT count(*) AS n,"
        "       max(ts) AS last_ts,"
        "       round(extract(epoch FROM now() - max(ts)))::bigint AS age_seconds,"
        "       (SELECT raw FROM raw_reading WHERE model = %s ORDER BY ts DESC LIMIT 1) AS last_raw"
        "  FROM raw_reading WHERE model = %s",
        (MODEL, MODEL), one=True)
    n = int(row["n"] or 0)
    age = row["age_seconds"]
    age = int(age) if age is not None else None
    last = None
    if row["last_raw"] is not None:
        last = cv.convert(row["last_raw"])
        last["ts"] = iso(row["last_ts"])
    return {
        "ok": True,
        "model": MODEL,
        "rows_total": n,
        "last_ts": iso(row["last_ts"]),
        "age_seconds": age,
        "stale": (age is None) or (age > STALE_SECONDS),
        "stale_after_seconds": STALE_SECONDS,
        "last": last,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


def api_readings(hours, limit, offset):
    rows = query(
        "SELECT ts, raw FROM raw_reading"
        " WHERE model = %s AND ts > now() - make_interval(secs => %s)"
        " ORDER BY ts DESC LIMIT %s OFFSET %s",
        (MODEL, hours * 3600, limit, offset))
    items = [cv.convert_row(r) for r in rows]
    for it in items:
        it["ts"] = iso(it["ts"])
    return {"model": MODEL, "hours": hours, "count": len(items),
            "offset": offset, "items": items}


def api_summary(hours):
    """区间统计。⭐ 平均值/极值只统计**有效区间**（4~20 mA 之内）的行 ——
    断线(raw=0) 会把均值拖到 -25，那不是浓度。"""
    span = f"(({cv.I_HI} - {cv.I_LO}) * 1000.0)"
    base = f"((raw - {cv.I_LO} * 1000) * ({cv.C_HI} - {cv.C_LO}) / {span} + {cv.C_LO})"
    conc = (f"{base} * (1 + {cv.OFFSET_PCT} / 100.0)" if cv.OFFSET_MODE == "mul"
            else f"{base} + {cv.OFFSET_CONC}")
    row = query(
        "WITH x AS (SELECT raw FROM raw_reading"
        "            WHERE model = %s AND ts > now() - make_interval(secs => %s)),"
        f"     v AS (SELECT raw FROM x WHERE raw >= {cv.I_LO} * 1000 AND raw <= {cv.OVER_HIGH})"
        " SELECT (SELECT count(*) FROM x) AS n,"
        "        (SELECT count(*) FROM v) AS n_valid,"
        f"       (SELECT round(avg({conc}), 3) FROM v) AS conc_avg,"
        f"       (SELECT round(min({conc}), 3) FROM v) AS conc_min,"
        f"       (SELECT round(max({conc}), 3) FROM v) AS conc_max,"
        f"       count(*) FILTER (WHERE raw < {cv.BREAK_LOW}) AS broken,"
        f"       count(*) FILTER (WHERE raw >= {cv.BREAK_LOW} AND raw < {cv.I_LO} * 1000) AS low,"
        f"       count(*) FILTER (WHERE raw > {cv.OVER_HIGH}) AS over"
        "  FROM x",
        (MODEL, hours * 3600), one=True)

    def f(v):
        return None if v is None else float(v)

    return {"model": MODEL, "hours": hours,
            "n": int(row["n"] or 0),
            "n_valid": int(row["n_valid"] or 0),
            "conc_avg": f(row["conc_avg"]), "conc_min": f(row["conc_min"]),
            "conc_max": f(row["conc_max"]),
            "broken": int(row["broken"] or 0),
            "low": int(row["low"] or 0),
            "over": int(row["over"] or 0)}


# ─────────────────────────── 看板页面 ───────────────────────────
PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ECAS 浓度计 · __MODEL__</title>
<style>
 :root{--bg:#0f1720;--card:#182533;--fg:#e8eef6;--dim:#8fa3b8;--ok:#37d67a;--warn:#ffb020;--bad:#ff5c5c}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);
   font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
 .wrap{max-width:1000px;margin:0 auto;padding:20px}
 h1{font-size:16px;font-weight:600;margin:0 0 2px} .sub{color:var(--dim);font-size:12px}
 .banner{margin:14px 0;padding:10px 14px;border-radius:8px;display:none}
 .banner.show{display:block} .banner.stale{background:#3a2018;color:var(--warn);border:1px solid #57301f}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:14px 0}
 .card{background:var(--card);border-radius:10px;padding:14px}
 .card .k{color:var(--dim);font-size:12px;margin-bottom:6px}
 .card .v{font-size:26px;font-weight:600;letter-spacing:.5px}
 .card .u{font-size:13px;color:var(--dim);margin-left:4px}
 .big .v{font-size:46px}
 table{width:100%;border-collapse:collapse;margin-top:10px;font-variant-numeric:tabular-nums}
 th,td{padding:7px 10px;text-align:right;border-bottom:1px solid #22323f}
 th:first-child,td:first-child{text-align:left}
 th{color:var(--dim);font-weight:500;font-size:12px}
 tr:hover td{background:#1d2b3a}
 .ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
 footer{color:var(--dim);font-size:12px;margin:18px 0 40px}
 code{background:#101a24;padding:1px 5px;border-radius:4px;color:#a8c7e8}
</style></head><body><div class="wrap">
 <h1>ECAS 浓度计实时看板</h1>
 <div class="sub">型号 __MODEL__ ｜ 数据源：DTU → MQTT → PostgreSQL（库里只有 型号/时间/原始值，换算在本页后端完成）</div>
 <div id="banner" class="banner"></div>
 <div class="grid">
   <div class="card big"><div class="k">当前浓度</div><div class="v"><span id="conc">—</span><span class="u">%</span></div></div>
   <div class="card"><div class="k">未补偿浓度</div><div class="v"><span id="concraw">—</span></div></div>
   <div class="card"><div class="k">电流</div><div class="v"><span id="ma">—</span><span class="u">mA</span></div></div>
   <div class="card"><div class="k">原始值 raw</div><div class="v"><span id="raw">—</span></div></div>
   <div class="card"><div class="k">状态</div><div class="v"><span id="status">—</span></div></div>
   <div class="card"><div class="k">数据时间</div><div class="v" style="font-size:15px"><span id="ts">—</span></div>
     <div class="k" style="margin:6px 0 0"><span id="age">—</span></div></div>
 </div>
 <div class="grid">
   <div class="card"><div class="k">近 24h 条数</div><div class="v"><span id="sn">—</span></div></div>
   <div class="card"><div class="k">24h 平均浓度（仅有效区间）</div><div class="v"><span id="savg">—</span></div></div>
   <div class="card"><div class="k">24h 最小 / 最大</div><div class="v" style="font-size:18px"><span id="smin">—</span> / <span id="smax">—</span></div></div>
   <div class="card"><div class="k">断线 / 偏低 / 超量程</div><div class="v" style="font-size:18px"><span id="sbad">—</span></div></div>
 </div>
 <table><thead><tr><th>时间</th><th>raw</th><th>mA</th><th>未补偿</th><th>浓度</th><th>状态</th></tr></thead>
 <tbody id="rows"><tr><td colspan="6" style="color:var(--dim)">加载中…</td></tr></tbody></table>
 <footer>自动刷新 30 秒 ｜ 口径：<code>浓度 = (raw-4000)/160 + 0.3</code> ｜
   断线 raw&lt;3600 ｜ 偏低 3600~4000（活零点以下）｜ 超量程 raw&gt;20500 ｜ 接口：<code>/api/health</code> <code>/api/readings</code> <code>/api/summary</code> <code>/api/params</code>
 </footer>
</div>
<script>
const $ = id => document.getElementById(id);
const fmt = (v, n=3) => (v===null||v===undefined) ? '—' : Number(v).toFixed(n);
async function tick(){
  try{
    const h = await (await fetch('/api/health')).json();
    const s = await (await fetch('/api/summary?hours=24')).json();
    const r = await (await fetch('/api/readings?hours=24&limit=50')).json();
    if(h.last){
      $('conc').textContent = fmt(h.last.conc); $('concraw').textContent = fmt(h.last.conc_raw);
      $('ma').textContent = fmt(h.last.ma); $('raw').textContent = h.last.raw;
      $('status').textContent = h.last.status;
      $('status').className = h.last.status==='正常'?'ok':(h.last.status==='断线'?'bad':'warn');
      $('ts').textContent = (h.last.ts||'').replace('T',' ').slice(0,19);
    }
    $('age').textContent = h.age_seconds===null ? '无数据' :
      (h.stale ? '⚠️ 已断流 '+h.age_seconds+' 秒' : '数据新鲜（'+h.age_seconds+' 秒前）');
    $('age').className = h.stale ? 'warn':'ok';
    const b = $('banner');
    if(h.stale){ b.className='banner stale show';
      b.textContent='⚠️ 数据已断流 '+(h.age_seconds===null?'（库里还没有数据）':h.age_seconds+' 秒')+
        ' —— 常见原因：DTU 侧 MQTT 口令未同步、DTU 掉线、监视服务停了。'; }
    else b.className='banner';
    $('sn').textContent = s.n + '（有效 ' + s.n_valid + '）'; $('savg').textContent = fmt(s.conc_avg);
    $('smin').textContent = fmt(s.conc_min); $('smax').textContent = fmt(s.conc_max);
    $('sbad').textContent = s.broken+' / '+s.low+' / '+s.over;
    $('rows').innerHTML = r.items.map(x =>
      `<tr><td>${(x.ts||'').replace('T',' ').slice(0,19)}</td><td>${x.raw}</td><td>${fmt(x.ma)}</td>`+
      `<td>${fmt(x.conc_raw)}</td><td>${fmt(x.conc)}</td>`+
      `<td class="${x.status==='正常'?'ok':(x.status==='断线'?'bad':'warn')}">${x.status}</td></tr>`
    ).join('') || '<tr><td colspan="6" style="color:var(--dim)">近 24 小时没有数据</td></tr>';
  }catch(e){ $('banner').className='banner stale show'; $('banner').textContent='⚠️ 读取失败：'+e; }
}
tick(); setInterval(tick, 30000);
</script></body></html>"""


# ─────────────────────────── HTTP ───────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "ecas-readonly/1.0"

    def log_message(self, fmt, *a):
        sys.stdout.write("%s  %s\n" % (datetime.now().strftime("%F %T"), fmt % a))
        sys.stdout.flush()

    def _send(self, body, ctype, code=200):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False, default=str),
                   "application/json; charset=utf-8", code)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._send(PAGE.replace("__MODEL__", MODEL),
                                  "text/html; charset=utf-8")
            if u.path == "/healthz":
                return self._send("ok\n", "text/plain; charset=utf-8")
            if u.path == "/api/params":
                return self._json({"model": MODEL, **cv.params()})
            if u.path == "/api/health":
                return self._json(api_health())

            def num(name, default, lo, hi):
                try:
                    v = int(q.get(name, [default])[0])
                except (TypeError, ValueError):
                    v = default
                return max(lo, min(hi, v))

            if u.path == "/api/readings":
                hours = num("hours", 24, 1, MAX_HOURS)
                limit = num("limit", 200, 1, MAX_LIMIT)
                offset = num("offset", 0, 0, 10 ** 9)
                return self._json(api_readings(hours, limit, offset))
            if u.path == "/api/summary":
                return self._json(api_summary(num("hours", 24, 1, MAX_HOURS)))
            self._json({"error": "not found", "path": u.path}, 404)
        except Exception as e:                      # noqa: BLE001
            self._json({"error": str(e), "type": type(e).__name__}, 500)


def main():
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"ECAS 只读接口已启动：http://{LISTEN_HOST}:{LISTEN_PORT}/  （模型 {MODEL}）", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
