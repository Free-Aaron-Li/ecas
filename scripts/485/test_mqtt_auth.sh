#!/usr/bin/env bash
# ==============================================================================
# ECAS · MQTT 认证联通性测试（服务器侧）
#
#   作用：四组对照判定「匿名 / 正确口令 / 错口令 / 错用户名」，给出结论
#         ——「认证到底开了没有」。
#
#   ⭐ 为什么用 mosquitto_pub 而不是 mosquitto_sub 来测认证：
#      订阅要等到**一条消息**才算成功，而本系统数据 **100 秒一条**；
#      用 `-W 5` 去测必然打印 **Timed out** —— 那是「连上了、但 5 秒内没数据」，
#      不是认证失败！两种情况输出完全不同：
#          连上但没数据  →  Timed out
#          认证被拒      →  Connection error: Connection Refused: not authorised.
#      mosquitto_pub 一连上就发一条自测消息、立刻退出 ⇒ 秒级、判定干脆。
#
#   用法：
#       ./test_mqtt_auth.sh                          # 交互输入口令
#       MQTT_PASS='xxx' ./test_mqtt_auth.sh          # 从环境取（不进 shell 历史）
#       ./test_mqtt_auth.sh -u ecas -P 'xxx' -H 127.0.0.1 -p 1883
#       ./test_mqtt_auth.sh --show                   # 只看 mosquitto 配置状态
#       ./test_mqtt_auth.sh --sub -W 130             # 追加：带口令订阅，真的等一条数据
#
#   ⚠️ 服务器上没有加密卷；口令在 mac 上现读后手工输入：
#         awk -F'"' '/^password/{print $2}' /Volumes/keys/ecas/mqtt.toml
#
#   退出码：0 = 认证已生效；1 = 认证未开（匿名可连）；2 = 参数/环境错误；3 = 无法判定
# ==============================================================================
set -u

HOST="127.0.0.1"; PORT=1883; USER_NAME="ecas"; USER_PASS=""
SELFTEST_TOPIC="ecas/selftest"; DATA_TOPIC="dpro66"
WAIT=3; SHOW_ONLY=0; DO_SUB=0
MOSQ_PUB="${MOSQ_PUB:-mosquitto_pub}"
MOSQ_SUB="${MOSQ_SUB:-mosquitto_sub}"
CONF="/etc/mosquitto/mosquitto.conf"
WRONG_PASS="__ecas_wrong_password__"
WRONG_USER="__ecas_no_such_user__"

usage() { sed -n '3,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while (( $# )); do
    case "$1" in
        -H|--host)  HOST="$2"; shift 2 ;;
        -p|--port)  PORT="$2"; shift 2 ;;
        -u|--user)  USER_NAME="$2"; shift 2 ;;
        -P|--pass)  USER_PASS="$2"; shift 2 ;;
        -t|--topic) DATA_TOPIC="$2"; shift 2 ;;
        -W|--wait)  WAIT="$2"; shift 2 ;;
        --conf)     CONF="$2"; shift 2 ;;
        --sub)      DO_SUB=1; shift ;;
        --show)     SHOW_ONLY=1; shift ;;
        -h|--help)  usage ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

line() { printf '%s\n' "--------------------------------------------------------------------------------"; }

# ---------------------------------------------------------------- 配置状态
show_state() {
    line; printf '%s\n' "配置状态"
    if [[ -f "$CONF" ]]; then
        printf '  配置文件       ：%s\n' "$CONF"
        printf '  监听           ：%s\n' "$(grep -nE '^[[:space:]]*(listener|port)' "$CONF" | tr '\n' ' ')"
        printf '  allow_anonymous：%s\n' "$(grep -nE '^[[:space:]]*allow_anonymous' "$CONF" || echo '（未设置 → 默认 true，匿名可连）')"
        printf '  password_file  ：%s\n' "$(grep -nE '^[[:space:]]*password_file' "$CONF" || echo '（未设置 → 没有任何可校验的账号）')"
    else
        printf '  配置文件       ：%s（不存在）\n' "$CONF"
    fi
    if [[ -f /etc/mosquitto/passwd ]]; then
        printf '  密码文件用户   ：%s\n' "$(cut -d: -f1 /etc/mosquitto/passwd | tr '\n' ' ')"
    else
        printf '  密码文件用户   ：（/etc/mosquitto/passwd 尚未创建）\n'
    fi
    printf '  防火墙         ：腾讯云控制台 + 宝塔面板都要放行 TCP %s\n' "$PORT"
    line
}
show_state
(( SHOW_ONLY )) && exit 0

# ------------------------------------------------- 依据配置预判（零改动旁路提示）
_pre_anon=""; _pre_nopw=""
[[ -f "$CONF" ]] && _pre_anon="$(grep -E '^[[:space:]]*allow_anonymous[[:space:]]+true' "$CONF" || true)"
[[ -f /etc/mosquitto/passwd ]] || _pre_nopw="1"
if [[ -n "$_pre_anon" && -n "$_pre_nopw" ]]; then
    printf '📋 预判（仅读配置）：allow_anonymous true ＋ 没有 password_file ⇒ **匿名必然连得上**，认证还没开。\n'
    printf '   下面的四组对照只是复核，真正的修复是跑 ./setup_mqtt_auth.sh。\n'
fi

# ---------------------------------------------------------------- 口令
if [[ -z "$USER_PASS" ]]; then
    if [[ -n "${MQTT_PASS:-}" ]]; then
        USER_PASS="$MQTT_PASS"
    else
        _tty=""
        if [[ -t 0 ]]; then _tty="/dev/stdin"
        elif [[ -c /dev/tty ]] && { : < /dev/tty; } 2>/dev/null; then _tty="/dev/tty"; fi
        for _try in 1 2 3; do
            [[ -z "$_tty" ]] && break
            read -r -s -p "请输入 MQTT 口令（用户 ${USER_NAME}）: " USER_PASS < "$_tty" 2>/dev/null || USER_PASS=""
            echo
            [[ -n "$USER_PASS" ]] && break
            printf '  ⚠️ 读到的是空口令（第 %d/3 次）。想跳过认证对照就按 Ctrl-C。\n' "$_try"
        done
    fi
fi
if [[ -z "$USER_PASS" ]]; then
    printf '⚠️ 未提供口令 —— 跳过「正确口令 / 错用户名」两项\n'
    printf '   两种给法：MQTT_PASS=*** %s   ｜   %s -P ***\n' "$0" "$0"
    printf '   口令在 mac 上现读：awk -F\\" '"'"'/^password/{print $2}'"'"' /Volumes/keys/ecas/mqtt.toml\n'
fi

command -v "$MOSQ_PUB" >/dev/null 2>&1 || { printf '❌ 找不到 %s（先装 mosquitto / mosquitto-clients）\n' "$MOSQ_PUB"; exit 2; }

# ---------------------------------------------------------------- 单项测试
RC=0; OUT=""
is_auth_reject() { printf '%s' "$OUT" | grep -qiE 'not authoris|not authoriz|not authorised'; }

run_pub() {
    if command -v timeout >/dev/null 2>&1; then
        OUT="$(timeout 6 "$MOSQ_PUB" -h "$HOST" -p "$PORT" -t "$SELFTEST_TOPIC" \
               -m 'ecas-auth-selftest' -i "ecas-auth-$$-$RANDOM" "$@" 2>&1)"; RC=$?
    else
        OUT="$("$MOSQ_PUB" -h "$HOST" -p "$PORT" -t "$SELFTEST_TOPIC" \
               -m 'ecas-auth-selftest' -i "ecas-auth-$$-$RANDOM" "$@" 2>&1)"; RC=$?
    fi
}

UNKNOWN=0; SAW_REJECT=0
verdict() {                        # verdict <期望: open|closed>
    local expect="$1"
    if [[ $RC -eq 0 ]]; then
        if [[ "$expect" == "open" ]]; then printf '  ✅ 通过（已授权）\n'
        else printf '  ❌ **不该通过却通过了** —— 认证没生效\n'; fi
    else
        if is_auth_reject; then
            SAW_REJECT=1
            if [[ "$expect" == "closed" ]]; then printf '  ✅ 被拒（not authorised）\n'
            else printf '  ❌ **正确口令也被拒** —— 查用户是否写进密码文件 / 是否重启过服务\n'; fi
        else
            printf '  ⚠️ 无法判定：既不是授权失败，也不是成功\n'; UNKNOWN=1
        fi
    fi
    printf '     输出：%s\n' "$(printf '%s' "$OUT" | head -2 | tr '\n' ' ')"
}

line; printf '%s\n' "测试 1/4 · 匿名（不带 -u/-P）"
run_pub; verdict closed

if [[ -n "$USER_PASS" ]]; then
    line; printf '测试 2/4 · 正确口令（-u %s）\n' "$USER_NAME"
    run_pub -u "$USER_NAME" -P "$USER_PASS"; verdict open

    line; printf '测试 3/4 · 错口令（-u %s）\n' "$USER_NAME"
    run_pub -u "$USER_NAME" -P "$WRONG_PASS"; verdict closed

    line; printf '测试 4/4 · 错用户名（口令正确）\n'
    run_pub -u "$WRONG_USER" -P "$USER_PASS"; verdict closed
fi

# ---------------------------------------------------------------- 可选：真数据
if (( DO_SUB )); then
    line; printf '附加测试 · 带口令订阅一次真实数据（%s，最多等 %s 秒）\n' "$DATA_TOPIC" "$WAIT"
    printf '  ⚠️ 本系统数据约 100 秒一条 —— 等待中出现「Timed out」是正常现象，不代表认证失败。\n'
    if [[ -n "$USER_PASS" ]]; then
        _o="$("$MOSQ_SUB" -h "$HOST" -p "$PORT" -u "$USER_NAME" -P "$USER_PASS" \
              -t "$DATA_TOPIC" -C 1 -W "$WAIT" -F '%I|%x' 2>&1)"; _rc=$?
    else
        _o="$("$MOSQ_SUB" -h "$HOST" -p "$PORT" -t "$DATA_TOPIC" -C 1 -W "$WAIT" -F '%I|%x' 2>&1)"; _rc=$?
    fi
    printf '  退出码=%s 输出：%s\n' "$_rc" "$(printf '%s' "$_o" | head -1)"
fi

# ---------------------------------------------------------------- 结论
line
if (( UNKNOWN )); then
    printf '📌 结论：**无法判定** —— 出现非授权类错误，先确认 broker 在跑、端口对、网络通。\n'
    exit 3
fi
if (( SAW_REJECT )); then
    printf '📌 结论：**认证已生效** ✅（匿名被拒；错口令/错用户名被拒）\n'
    printf '        此后订阅、发布都必须带账号：-u %s -P <口令>\n' "$USER_NAME"
    exit 0
else
    printf '📌 结论：**认证尚未开启**（匿名可连）\n'
    printf '        原因二选一：allow_anonymous 仍为 true；或没有 password_file。\n'
    printf '        此状态下「错口令」也会连上 —— 没有任何东西去校验它。\n'
    printf '        执行 ./setup_mqtt_auth.sh 开启认证后重跑本脚本，1/3/4 应变为「被拒」。\n'
    exit 1
fi
