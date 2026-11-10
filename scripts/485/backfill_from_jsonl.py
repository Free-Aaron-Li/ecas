#!/usr/bin/env python3
"""把 rtu-live.jsonl（或云端 spool）里已采到的帧回灌进数据库，幂等。

背景（2026-11-10）：本地服务器 13:48 宕机、17:00 恢复，但重启后 MQTT broker 起不来，
浓度数据从 13:48 断到 19:20。rtu-master 一直在写 /opt/ecas/rtu-master/logs/rtu-live.jsonl，
所以 17:00–19:20 的数据其实都在磁盘上，只是没进库。本工具就是把这些帧补进
raw_reading / flow_reading（PK 冲突即跳过，可反复跑）。

用法：
  python3 backfill_from_jsonl.py --file /opt/ecas/rtu-master/logs/rtu-live.jsonl \
      --since 2026-11-10T13:40 --until 2026-11-10T19:25 | \
      docker compose exec -T db psql -U ecas -d ecas -v ON_ERROR_STOP=1

  --dry-run 只统计将回灌的行数（不输出 SQL）。
  --station conc|flow|all（默认 all）

字段口径（与 collector 入库一致）：
  conc → raw_reading(model='DPRO-66', ts, raw)
  flow → flow_reading(model='FMC400', ts, regs int[14])
只回灌 ok=true 且带 ts 的帧；没有 regs 的 flow 帧跳过。
"""
import argparse
import json
import sys
from datetime import datetime


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def sql_ts(s):
    # Postgres 直接吃 ISO 8601（带时区），比手工格式化更不易丢精度
    return "'" + s.strip().replace("'", "''") + "'"


def rows_from_file(path, since, until, station):
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            st = rec.get("station")
            if st not in ("conc", "flow"):
                continue
            if station != "all" and st != station:
                continue
            if rec.get("ok") is False:
                continue
            ts_raw = rec.get("ts")
            ts = parse_ts(ts_raw)
            if ts is None:
                continue
            if since and ts < since:
                continue
            if until and ts > until:
                continue
            if st == "conc":
                raw = rec.get("raw")
                if raw is None:
                    continue
                yield ("conc", sql_ts(ts_raw), int(raw))
            else:
                regs = rec.get("regs")
                if not isinstance(regs, list) or len(regs) != 14:
                    continue
                yield ("flow", sql_ts(ts_raw), [int(x) for x in regs])


def main(argv=None):
    ap = argparse.ArgumentParser(description="rtu-live.jsonl → raw_reading/flow_reading 幂等回灌")
    ap.add_argument("--file", action="append", required=True, help="JSONL 路径（可重复给）")
    ap.add_argument("--since", default="", help="起（ISO，如 2026-11-10T13:40）")
    ap.add_argument("--until", default="", help="止（ISO）")
    ap.add_argument("--station", default="all", choices=["all", "conc", "flow"])
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    # 边界时间允许省略时区（按本机时区解释，服务器/现场都是 CST）
    localtz = datetime.now().astimezone().tzinfo

    def norm(d):
        if d is not None and d.tzinfo is None:
            return d.replace(tzinfo=localtz)
        return d

    since = norm(parse_ts(a.since)) if a.since else None
    until = norm(parse_ts(a.until)) if a.until else None

    conc, flow = [], []

    def flush(kind):
        buf = conc if kind == "conc" else flow
        while buf:
            chunk, buf[:] = buf[: a.batch], buf[a.batch:]
            if kind == "conc":
                vals = ",".join("('DPRO-66',%s,%d)" % (t, r) for t, r in chunk)
                print("INSERT INTO raw_reading(model,ts,raw) VALUES %s ON CONFLICT DO NOTHING;" % vals)
            else:
                vals = ",".join(
                    "('FMC400',%s,ARRAY[%s])" % (t, ",".join(str(x) for x in regs))
                    for t, regs in chunk
                )
                print("INSERT INTO flow_reading(model,ts,regs) VALUES %s ON CONFLICT DO NOTHING;" % vals)

    n_c = n_f = 0
    candidates = (x for f in a.file for x in rows_from_file(f, since, until, a.station))
    for kind, t, payload in candidates:
        if kind == "conc":
            conc.append((t, payload))
            n_c += 1
            if len(conc) >= a.batch:
                if not a.dry_run:
                    flush("conc")
                conc = []
        else:
            flow.append((t, payload))
            n_f += 1
            if len(flow) >= a.batch:
                if not a.dry_run:
                    flush("flow")
                flow = []
    if not a.dry_run:
        flush("conc")
        flush("flow")

    sys.stderr.write("回灌候选：conc %d 行，flow %d 行%s\n" % (n_c, n_f, "（dry-run，未输出 SQL）" if a.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
