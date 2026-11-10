#!/bin/bash
# 本地→云端心跳（2026-11-10）：每 60s POST 一次，用于云侧判断本地在线/离线并决定 DTU 落点。
# 云侧 ecas-gateway-flip.sh：心跳新鲜 + 浓度/流量都新鲜 ⇒ direct；否则 ⇒ capture（落 spool 等回灌）。
set -u
CLOUD="10.10.10.123"
PORT="${HB_PORT:-18889}"
DB="ecas-db"
TO=5

q() { docker exec "$DB" psql -U ecas -d ecas -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }

conc_age=$(q "select coalesce(round(extract(epoch from now()-max(ts)))::int, 99999) from raw_reading where model='DPRO-66';")
flow_age=$(q "select coalesce(round(extract(epoch from now()-max(ts)))::int, 99999) from flow_reading where model='FMC400';")
conc_rows=$(q "select count(*) from raw_reading where ts > now()-interval '1 hour';")
broker=$(docker inspect -f '{{.State.Running}}' ecas-mosquitto 2>/dev/null || echo false)
rtu=$(docker inspect -f '{{.State.Running}}' ecas-rtu-master 2>/dev/null || echo false)
[ -z "$conc_age" ] && conc_age=99999
[ -z "$flow_age" ] && flow_age=99999

body=$(python3 - "$conc_age" "$flow_age" "$conc_rows" "$broker" "$rtu" <<'PY'
import json,sys,datetime,os,socket
conc,flow,rows,broker,rtu=sys.argv[1:6]
rec={"host":socket.gethostname(),"ts":datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
     "conc_age":int(conc),"flow_age":int(flow),"conc_rows_1h":int(rows or 0),
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
