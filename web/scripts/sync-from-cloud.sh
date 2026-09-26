#!/usr/bin/env bash
# 用云端 ecas 库刷新本机开发库快照（幂等：dump 带 --clean --if-exists）
#
#   bash web/scripts/sync-from-cloud.sh
#
# 环境变量可覆盖：HOST_ALIAS（默认 ecas）LOCAL_PORT（55432）LOCAL_DB（ecas_dev）LOCAL_USER（ecas）
set -euo pipefail

HOST_ALIAS="${HOST_ALIAS:-ecas}"
LOCAL_PORT="${LOCAL_PORT:-55432}"
LOCAL_DB="${LOCAL_DB:-ecas_dev}"
LOCAL_USER="${LOCAL_USER:-ecas}"

TMP="$(mktemp -t ecas-dump-XXXXXX.sql)"
trap 'rm -f "$TMP"' EXIT

printf '→ 从云端拉取 pg_dump（主机别名 %s）…\n' "$HOST_ALIAS"
ssh -o ControlMaster=no -o BatchMode=yes -o ConnectTimeout=10 "$HOST_ALIAS" \
  "PGPASSFILE=/root/.pgpass pg_dump -h 127.0.0.1 -U ecas -d ecas --no-owner --no-privileges --clean --if-exists" \
  > "$TMP"
printf '  dump：%s（%s 行，%s）\n' "$TMP" "$(wc -l < "$TMP" | tr -d ' ')" "$(du -h "$TMP" | cut -f1)"

printf '→ 灌入本机 %s（127.0.0.1:%s，用户 %s）…\n' "$LOCAL_DB" "$LOCAL_PORT" "$LOCAL_USER"
psql -X -q -v ON_ERROR_STOP=1 -p "$LOCAL_PORT" -U "$LOCAL_USER" -d "$LOCAL_DB" -f "$TMP"

printf '→ 核对：\n'
psql -X -p "$LOCAL_PORT" -U "$LOCAL_USER" -d "$LOCAL_DB" \
  -c "SELECT count(*) AS 行数, min(ts) AS 最早, max(ts) AS 最新 FROM raw_reading;"
printf '完成。看板会自动刷新（30 秒轮询）。\n'
