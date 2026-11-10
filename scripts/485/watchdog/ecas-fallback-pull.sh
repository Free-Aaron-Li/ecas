#!/bin/bash
# 本地拉取云端 spool 并回灌（2026-11-10）：本地恢复后把离线期间云侧缓存的帧补进库。
# 通道：受限 SSH（只有 pull/rotate/size 三个动作，无 shell）；回灌走 backfill 的 PK 幂等 INSERT。
set -u
CLOUD="${CLOUD_HOST:-124.222.122.146}"
KEY="${SPOOL_KEY:-/home/ubuntu/.ssh/ecas-spool-key}"
DIR="/var/lib/ecas/fallback"
RAW="$DIR/cloud-spool.jsonl"
CUR="$DIR/pull.cursor"
LOG="/var/log/ecas/fallback-pull.jsonl"
mkdir -p "$DIR"
sshcmd() { ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 \
             -o IdentitiesOnly=yes -i "$KEY" "root@$CLOUD" "$1"; }

tmp="$RAW.tmp"
if ! sshcmd pull > "$tmp" 2>"$DIR/pull.err"; then
  echo "$(date '+%F %T') pull 失败：$(tail -1 "$DIR/pull.err" 2>/dev/null)" >&2
  echo "{\"ts\":\"$(date -Iseconds)\",\"event\":\"pull_error\"}" >> "$LOG"; exit 1
fi
lines=$(wc -l < "$tmp" | tr -d ' ')
if [ "$lines" -eq 0 ]; then
  echo "$(date '+%F %T') 云端无缓存"; rm -f "$tmp"; exit 0
fi
mv "$tmp" "$RAW"

since=""
[ -r "$CUR" ] && since="$(cat "$CUR")"
args=(--file "$RAW")
[ -n "$since" ] && args+=(--since "$since")
python3 /opt/ecas/rtu-master/backfill_from_jsonl.py "${args[@]}" 2>"$DIR/backfill.err" \
  | docker exec -i ecas-db psql -U ecas -d ecas -v ON_ERROR_STOP=1 >"$DIR/backfill.out" 2>&1
rc=${PIPESTATUS[0]}
if [ "$rc" -ne 0 ]; then
  echo "$(date '+%F %T') 回灌失败 rc=$rc：$(tail -2 "$DIR/backfill.err" | tr '\n' ' ')" >&2
  echo "{\"ts\":\"$(date -Iseconds)\",\"event\":\"backfill_error\",\"lines\":$lines}" >> "$LOG"; exit 1
fi
cursor=$(python3 - "$RAW" <<'PY'
import json,sys
mx=""
for line in open(sys.argv[1],encoding="utf-8"):
    try: r=json.loads(line)
    except Exception: continue
    t=r.get("ts") or ""
    if t>mx: mx=t
print(mx)
PY
)
[ -n "$cursor" ] && printf '%s' "$cursor" > "$CUR"
sshcmd rotate >/dev/null 2>&1 || true
echo "{\"ts\":\"$(date -Iseconds)\",\"event\":\"pull_ok\",\"lines\":$lines,\"cursor\":\"$cursor\"}" >> "$LOG"
echo "$(date '+%F %T') 回灌完成：$lines 行，cursor=$cursor"
