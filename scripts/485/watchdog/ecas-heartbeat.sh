#!/bin/bash
# 本地→云端心跳（2026-11-10 初版；2026-11-11 改为按"实时帧"判新鲜）
# 云侧 ecas-gateway-flip.sh：心跳新鲜 + 浓度/流量都新鲜 ⇒ direct；否则 ⇒ capture。
#
# 2026-11-11 修的坑：初版用库里 max(ts) 算新鲜度，而 capture 期间库里的数来自
# 每 15 分钟的回灌 ⇒ 库"很新鲜"但直采早就断了，云端据此又把选路切回 direct，
# 形成 15 分钟振荡。现在 conc_age/flow_age 一律取 rtu-live.jsonl 里最后一帧
# "ok":true 的时刻（真实采集证据），库里时间只作为 db_* 旁证上报。
set -u
CLOUD="10.10.10.123"
PORT="${HB_PORT:-18889}"
DB="ecas-db"
JSONL="${RTU_JSONL:-/opt/ecas/rtu-master/logs/rtu-live.jsonl}"
TO=5

q() { docker exec "$DB" psql -U ecas -d ecas -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }

db_conc=$(q "select coalesce(round(extract(epoch from now()-max(ts)))::int, 99999) from raw_reading where model='DPRO-66';")
db_flow=$(q "select coalesce(round(extract(epoch from now()-max(ts)))::int, 99999) from flow_reading where model='FMC400';")
conc_rows=$(q "select count(*) from raw_reading where ts > now()-interval '1 hour';")
broker=$(docker inspect -f '{{.State.Running}}' ecas-mosquitto 2>/dev/null || echo false)
rtu=$(docker inspect -f '{{.State.Running}}' ecas-rtu-master 2>/dev/null || echo false)
[ -z "$db_conc" ] && db_conc=99999
[ -z "$db_flow" ] && db_flow=99999

live=$(python3 - "$JSONL" <<'PY'
import json,sys,datetime,time
path=sys.argv[1]
last={}
try:
    with open(path,"rb") as fh:
        fh.seek(0,2); size=fh.tell()
        fh.seek(max(0,size-400_000))
        for line in fh.read().decode("utf-8","replace").splitlines():
            try: rec=json.loads(line)
            except Exception: continue
            if not rec.get("ok"): continue
            st=rec.get("station")
            if st in ("conc","flow") and rec.get("ts"):
                last[st]=rec["ts"]
except OSError:
    pass
now=datetime.datetime.now().astimezone()
out=[]
for st in ("conc","flow"):
    ts=last.get(st)
    if not ts: out.append("99999"); continue
    try:
        t=datetime.datetime.fromisoformat(ts)
        out.append(str(max(0,int((now-t).total_seconds()))))
    except Exception:
        out.append("99999")
print(" ".join(out))
PY
)
read -r conc_age flow_age <<<"$live"
[ -z "${conc_age:-}" ] && conc_age=99999
[ -z "${flow_age:-}" ] && flow_age=99999

body=$(python3 - "$conc_age" "$flow_age" "$db_conc" "$db_flow" "$conc_rows" "$broker" "$rtu" <<'PY'
import json,sys,datetime,os,socket
conc,flow,dbc,dbf,rows,broker,rtu=sys.argv[1:8]
rec={"host":socket.gethostname(),"ts":datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
     "conc_age":int(conc),"flow_age":int(flow),
     "db_conc_age":int(dbc),"db_flow_age":int(dbf),
     "conc_rows_1h":int(rows or 0),
     "broker":broker=="true","rtu":rtu=="true","uptime_s":int(float(open('/proc/uptime').read().split()[0]))}
print(json.dumps(rec))
PY
)

if curl -sf -m "$TO" -X POST -H 'Content-Type: application/json' -d "$body" \
    "http://$CLOUD:$PORT/hb" >/dev/null; then
  echo "$(date '+%F %T') hb ok $body"
else
  echo "$(date '+%F %T') hb FAIL $body" >&2
  exit 1
fi
