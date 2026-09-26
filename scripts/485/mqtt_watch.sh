#!/usr/bin/env bash
# ============================================================================
# ECAS · MQTT 冒名检测（隐形增量扫描 mosquitto 日志）
#
# 为什么需要它：DTU 侧不支持 TLS（塔石确认不了 SSL）⇒ 4G 链路上加密做不到，
#   能做的只有「认证 + 主题 ACL + 最小权限」+ **发现有人冒充**。
#   这个脚本就是最后那一环：真 DTU 的账号一旦被冒用，行为特征藏不住 ——
#   未知 ClientId、来源 IP 突变、匿名连接、暴力试口令。
#
# 与 ecas_healthcheck.sh 的分工（互不重复）：
#   healthcheck ：数据有没有断流、监视服务在不在、"当前 DTU 账号正被拒"
#   mqtt_watch  ：谁在连、从哪连、用哪个 ClientId/账号 —— **不该出现的人**
#
# 输出：
#   /root/ecas/logs/mqtt-watch.jsonl   一行一个事件（审计留痕，只追加）
#   /root/ecas/state/mqtt-watch.state  扫描位置 + 记住的 DTU 来源 IP（不重复告警）
#
# 由 ecas-mqtt-watch.timer 每 2 分钟调用一次；手动：mqtt_watch.sh --show [-n N]
# ============================================================================

set -uo pipefail

LOG="/var/log/mosquitto/mosquitto.log"
STATE_DIR="/root/ecas/state"
STATE="$STATE_DIR/mqtt-watch.state"
OUT_DIR="/root/ecas/logs"
OUT="$OUT_DIR/mqtt-watch.jsonl"

# ---- 白名单（改这里就等于改判据）----
ALLOW_USERS="ecas dpro66-dtu ecas-monitor"
DTU_USERS="dpro66-dtu"                       # 现场 DTU 用的账号（谁的 IP 值得盯）
ALLOW_CLIENTS="dpro66-01 dpro66-dtu dpro66-dtu-01 dpro66-monitor"
AUTH_BURST_N=3                               # 一轮里「not authorised」超过这个数就报

SHOW=0; SHOW_N=20; SCAN_ALL=0
while (( $# )); do
    case "$1" in
        --show)   SHOW=1; shift ;;
        --all)    SCAN_ALL=1; shift ;;          # 全量扫历史（默认首次运行只从日志末尾开始）
        -n|--lines) SHOW_N="$2"; shift 2 ;;
        --log)    LOG="$2"; shift 2 ;;
        --state)  STATE="$2"; shift 2 ;;
        --out)    OUT="$2"; shift 2 ;;
        -h|--help) sed -n '2,/^set -uo/p' "$0" | sed 's/^# \{0,1\}//;$d'; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

if (( SHOW )); then
    if [[ -s "$OUT" ]]; then tail -n "$SHOW_N" "$OUT"; else echo "（还没有事件）"; fi
    exit 0
fi

mkdir -p "$(dirname "$STATE")" "$(dirname "$OUT")"
touch "$OUT" || exit 1

[[ -r "$LOG" ]] || { echo "读不到 $LOG" >&2; exit 1; }

# ---- 读取上次扫描位置（日志轮转后自动归零）----
off=0; last_dtu_ip=""; have_state=0
if [[ -r "$STATE" ]]; then
    # shellcheck disable=SC1090
    source "$STATE" 2>/dev/null || true
    off="${off:-0}"; last_dtu_ip="${last_dtu_ip:-}"; have_state=1
fi
size=$(wc -c < "$LOG" 2>/dev/null || echo 0); size="${size// /}"
# 首次运行（没有状态文件）：只从日志末尾开始盯，**不把历史当告警**；要看历史加 --all
if (( ! have_state && ! SCAN_ALL )); then
    printf 'off=%s\nlast_dtu_ip=%s\n' "$size" "$last_dtu_ip" > "$STATE"
    printf '首次运行：扫描位置已落到当前日志末尾（off=%s）；历史不告警，全量扫历史请加 --all\n' "$size"
    exit 0
fi
if (( size < off )); then off=0; fi           # 轮转过 ⇒ 从头读
if (( size <= off )); then
    printf 'off=%s\nlast_dtu_ip=%s\n' "$off" "$last_dtu_ip" > "$STATE"
    exit 0                                     # 没有新日志
fi

new="$(tail -c +$((off + 1)) "$LOG" 2>/dev/null || true)"
off="$size"

if (( SHOW )); then echo "$new"; fi

json_esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
n_events=0
emit() {   # $1 level  $2 kind  $3 detail  $4 ip  $5 client  $6 user
    local ts; ts="$(date '+%F %T%:z')"; [[ "$ts" == *:z ]] && ts="$(date '+%F %T%z')"
    printf '{"ts":"%s","level":"%s","kind":"%s","detail":"%s","ip":"%s","client":"%s","user":"%s"}\n' \
        "$ts" "$1" "$2" "$(json_esc "$3")" "${4:-}" "${5:-}" "${6:-}" >> "$OUT"
    n_events=$(( n_events + 1 ))
    printf '%s  [%s] %s —— %s\n' "$ts" "$1" "$2" "$3"
}

# ---- 规则 1~4：看「谁连上来了」----
while IFS= read -r line; do
    [[ "$line" == *"New client connected from "* ]] || continue
    ip="${line#*New client connected from }"; ip="${ip%%:*}"
    rest="${line#* as }"
    client="${rest%% *}"
    user=""
    if [[ "$line" =~ u\'([^\']*)\' ]]; then user="${BASH_REMATCH[1]}"; fi
    client_raw="$client"
    if [[ "$client" =~ ^[0-9A-F]{8}-[0-9A-F]{4}- ]]; then client="<随机 ClientId: $client>"; fi

    if [[ -z "$user" ]]; then
        emit "严重" "匿名连接" "有人以匿名身份连上 MQTT（allow_anonymous 应该已是 false！）" "$ip" "$client" ""
        continue
    fi
    if ! grep -qw -- "$user" <<< "$ALLOW_USERS"; then
        emit "严重" "未知账号" "账号 ${user} 不在白名单（${ALLOW_USERS}）" "$ip" "$client" "$user"
        continue
    fi
    # ClientId 检查：管理账号 ecas 我自己会用随机 ClientId 做测试，跳过；
    # 现场 DTU / 采集账号则是「**任何**白名单外的 ClientId 都要报」（含随机 UUID —— 冒名者最爱随机）
    if [[ "$user" == "ecas" || " $ALLOW_USERS " != *" $user "* ]]; then
        :                                            # 管理账号 / 未知账号（上面已报）不重复报
    elif [[ " $DTU_USERS " == *" $user "* ]]; then
        grep -qw -- "$client_raw" <<< "$ALLOW_CLIENTS" \
            || emit "警告" "未知ClientId" "现场账号 ${user} 用了白名单外的 ClientId（疑似冒名）" "$ip" "$client" "$user"
    else
        [[ "$client" == \<随机* ]] || grep -qw -- "$client_raw" <<< "$ALLOW_CLIENTS" \
            || emit "警告" "未知ClientId" "账号 ${user} 用了白名单外的 ClientId" "$ip" "$client" "$user"
    fi
    if [[ " $DTU_USERS " == *" $user "* ]]; then
        if [[ -n "$last_dtu_ip" && "$ip" != "$last_dtu_ip" ]]; then
            # 4G 重播号 ⇒ 每次重连都可能换 IP，属常态：只记「提示」，不当告警（2026-09-26 先生确认降级）
            emit "提示" "DTU来源IP变化" "DTU 账号 ${user} 的来源 IP 从 ${last_dtu_ip} 变成 ${ip}（4G 重播号属正常）" "$ip" "$client" "$user"
        fi
        last_dtu_ip="$ip"
    fi
done <<< "$new"

# ---- 规则 5：口令爆破 ----
n_authfail="$(grep -c -- "not authorised" <<< "$new" || true)"
if (( n_authfail >= AUTH_BURST_N )); then
    emit "警告" "口令失败激增" "本轮 ${n_authfail} 次认证失败（阈值 ${AUTH_BURST_N}）—— 有人在试口令" "" "" ""
fi

# ---- 规则 6：协议错（扫描器特征，只记不改状态）----
n_proto="$(grep -c -- "disconnected due to protocol error" <<< "$new" || true)"
if (( n_proto > 0 )); then
    emit "提示" "协议错误" "${n_proto} 次非 MQTT 连接（互联网背景扫描的典型特征）" "" "" ""
fi

printf 'off=%s\nlast_dtu_ip=%s\n' "$off" "$last_dtu_ip" > "$STATE"
exit 0
