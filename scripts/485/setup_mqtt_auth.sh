#!/usr/bin/env bash
# ==============================================================================
# ECAS · MQTT 账号密码一键配置（服务器侧 · Mosquitto）
#
#   作用：把统一账号写进 Mosquitto，并关掉匿名登录
#         ⚠️ 账号取自 mac 挂载的加密卷 /Volumes/keys/ecas/mqtt.toml（现场读取，勿复制到工作区）
#   位置：在【服务器】上运行（腾讯云轻量 124.222.122.146），需要 root
#   用法：
#       ./setup_mqtt_auth.sh                          # 交互式（推荐）
#       ./setup_mqtt_auth.sh --user ecas --pass 'xxx'
#       ./setup_mqtt_auth.sh --user ecas --pass 'xxx' -y     # 免确认
#       ./setup_mqtt_auth.sh --show                   # 只看当前状态，不改
#
#   ⚠️⚠️ 执行前务必确认：DTU 那边已经填好「设备账户 / 设备密码 + ClientId」，
#        否则一关匿名，DTU 立刻连不上，数据就断了。
#        保险做法：先只 --show，再改 DTU，最后才跑这个脚本。
# ==============================================================================
set -u

CONF="/etc/mosquitto/mosquitto.conf"
PASSWD_FILE="/etc/mosquitto/passwd"
USER_NAME="ecas"
USER_PASS=""
ASSUME_YES=0
SHOW_ONLY=0
DRY_RUN=0

usage() { sed -n '3,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while (( $# )); do
    case "$1" in
        -u|--user)     USER_NAME="$2"; shift 2 ;;
        -p|--pass)     USER_PASS="$2"; shift 2 ;;
        --passwd-file) PASSWD_FILE="$2"; shift 2 ;;
        --conf)        CONF="$2"; shift 2 ;;
        -y|--yes)      ASSUME_YES=1; shift ;;
        --show)        SHOW_ONLY=1; shift ;;
        --dry-run)     DRY_RUN=1; shift ;;
        -h|--help)     usage ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

say()  { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { echo "❌ $*" >&2; exit 1; }

# ---------------------------------------------------------------- 当前状态
show_state() {
    echo "---------------------------------------------- 当前状态"
    if command -v mosquitto >/dev/null 2>&1; then
        echo "  mosquitto      ：$(mosquitto -h 2>&1 | head -1)"
    else
        echo "  mosquitto      ：❌ 未安装"
    fi
    echo "  配置文件       ：${CONF}$([[ -f $CONF ]] || echo ' （不存在！）')"
    if [[ -f "$CONF" ]]; then
        echo "  监听配置       ：$(grep -nE '^\s*(listener|port)' "$CONF" | tr '\n' ' ')"
        echo "  allow_anonymous：$(grep -nE '^\s*allow_anonymous' "$CONF" || echo '（未设置 → 默认 true 匿名可连）')"
        echo "  password_file  ：$(grep -nE '^\s*password_file' "$CONF" || echo '（未设置）')"
    fi
    if [[ -f "$PASSWD_FILE" ]]; then
        echo "  密码文件       ：${PASSWD_FILE} —— 用户：$(cut -d: -f1 "$PASSWD_FILE" | tr '\n' ' ')"
    else
        echo "  密码文件       ：${PASSWD_FILE}（尚未创建）"
    fi
    # ⭐ 一眼看出「为什么不带密码也能收数据」
    if grep -qE '^\s*allow_anonymous\s+true' "$CONF" 2>/dev/null && ! grep -qE '^\s*password_file' "$CONF" 2>/dev/null; then
        echo "  ⚠️ 当前状态     ：allow_anonymous true 且无 password_file ⇒ **匿名可连**，不带账号也能收数据。"
        echo "                   认证还没开 —— 这正是本脚本要改的（改完必须带 -u/-P 才能订阅）。"
    fi
    echo "  监听端口       ：$(ss -tln 2>/dev/null | grep -E ':1883' || echo '❌ 没有进程在听 1883')"
    echo "---------------------------------------------------------"
}

show_state
(( SHOW_ONLY )) && exit 0

# ---------------------------------------------------------------- 前置检查
if (( ! DRY_RUN )); then [[ "$(id -u)" == "0" ]] || fail "请用 root 运行（或 sudo）"; fi
if ! command -v mosquitto_passwd >/dev/null 2>&1; then
    if (( DRY_RUN )); then
        echo "⚠️ 【dry-run】本机没有 mosquitto_passwd（在服务器上必须要有）"
    else
        fail "找不到 mosquitto_passwd —— 先装 mosquitto（含 mosquitto-clients）"
    fi
fi
[[ -f "$CONF" ]] || fail "配置文件不存在：${CONF}"

# 允许从环境变量取密码 —— 避免口令出现在命令行参数里（会进 shell history 与进程表）
if [[ -z "$USER_PASS" && -n "${MQTT_PASS:-}" ]]; then USER_PASS="$MQTT_PASS"; fi

if [[ -z "$USER_PASS" ]]; then
    read -r -s -p "请输入 MQTT 密码（用户 ${USER_NAME}）: " USER_PASS; echo
    read -r -s -p "再输入一次确认: " P2; echo
    [[ "$USER_PASS" == "$P2" ]] || fail "两次输入不一致"
fi
[[ -n "$USER_PASS" ]] || fail "密码不能为空"

# ⚠️ 密码里不能有这些字符（DTU 的 AT 命令用引号包起来，会截断）
case "$USER_PASS" in
    *\"*|*,*|*\\*|*" "*) fail '密码不能包含 双引号 逗号 反斜杠 空格（DTU 的 AT 命令会被截断）' ;;
esac

if (( ! ASSUME_YES )); then
    echo
    echo "即将执行：把用户 ${USER_NAME} 写入 ${PASSWD_FILE}，并设置 allow_anonymous false"
    echo "⚠️ 如果 DTU 还没填账号密码，这一改它就会连不上、数据中断！"
    read -r -p "确定继续？(yes/N) " ans
    # ⭐ 接受 y / Y / yes（09-25 踩坑：先前只认字面 yes，输 y 会静默「已取消」，白跑一趟）
    case "$ans" in
        y|Y|yes|YES|Yes) ;;
        *) echo "已取消（需要输入 yes 或 y 才继续；本次没有改动任何配置）"; exit 0 ;;
    esac
fi

# ---------------------------------------------------------------- 写用户
if (( DRY_RUN )); then
    say "【dry-run】不写密码文件、不改配置、不重启"
    if [[ -f "$PASSWD_FILE" ]]; then
        say "  计划：mosquitto_passwd -b ${PASSWD_FILE} ${USER_NAME} ***"
    else
        say "  计划：mosquitto_passwd -c -b ${PASSWD_FILE} ${USER_NAME} ***"
    fi
elif [[ -f "$PASSWD_FILE" ]]; then
    say "更新密码文件中的用户 ${USER_NAME}（保留其它用户）"
    mosquitto_passwd -b "$PASSWD_FILE" "$USER_NAME" "$USER_PASS" || fail "mosquitto_passwd 失败"
else
    say "创建密码文件 ${PASSWD_FILE}（-c 会覆盖，这里是首次创建）"
    mosquitto_passwd -c -b "$PASSWD_FILE" "$USER_NAME" "$USER_PASS" || fail "mosquitto_passwd 失败"
fi
if (( ! DRY_RUN )); then
    chown mosquitto:mosquitto "$PASSWD_FILE" 2>/dev/null || true
    chmod 600 "$PASSWD_FILE"
fi

# ---------------------------------------------------------------- 写配置
# ⭐ 不「追加」而是「原地改写」：同一选项在文件里出现两次时取值次序有歧义
#    （本现场 mosquitto.conf 尾部已有一行 allow_anonymous true），就地改掉最稳。
if grep -qE '^\s*allow_anonymous\s+false' "$CONF"; then
    say "配置里已有 allow_anonymous false"
    if ! grep -qE '^\s*password_file' "$CONF"; then
        say "补上缺失的 password_file 行"
        [[ $DRY_RUN == 0 ]] && echo "password_file ${PASSWD_FILE}" >> "$CONF"
    fi
else
    if (( DRY_RUN )); then
        say "【dry-run】计划改动 ${CONF}："
        if grep -qE '^\s*allow_anonymous\s+true' "$CONF"; then
            echo "   $(grep -nE '^\s*allow_anonymous\s+true' "$CONF" | tr '\n' ' ')"
            echo "   → 原地改为 allow_anonymous false（不新增重复行）"
        else
            echo "   文件里没有 allow_anonymous 行 → 追加 allow_anonymous false"
        fi
        grep -qE '^\s*password_file' "$CONF" \
            && echo "   password_file 已存在，保持不变" \
            || echo "   追加 password_file ${PASSWD_FILE}"
    else
        say "改写 ${CONF}（先备份）"
        cp -a "$CONF" "${CONF}.bak-$(date +%Y%m%d-%H%M%S)"
        _mode="$(stat -c %a "$CONF" 2>/dev/null || stat -f %Lp "$CONF" 2>/dev/null || echo 644)"
        _tmp="$(mktemp)"
        awk -v pw="password_file ${PASSWD_FILE}" '
            /^[[:space:]]*allow_anonymous[[:space:]]+true[[:space:]]*$/ { sub(/true/,"false"); print; anon=1; next }
            /^[[:space:]]*allow_anonymous/                                    { anon=1; print; next }
            /^[[:space:]]*password_file/                                      { pws=1;  print; next }
            { print }
            END {
                if (!anon) print "allow_anonymous false";
                if (!pws)  print pw;
            }' "$CONF" > "$_tmp"
        chmod "$_mode" "$_tmp"
        mv "$_tmp" "$CONF"
        say "改完的实际内容："
        grep -nE '^\s*(allow_anonymous|password_file)' "$CONF" | sed 's/^/    /'
    fi
fi

# ---------------------------------------------------------------- 重启 + 验证
if (( DRY_RUN )); then
    say "【dry-run】到此为止 —— 没有写密码文件、没有改配置、没有重启。"
    exit 0
fi
say "重启 mosquitto"
systemctl restart mosquitto || fail "重启失败（journalctl -u mosquitto -n 50 看看）"
sleep 1
systemctl is-active --quiet mosquitto && say "mosquitto 状态：active ✅" || fail "mosquitto 没起来！检查 ${CONF}"

echo
say "监听端口："
ss -tlnp 2>/dev/null | grep ':1883' || echo "  ❌ 没有在听 1883"

echo
say "自测 ①：带账号订阅（本机）"
timeout 3 mosquitto_sub -h 127.0.0.1 -p 1883 -u "$USER_NAME" -P "$USER_PASS" -t 'dpro66/#' -v && echo "  ✅ 带账号可以订阅" || echo "  ⚠️ 没收到消息（3 秒内无数据也正常，只要能连上不报错就行）"

say "自测 ②：匿名应当被拒绝"
if timeout 3 mosquitto_sub -h 127.0.0.1 -p 1883 -t 'dpro66/#' -C 1 >/dev/null 2>&1; then
    echo "  ⚠️ 匿名竟然连上了 —— 检查 allow_anonymous / 是否被后面的配置覆盖"
else
    echo "  ✅ 匿名被拒绝（符合预期）"
fi

# ---------------------------------------------------------------- 收尾提醒
cat <<EOF

================================================================================
✅ 服务器侧完成。接下来：

 1) 【DTU 侧】浏览器进 DTU 配置页 →「参数设置」
      设备账户   ecas
      设备密码   （刚才输入的那个）
      ClientId   dpro66-01            ← 每台设备必须不同！
      推送主题1  开启，填  dpro66
    → 点「配置参数」→「重启设备」→「读取参数」回读核对

 2) 【监视脚本】现在必须带账号了：
      cat /Volumes/keys/ecas/mqtt.toml        # 现读现用
      ./dpro66_monitor.sh                          # 已支持 MQTT_USER/MQTT_PASS
      # 或显式： ./dpro66_monitor.sh -u $USER_NAME -P '<密码>' -t dpro66

 3) 【订阅抓包】同理：
      mosquitto_sub -h 127.0.0.1 -p 1883 -u $USER_NAME -P '<密码>' -t 'dpro66/#' -v

 4) 【别的设备】账号密码照抄这一套即可，只改 ClientId 和主题里的序号。

 回滚：配置备份在同目录 ${CONF}.bak-* ；
       若想恢复匿名，删掉 allow_anonymous false / password_file 两行后重启。
================================================================================
EOF
