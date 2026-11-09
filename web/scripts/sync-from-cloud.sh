#!/usr/bin/env bash
# 用云端 ecas 库刷新本机开发库快照（幂等：dump 带 --clean --if-exists）
#
#   bash web/scripts/sync-from-cloud.sh
#
# ⚠️ 2026-09-29 起现网换了机器，本脚本已跟着改：
#   旧：ssh ecas（腾讯云）→ 宿主机 `PGPASSFILE=/root/.pgpass pg_dump -h 127.0.0.1`
#       （systemd 时代库跑在宿主机上，读 .pgpass 要 root）
#   新：ssh ubuntu-super → `docker exec ecas-db pg_dump`（库现在是容器）
#       用户 ubuntu 在 docker 组里 ⇒ **不需要 root 口令**，也就不再依赖 /Volumes/keys；
#       容器内 pg_hba 对 local 连接是 trust，所以无需口令，口令从不流经本机。
#
# 环境变量可覆盖：HOST（默认 ubuntu-super；也认旧的 HOST_ALIAS）
#   DB_CONTAINER（ecas-db）REMOTE_USER / REMOTE_DB（ecas）
#   LOCAL_PORT（55432）LOCAL_DB（ecas_dev）LOCAL_USER（ecas）
set -euo pipefail

HOST="${HOST:-${HOST_ALIAS:-ubuntu-super}}"
DB_CONTAINER="${DB_CONTAINER:-ecas-db}"
REMOTE_USER="${REMOTE_USER:-ecas}"
REMOTE_DB="${REMOTE_DB:-ecas}"
LOCAL_PORT="${LOCAL_PORT:-55432}"
LOCAL_DB="${LOCAL_DB:-ecas_dev}"
LOCAL_USER="${LOCAL_USER:-ecas}"

TMP="$(mktemp -t ecas-dump-XXXXXX.sql)"
trap 'rm -f "$TMP"' EXIT

printf '→ 从 %s 的容器 %s 拉取 pg_dump…\n' "$HOST" "$DB_CONTAINER"
ssh -o ControlMaster=no -o ControlPath=none -o BatchMode=yes -o ConnectTimeout=10 "$HOST" \
  "docker exec $DB_CONTAINER pg_dump -U $REMOTE_USER -d $REMOTE_DB --no-owner --no-privileges --clean --if-exists" \
  > "$TMP"
printf '  dump：%s（%s 行，%s）\n' "$TMP" "$(wc -l < "$TMP" | tr -d ' ')" "$(du -h "$TMP" | cut -f1)"

printf '→ 灌入本机 %s（127.0.0.1:%s，用户 %s）…\n' "$LOCAL_DB" "$LOCAL_PORT" "$LOCAL_USER"
psql -X -q -v ON_ERROR_STOP=1 -p "$LOCAL_PORT" -U "$LOCAL_USER" -d "$LOCAL_DB" -f "$TMP"

printf '→ 核对：\n'
psql -X -p "$LOCAL_PORT" -U "$LOCAL_USER" -d "$LOCAL_DB" \
  -c "SELECT count(*) AS 行数, min(ts) AS 最早, max(ts) AS 最新 FROM raw_reading;"
printf '完成。看板会自动刷新（30 秒轮询）。\n'
