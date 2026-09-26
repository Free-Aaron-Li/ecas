#!/usr/bin/env bash
# ==============================================================================
# ECAS · MQTT 主题 ACL + 最小权限账号（服务器侧 · Mosquitto）
#
#   为什么需要它：DTU 不支持 TLS ⇒ 最后一段（4G→服务器）**无法加密**。
#   所以口径改成「限制爆炸半径 + 可发现冒名」：
#     · 认证（已开）      —— 不带账号连不上
#     · 授权（本脚本）    —— 每个账号只能碰自己那一个主题，别的主题读写全拒
#     · 分权（本脚本）    —— DTU 只能「写」，采集器只能「读」，互不越权
#     默认拒绝：ACL 里没列出的用户/主题一律拒绝（比现在「认证过就能读写任意主题」严得多）
#
#   账号角色：
#     ecas          管理/测试：可读写 dpro66（DTU 切换前也用它兜底）
#     dpro66-dtu    现场 DTU 专用：**只能写** dpro66（读不到任何东西，包括别人发的）
#     ecas-monitor  服务器采集器专用：**只能读** dpro66（发不了假数据）
#
#   位置：在【服务器】上运行（腾讯云轻量 124.222.122.146），需要 root
#   用法：
#       ./setup_mqtt_acl.sh --show                       # 只看当前状态，不改
#       ./setup_mqtt_acl.sh --dry-run                    # 零写入预览
#       ./setup_mqtt_acl.sh -y                           # 执行（新账号口令自动随机生成并打印一次）
#       MQTT_DTU_PASS=xx MQTT_MON_PASS=yy ./setup_mqtt_acl.sh -y    # 指定口令
#       ./setup_mqtt_acl.sh -y --rotate                  # 连已有账号的口令也重新生成
#       ./setup_mqtt_acl.sh -y --secrets-out /root/ecas/credentials/x.txt   # 只写文件、不上屏
#
#   ⚠️ 全程只用 SIGHUP 热加载（systemctl reload），**绝不 restart** ⇒ 不断 DTU 连接、不丢数据
#   ⚠️ 已建账号默认**不改口令**（幂等）；要换口令加 --rotate 或给环境变量
#   ⚠️ 口令不落工作区：只在这里打印一次（并写入服务器的加密凭据/systemd-creds）
# ==============================================================================
set -u

CONF="/etc/mosquitto/mosquitto.conf"
PASSWD_FILE="/etc/mosquitto/passwd"
ACL_FILE="/etc/mosquitto/acl.conf"
TOPIC="dpro66"
USER_ADMIN="ecas"
USER_DTU="dpro66-dtu"
USER_MON="ecas-monitor"
ASSUME_YES=0
SHOW_ONLY=0
DRY_RUN=0
ROTATE=0
SECRETS_OUT=""
NEW_CREDS=()
PASS_ADMIN="${MQTT_ADMIN_PASS:-}"
PASS_DTU="${MQTT_DTU_PASS:-}"
PASS_MON="${MQTT_MON_PASS:-}"

usage() { sed -n '3,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while (( $# )); do
    case "$1" in
        --topic)       TOPIC="$2"; shift 2 ;;
        --admin-user)  USER_ADMIN="$2"; shift 2 ;;
        --dtu-user)    USER_DTU="$2"; shift 2 ;;
        --mon-user)    USER_MON="$2"; shift 2 ;;
        --conf)        CONF="$2"; shift 2 ;;
        --passwd-file) PASSWD_FILE="$2"; shift 2 ;;
        --acl-file)    ACL_FILE="$2"; shift 2 ;;
        --admin-pass)  PASS_ADMIN="$2"; shift 2 ;;
        --dtu-pass)    PASS_DTU="$2"; shift 2 ;;
        --mon-pass)    PASS_MON="$2"; shift 2 ;;
        -y|--yes)      ASSUME_YES=1; shift ;;
        --show)        SHOW_ONLY=1; shift ;;
        --dry-run)     DRY_RUN=1; shift ;;
        --rotate)      ROTATE=1; shift ;;
        --secrets-out) SECRETS_OUT="$2"; shift 2 ;;
        -h|--help)     usage ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

say()  { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { echo "❌ $*" >&2; exit 1; }
ok()   { echo "✅ $*"; }

# ---------------------------------------------------------------- 随机口令
# 只用字母数字：部分 DTU 网页输入框对符号不友好；长度 32 足够
gen_pass() { python3 -c 'import secrets,string; a=string.ascii_letters+string.digits; print("".join(secrets.choice(a) for _ in range(32)))'; }

# ---------------------------------------------------------------- 当前状态
show_state() {
    echo "---------------------------------------------- 当前状态"
    echo "  配置文件       ：${CONF}$([[ -f $CONF ]] || echo ' （不存在！）')"
    echo "  allow_anonymous：$(grep -nE '^\s*allow_anonymous' "$CONF" 2>/dev/null || echo '（未设置）')"
    echo "  password_file  ：$(grep -nE '^\s*password_file' "$CONF" 2>/dev/null || echo '（未设置）')"
    echo "  acl_file       ：$(grep -nE '^\s*acl_file' "$CONF" 2>/dev/null || echo '❌（未设置 ⇒ 认证过即可读写任意主题）')"
    if [[ -f "$PASSWD_FILE" ]]; then
        echo "  账号           ：$(cut -d: -f1 "$PASSWD_FILE" | tr '\n' ' ')"
        echo "  口令文件属主   ：$(stat -c '%U:%G %a' "$PASSWD_FILE")（必须是 mosquitto:mosquitto 640，否则重启后连不上）"
    else
        echo "  口令文件       ：${PASSWD_FILE}（尚未创建）"
    fi
    if [[ -f "$ACL_FILE" ]]; then
        echo "  ACL 文件       ：${ACL_FILE}（$(wc -l < "$ACL_FILE") 行，属主 $(stat -c '%U:%G %a' "$ACL_FILE")）"
    else
        echo "  ACL 文件       ：${ACL_FILE}（尚未创建）"
    fi
    echo "  监听           ：$(ss -tln 2>/dev/null | grep -E ':(1883|8883)' | awk '{print $4}' | tr '\n' ' ')"
    echo "  当前连接       ："
    ss -tnp 2>/dev/null | grep -E ':1883|:8883' | sed 's/^/    /' || true
    echo "---------------------------------------------------------"
}

show_state
(( SHOW_ONLY )) && exit 0

# ---------------------------------------------------------------- 前置检查
if (( ! DRY_RUN )); then [[ "$(id -u)" == "0" ]] || fail "请用 root 运行（或 sudo）"; fi
[[ -f "$CONF" ]] || fail "配置文件不存在：${CONF}"
if ! command -v mosquitto_passwd >/dev/null 2>&1; then
    if (( DRY_RUN )); then echo "⚠️ 【dry-run】本机没有 mosquitto_passwd（在服务器上必须要有）"
    else fail "找不到 mosquitto_passwd —— 先装 mosquitto"; fi
fi
[[ -f "$PASSWD_FILE" ]] || fail "口令文件不存在：${PASSWD_FILE}（先跑 setup_mqtt_auth.sh）"

# ---------------------------------------------------------------- 计划
echo
echo "---------------------------------------------- 计划"
echo "  1) 写 ACL 文件      ：${ACL_FILE}"
echo "     · user ${USER_ADMIN}  → 读+写 ${TOPIC}"
echo "     · user ${USER_DTU}   → 只写 ${TOPIC}"
echo "     · user ${USER_MON}   → 只读 ${TOPIC}"
echo "     · 其余用户/主题     → 一律拒绝"
echo "  2) 配置里补         ：acl_file ${ACL_FILE}"
echo "  3) 账号             ：$([[ $ROTATE == 1 ]] && echo '三个都新建/改口令' || echo '已存在的不动，缺的才新建')"
echo "  4) 权限             ：口令文件与 ACL 文件 → mosquitto:mosquitto 640"
echo "  5) 生效方式         ：systemctl reload mosquitto（SIGHUP，不重启、不断连接）"
echo "  6) 新口令落点       ：${SECRETS_OUT:-（无 —— 会在屏幕上打印一次）}"
echo "----------------------------------------------"
if (( DRY_RUN )); then
    echo
    echo "【dry-run】以上动作均未执行。去掉 --dry-run 即真正落地。"
    exit 0
fi
if (( ! ASSUME_YES )); then
    printf '确认执行？输入 yes 继续：'
    read -r ans
    [[ "$ans" == "yes" || "$ans" == "y" || "$ans" == "Y" ]] || { echo "已取消（未做任何改动）"; exit 1; }
fi

TS="$(date +%Y%m%d-%H%M%S)"

# ---------------------------------------------------------------- 1) 备份
cp -a "$CONF" "${CONF}.bak-${TS}"
cp -a "$PASSWD_FILE" "${PASSWD_FILE}.bak-${TS}"
[[ -f "$ACL_FILE" ]] && cp -a "$ACL_FILE" "${ACL_FILE}.bak-${TS}"
ok "已备份：${CONF}.bak-${TS} ｜ ${PASSWD_FILE}.bak-${TS}"

# ---------------------------------------------------------------- 2) 账号
user_exists() { cut -d: -f1 "$PASSWD_FILE" | grep -qx "$1"; }

ensure_user() {   # $1=账号  $2=口令（空=自动生成）
    local u="$1" p="$2" out
    if user_exists "$u" && (( ! ROTATE )); then
        say "账号已存在，保持原口令：${u}"
        return 0
    fi
    if [[ -z "$p" ]]; then
        p="$(gen_pass)"
        NEW_CREDS+=("${u}:${p}")     # 只留在本进程内存里，最后打印一次
    fi
    mosquitto_passwd -b "$PASSWD_FILE" "$u" "$p" >/dev/null || fail "写账号失败：${u}"
    say "账号已写入：${u}$([[ -n $2 ]] && echo '（口令由你提供）' || echo '（口令新生成）')"
}

ensure_user "$USER_ADMIN" "$PASS_ADMIN"
ensure_user "$USER_DTU"   "$PASS_DTU"
ensure_user "$USER_MON"   "$PASS_MON"

# ---------------------------------------------------------------- 3) ACL 文件
cat > "$ACL_FILE" <<EOF
# ==============================================================================
# ECAS 浓度计 · Mosquitto 主题 ACL
#   由 scripts/485/setup_mqtt_acl.sh 生成于 ${TS}
#   口径：**默认拒绝** —— 未列出的用户/主题，读写一律拒绝
#   颜色/角色：${USER_ADMIN} 读写 ｜ ${USER_DTU} 只写（现场 DTU）｜ ${USER_MON} 只读（采集器）
# ==============================================================================

user ${USER_ADMIN}
topic read ${TOPIC}
topic write ${TOPIC}

user ${USER_DTU}
topic write ${TOPIC}

user ${USER_MON}
topic read ${TOPIC}
EOF
chmod 640 "$ACL_FILE"; chown mosquitto:mosquitto "$ACL_FILE"
ok "ACL 已写入：${ACL_FILE}"

# ---------------------------------------------------------------- 4) 配置里挂 acl_file
if ! grep -qE '^[[:space:]]*acl_file[[:space:]]' "$CONF"; then
    {
        echo ""
        echo "# ==== ECAS 浓度计 MQTT 主题 ACL（$(date '+%Y-%m-%d') 追加）===="
        echo "# 由 scripts/485/setup_mqtt_acl.sh 写入；改 ACL 后 systemctl reload mosquitto 即生效"
        echo "acl_file ${ACL_FILE}"
    } >> "$CONF"
    ok "已在 ${CONF} 追加 acl_file"
else
    say "配置里已有 acl_file，未改动"
fi

# 口令文件属主（mosquitto 会降权到 mosquitto 用户去读；root:root 600 会导致重启后起不来）
chown mosquitto:mosquitto "$PASSWD_FILE"; chmod 640 "$PASSWD_FILE"
ok "口令文件权限：$(stat -c '%U:%G %a' "$PASSWD_FILE")"

# ---------------------------------------------------------------- 5) 热加载 + 自检
before_pid="$(systemctl show -p MainPID --value mosquitto 2>/dev/null || echo 0)"
systemctl reload mosquitto || fail "reload 失败 —— 用 ${CONF}.bak-${TS} 回滚后 systemctl reload mosquitto"
sleep 1
systemctl is-active mosquitto >/dev/null || fail "❌ mosquitto 未在运行！立即回滚：cp -a ${CONF}.bak-${TS} ${CONF} && systemctl restart mosquitto"
after_pid="$(systemctl show -p MainPID --value mosquitto 2>/dev/null || echo 0)"
if [[ "$before_pid" == "$after_pid" && -n "$before_pid" && "$before_pid" != "0" ]]; then
    ok "PID 未变（${after_pid}）⇒ 是热加载，**DTU 的连接没被断开**"
else
    say "⚠️ PID 变化（${before_pid} → ${after_pid}）—— 请确认 DTU 是否已重连"
fi
ok "监听仍在：$(ss -tln | grep -E ':(1883|8883)' | awk '{print $4}' | tr '\n' ' ')"

# 看日志里有没有 ACL 语法错误
if [[ -f /var/log/mosquitto/mosquitto.log ]]; then
    tail -n 20 /var/log/mosquitto/mosquitto.log | grep -iE 'error|invalid' | tail -5 | sed 's/^/   日志：/' || true
fi

echo
echo "---------------------------------------------- 新生成的口令"
if (( ${#NEW_CREDS[@]} )); then
    if [[ -n "$SECRETS_OUT" ]]; then
        # 不打印：写进 root-only 文件（随后搬去加密卷 /Volumes/keys/ecas/，再从服务器删掉）
        { echo "# ECAS MQTT 账号口令 · 由 setup_mqtt_acl.sh 生成于 ${TS} · 抄完请删本文件"
          echo "# 用法：DTU 网页 → 通道-1 → 北向接口 → 设备账户/设备密码"
          for entry in "${NEW_CREDS[@]}"; do printf '%s %s\n' "${entry%%:*}" "${entry#*:}"; done
        } > "$SECRETS_OUT"
        chmod 600 "$SECRETS_OUT"
        ok "口令未打印，已写入：${SECRETS_OUT}（600）—— 请搬进加密卷后 shred"
    else
        for entry in "${NEW_CREDS[@]}"; do printf '  %-14s %s\n' "${entry%%:*}" "${entry#*:}"; done
        echo
        echo "  ⚠️ 把 DTU 的那一条抄进现场设备（网页 → 通道-1 → 北向接口 → 设备账户/设备密码），"
        echo "     并把它与采集器口令写入加密卷 /Volumes/keys/ecas/mqtt.toml —— 别留在终端里。"
        echo "     （更稳的用法：加 --secrets-out /root/ecas/credentials/xxx.txt，口令就不会出现在屏幕上）"
    fi
else
    echo "  （没有新口令：已有账号保持原口令）"
fi
echo "----------------------------------------------"
echo "下一步："
echo "  ① 现场 DTU 把账号改成 ${USER_DTU}（只写权限），ClientId 建议 ${USER_DTU}"
echo "  ② 采集器切到 ${USER_MON}（systemd-creds 重新加密 + systemctl restart ecas-monitor）"
echo "  ③ 验证：mosquitto_sub -h 127.0.0.1 -t ${TOPIC} -q 1 -C 1 -u ${USER_MON} -P <口令>   # 应收到一帧"
echo "  ④ 验证：同一口令发 ${TOPIC} 应被拒（只读账号不能发）"
echo "  ⑤ 回滚：cp -a ${CONF}.bak-${TS} ${CONF} && cp -a ${PASSWD_FILE}.bak-${TS} ${PASSWD_FILE} && systemctl reload mosquitto"
