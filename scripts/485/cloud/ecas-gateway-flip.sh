#!/bin/bash
# 云端选路（2026-11-10）：按本地心跳决定 DTU 落点
#   心跳新鲜且两个传感器都新鲜 → direct（DNAT 直达本地，与改造前一致）
#   心跳过期 / 浓度或流量停更        → capture（DNAT 直达云采集器，落 spool 等回灌）
set -u
HB="/var/lib/ecas/fallback/heartbeat.json"
LOG="/var/log/ecas/fallback-flip.jsonl"
GW="/usr/local/sbin/ecas-gateway.sh"
HB_STALE="${HB_STALE:-240}"
CONC_STALE="${CONC_STALE:-300}"
FLOW_STALE="${FLOW_STALE:-300}"
MODE_FILE="/etc/ecas/gateway-mode"
cur="$(cat "$MODE_FILE" 2>/dev/null || echo direct)"
mkdir -p /var/log/ecas /etc/ecas

desired="capture"; why="无心跳文件"
if [ -r "$HB" ]; then
  read -r age conc flow < <(python3 - "$HB" <<'PY'
import json,sys,time
d=json.load(open(sys.argv[1],encoding="utf-8"))
now=time.time()
print(int(now-d.get("recv_ts",0)), int(d.get("conc_age",99999)), int(d.get("flow_age",99999)))
PY
)
  if [ "$age" -le "$HB_STALE" ] && [ "$conc" -le "$CONC_STALE" ] && [ "$flow" -le "$FLOW_STALE" ]; then
    desired="direct"; why="心跳 ${age}s、浓度 ${conc}s、流量 ${flow}s 均新鲜"
  else
    why="心跳 ${age}s、浓度 ${conc}s、流量 ${flow}s"
  fi
fi

if [ "$desired" != "$cur" ]; then
  bash "$GW" "$desired" || exit 1
  python3 - "$LOG" "$desired" "$why" <<'PY'
import json,sys,datetime
log,mode,why=sys.argv[1:4]
rec={"ts":datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
     "event":"flip","mode":mode,"why":why}
with open(log,"a",encoding="utf-8") as fh: fh.write(json.dumps(rec,ensure_ascii=False)+"\n")
print(json.dumps(rec,ensure_ascii=False))
PY
else
  echo "mode=$cur 保持不变（$why）"
fi
