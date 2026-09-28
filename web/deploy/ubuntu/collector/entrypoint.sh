#!/usr/bin/env bash
# ============================================================================
# ECAS 采集器容器入口 —— TLS 取数（只读账号）→ 管道 → 现成的 dpro66_monitor.sh
#
# 左边：mqtt_tls_feeder.py 连云端 Mosquitto 8884（TLS + ecas-monitor 只读账号），
#       按 `%I|%x` 格式吐 `ts|hex` 行；
# 右边：`dpro66_monitor.sh --pipe`（**仓库里那一份，原样复用**，deploy.sh 推上来），
#       负责解析、去重、换算、判异常、写 PostgreSQL。
#
# ⛔ 凭据形态：都是 compose 的 container secret（源自 systemd 加密凭据）
#   /run/secrets/mqtt-pass   → 直接给 paho 读（口令不进 argv）
#   /run/secrets/pg-password → 现造成 /tmp/pgpass（0600）给 psql/libpq 用
#   两者都不出现在命令行、环境变量的值里，也不写日志。
# ============================================================================
set -uo pipefail

: "${MQTT_HOST:?需要 MQTT_HOST（云端 broker 地址）}"
MQTT_PORT="${MQTT_PORT:-8884}"
MQTT_TOPIC="${MQTT_TOPIC:-dpro66}"
MQTT_USER="${MQTT_USER:-ecas-monitor}"
MQTT_PASS_FILE="${MQTT_PASS_FILE:-/run/secrets/mqtt-pass}"
MQTT_CAFILE="${MQTT_CAFILE:-/etc/ecas/ca.crt}"
MQTT_CLIENT_ID="${MQTT_CLIENT_ID:-dpro66-collector-ubuntu}"

DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-ecas}"
DB_NAME="${DB_NAME:-ecas}"
PG_PASSWORD_FILE="${PG_PASSWORD_FILE:-/run/secrets/pg-password}"
PGPASSFILE="${PGPASSFILE:-/tmp/pgpass}"
DB_DSN="-h ${DB_HOST} -p ${DB_PORT} -U ${DB_USER} -d ${DB_NAME}"

DB_MODEL="${DB_MODEL:-DPRO-66}"
OFFSET_CONC="${OFFSET_CONC:-0.48}"   # ⭐ 现场标定值（不是 0.5）
GAIN="${GAIN:-1.0}"
I_LO="${I_LO:-4.0}"
I_HI="${I_HI:-20.0}"
C_LO="${C_LO:-0}"
C_HI="${C_HI:-100}"

# 数据库口令 → pgpass（库名/用户名/主机/端口都要与 DB_DSN 对上，libpq 才找得到）
if [ -r "$PG_PASSWORD_FILE" ]; then
    printf '%s:%s:%s:%s:%s\n' "$DB_HOST" "$DB_PORT" "$DB_NAME" "$DB_USER" \
        "$(cat "$PG_PASSWORD_FILE")" > "$PGPASSFILE"
    chmod 600 "$PGPASSFILE"
    export PGPASSFILE
else
    echo "[entrypoint] ⚠️ 没有 $PG_PASSWORD_FILE，入库会因缺口令失败" >&2
fi

echo "── ECAS 采集端 ────────────────────────────────────────────"
echo "  取数：TLS ${MQTT_HOST}:${MQTT_PORT}  主题 ${MQTT_TOPIC}  用户 ${MQTT_USER}"
echo "  入库：PostgreSQL ${DB_HOST}:${DB_PORT}/${DB_NAME}（用户 ${DB_USER}，口令走 PGPASSFILE）"
echo "  口径：型号 ${DB_MODEL}  量程 ${I_LO}-${I_HI}mA → ${C_LO}-${C_HI}  补偿 +${OFFSET_CONC}  增益 ×${GAIN}"
echo "───────────────────────────────────────────────────────────"

# 管道：feeder 死 → monitor 读到 EOF 退出；monitor 死 → feeder 收 SIGPIPE。
# compose 里加了 `init: true`（tini），容器停止时整组进程一起收信号。
python3 /opt/ecas/mqtt_tls_feeder.py \
    --host "$MQTT_HOST" --port "$MQTT_PORT" --topic "$MQTT_TOPIC" \
    --username "$MQTT_USER" --password-file "$MQTT_PASS_FILE" --cafile "$MQTT_CAFILE" \
    --cert-sha256 "${MQTT_CERT_SHA256:-}" \
    --client-id "$MQTT_CLIENT_ID" --verbose \
  | bash /opt/ecas/dpro66_monitor.sh --pipe \
      --model "$DB_MODEL" --no-csv --db-dsn "$DB_DSN" \
      --i-lo "$I_LO" --i-hi "$I_HI" --c-lo "$C_LO" --c-hi "$C_HI" \
      --offset "$OFFSET_CONC" --gain "$GAIN"
