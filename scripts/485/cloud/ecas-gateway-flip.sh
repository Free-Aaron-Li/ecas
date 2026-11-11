#!/bin/bash
# 云端选路（2026-11-10 初版；2026-11-11 修误切）
#   心跳新鲜 + 浓度/流量都新鲜            → direct（DNAT 直达本地，与改造前一致）
#   心跳过期，但本地 8883 仍可达           → 保持 direct，只记 hb_path_broken（心跳链路坏，不是本地离线）
#   心跳过期且本地 8883 不可达 / 传感器停更 → capture（DNAT 直达云采集器，落 spool 等回灌）
#
# 2026-11-11 事故：心跳接收端单线程被一个半开连接卡死，心跳 8h 全丢；因为"浓度/流量"取自
# 库（当时的数来自回灌，看着新鲜），flipper 把"心跳链路坏"当成了"本地离线"切了 capture，
# 白白把 DTU 抢到云端 8 小时。修法：心跳过期时先探本地 8883，能连上就绝不切。
set -u
HB="/var/lib/ecas/fallback/heartbeat.json"
LOG="/var/log/ecas/fallback-flip.jsonl"
GW="/usr/local/sbin/ecas-gateway.sh"
HB_STALE="${HB_STALE:-240}"
CONC_STALE="${CONC_STALE:-300}"
FLOW_STALE="${FLOW_STALE:-300}"
LOCAL_ADDR="${LOCAL_ADDR:-10.10.10.11}"
LOCAL_PORT="${LOCAL_PORT:-8883}"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-3}"
BROKEN_GAP="${BROKEN_GAP:-600}"
BROKEN_STAMP="/var/lib/ecas/fallback/hb-path-broken.last"
GRACE_FILE="/var/lib/ecas/fallback/direct-grace.until"
GRACE_SECS="${GRACE_SECS:-240}"
NOW=$(date +%s)
MODE_FILE="/etc/ecas/gateway-mode"
cur="$(cat "$MODE_FILE" 2>/dev/null || echo direct)"
mkdir -p /var/log/ecas /etc/ecas /var/lib/ecas/fallback

probe_local() {
  python3 - "$LOCAL_ADDR" "$LOCAL_PORT" "$PROBE_TIMEOUT" <<'PY'
import socket,sys
host,port,to=sys.argv[1],int(sys.argv[2]),float(sys.argv[3])
s=socket.socket(); s.settimeout(to)
try:
    s.connect((host,port)); sys.exit(0)
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

desired="capture"; why="无心跳文件"; event="flip"; broken=0
if [ -r "$HB" ]; then
  read -r age conc flow < <(python3 - "$HB" <<'PY'
import json,sys,time
d=json.load(open(sys.argv[1],encoding="utf-8"))
now=time.time()
print(int(now-d.get("recv_ts",0)), int(d.get("conc_age",99999)), int(d.get("flow_age",99999)))
PY
)
  if [ "$cur" = "capture" ]; then
    # 恢复判据（capture->direct）：本地活着就行——心跳新鲜 + 8883 可达。
    # 不能用传感器新鲜度：capture 期间 DTU 在云端，本地帧必然是停的，否则永远回不去。
    if [ "$age" -le "$HB_STALE" ] && probe_local; then
      desired="direct"; why="心跳 ${age}s、本地 ${LOCAL_ADDR}:${LOCAL_PORT} 可达 => 恢复直连（浓度 ${conc}s、流量 ${flow}s 待 DTU 回连后复核）"
    else
      why="心跳 ${age}s、浓度 ${conc}s、流量 ${flow}s（capture 维持：需心跳新鲜且本地 8883 可达）"
    fi
  elif [ "$age" -le "$HB_STALE" ] && [ "$NOW" -lt "$(cat "$GRACE_FILE" 2>/dev/null || echo 0)" ] && probe_local; then
    # 刚切回直连的宽限期：DTU 还没重连、本地帧当然还是旧的，此时绝不能又切回 capture
    desired="direct"; why="心跳 ${age}s、本地 8883 可达，直连宽限期剩余 $(( $(cat "$GRACE_FILE") - NOW ))s（浓度 ${conc}s、流量 ${flow}s 待 DTU 回连）"
  elif [ "$age" -le "$HB_STALE" ] && [ "$conc" -le "$CONC_STALE" ] && [ "$flow" -le "$FLOW_STALE" ]; then
    desired="direct"; why="心跳 ${age}s、浓度 ${conc}s、流量 ${flow}s 均新鲜"
  elif [ "$age" -gt "$HB_STALE" ] && [ "$conc" -le "$CONC_STALE" ] && [ "$flow" -le "$FLOW_STALE" ]; then
    # 心跳过期但最后一次心跳说传感器是好的：先怀疑心跳链路，探本地 8883
    if probe_local; then
      desired="direct"; broken=1
      why="心跳 ${age}s 但本地 ${LOCAL_ADDR}:${LOCAL_PORT} 可达（心跳链路断，保持直连）"
    else
      why="心跳 ${age}s、浓度 ${conc}s、流量 ${flow}s，且本地 8883 不可达"
    fi
  else
    why="心跳 ${age}s、浓度 ${conc}s、流量 ${flow}s（传感器已停更）"
  fi
fi

log_rec() {
  python3 - "$LOG" "$1" "$2" "$3" <<'PY'
import json,sys,datetime
log,ev,mode,why=sys.argv[1:5]
rec={"ts":datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
     "event":ev,"mode":mode,"why":why}
with open(log,"a",encoding="utf-8") as fh: fh.write(json.dumps(rec,ensure_ascii=False)+"\n")
print(json.dumps(rec,ensure_ascii=False))
PY
}

if [ "$broken" = 1 ]; then
  now=$(date +%s); last=$(cat "$BROKEN_STAMP" 2>/dev/null || echo 0)
  if [ $((now-last)) -ge "$BROKEN_GAP" ]; then
    echo "$now" > "$BROKEN_STAMP"
    log_rec hb_path_broken "$cur" "$why"
  fi
  # 心跳链路坏但仍是直连：不动选路
  [ "$cur" = "direct" ] && echo "mode=direct 保持不变（$why）" && exit 0
fi

if [ "$desired" != "$cur" ]; then
  bash "$GW" "$desired" || exit 1
  if [ "$desired" = "direct" ]; then echo $((NOW+GRACE_SECS)) > "$GRACE_FILE"; fi
  log_rec "$event" "$desired" "$why"
else
  echo "mode=$cur 保持不变（$why）"
fi
