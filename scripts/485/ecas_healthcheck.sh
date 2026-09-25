#!/usr/bin/env bash
# ============================================================================
# ECAS 看门狗（ecas-healthcheck.timer 每 5 分钟调用一次）
#
# 盯三件事，**只在状态变化时**记一行，避免把日志刷爆：
#   1) 数据有没有在来（>10 分钟没有新 raw = 断流）
#   2) 监视服务还活着吗（ecas-monitor.service）
#   3) DTU 有没有正在被拒（mosquitto 日志里 "Client dpro66-01 ... not authorised"
#      —— 这一条最关键：它把「口令没同步」和「链路不通」两种故障**分开**了）
#
# 日志：/root/ecas/logs/health.log   ｜ 状态：/root/ecas/data/.health-state
# ============================================================================
set -uo pipefail

export PGPASSFILE=/root/.pgpass      # systemd 下 HOME 不一定有，显式指定

LOG=/root/ecas/logs/health.log
STATE=/root/ecas/data/.health-state
MOSQ_LOG=/var/log/mosquitto/mosquitto.log
STALE_SEC=600        # 断流判定
AUTH_WINDOW=600      # 统计窗口（秒）
CLIENT_HINT='dpro66-01'

mkdir -p "$(dirname "$LOG")" "$(dirname "$STATE")"
say() { printf '%s  %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; }

now=$(date +%s)

# ── 1. 数据新鲜度 ────────────────────────────────────────────────────────
age=$(psql -X -qtAw -h 127.0.0.1 -U ecas -d ecas -c \
      "SELECT coalesce(round(extract(epoch FROM now()-max(ts)))::bigint, -1) FROM raw_reading;" 2>/dev/null | tr -d '[:space:]')
age=${age:-}
last=$(psql -X -qtAw -h 127.0.0.1 -U ecas -d ecas -c \
      "SELECT coalesce(to_char(max(ts),'YYYY-MM-DD HH24:MI:SS'),'（无数据）') FROM raw_reading;" 2>/dev/null | tr -d '\n')
rows=$(psql -X -qtAw -h 127.0.0.1 -U ecas -d ecas -c "SELECT count(*) FROM raw_reading;" 2>/dev/null | tr -d '[:space:]')

if [[ -z "$age" ]]; then
    data_state="db_error"
elif (( age < 0 )); then
    data_state="empty"
elif (( age > STALE_SEC )); then
    data_state="stale"
else
    data_state="fresh"
fi

# ── 2. 监视服务 ──────────────────────────────────────────────────────────
mon=$(systemctl is-active ecas-monitor.service 2>/dev/null || echo unknown)
sub=$(pgrep -x mosquitto_sub >/dev/null 2>&1 && echo yes || echo no)

# ── 3. DTU 认证失败（最近 AUTH_WINDOW 秒）────────────────────────────────
authfail=0
if [[ -r "$MOSQ_LOG" ]]; then
    authfail=$(awk -F: -v lim="$((now - AUTH_WINDOW))" '$1 > lim' "$MOSQ_LOG" 2>/dev/null \
               | grep -ac -- "$CLIENT_HINT.*not authorised" || true)
    authfail=${authfail:-0}
fi
if (( authfail > 0 )); then auth_state="failing"; else auth_state="ok"; fi

# ── 组装状态指纹：与上次不同才记录 ───────────────────────────────────────
fingerprint="${data_state}|${mon}|${sub}|${auth_state}"
prev=""
[[ -r "$STATE" ]] && prev="$(cat "$STATE" 2>/dev/null)"
printf '%s\n' "$fingerprint" >"$STATE"

if [[ "$fingerprint" == "$prev" ]]; then
    exit 0            # 状态没变，什么都不记
fi

# ── 状态变化：写人类可读的一行 ───────────────────────────────────────────
msg="状态变化 [${prev:-首次}] → [$fingerprint]"

case "$data_state" in
    fresh)  msg="$msg ｜ ✅ 数据正常：最新 $last（${age}s 前，共 ${rows} 行）" ;;
    stale)  msg="$msg ｜ ⚠️ 数据断流：最新 $last（已 ${age}s 无新数据，共 ${rows} 行）" ;;
    empty)  msg="$msg ｜ ⚠️ 库里还没有任何数据" ;;
    db_error) msg="$msg ｜ ❌ 查库失败（PostgreSQL 或 .pgpass 有问题？）" ;;
esac
case "$mon" in
    active) ;;
    *) msg="$msg ｜ ❌ 监视服务 ecas-monitor.service = $mon（journalctl -u ecas-monitor -n 30 看原因）" ;;
esac
[[ "$sub" == "no" && "$mon" == "active" ]] && msg="$msg ｜ ⚠️ 服务在跑但没有 mosquitto_sub 子进程（可能正在重连）"
if [[ "$auth_state" == "failing" ]]; then
    msg="$msg ｜ 🔑 DTU(${CLIENT_HINT}) 最近 $((AUTH_WINDOW/60)) 分钟被拒 ${authfail} 次 —— 十有八九是 DTU 侧口令没同步（用 /Volumes/keys/ecas/mqtt.toml 里的现役口令重填）"
elif [[ "$prev" == *"failing"* && "$auth_state" == "ok" ]]; then
    msg="$msg ｜ ✅ DTU 认证已恢复正常"
fi

say "$msg"
