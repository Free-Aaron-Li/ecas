#!/usr/bin/env bash
# ============================================================================
# ECAS 公网 MQTT 转发机（relay）部署 —— 腾讯云 124.222.122.146（ssh 别名 ecas）
#
#   目的（安全审查 H-01，2026-09-28 先生令「立刻按照建议解决」）：
#     现场 DTU 只会明文 MQTT、无法启用 TLS ⇒ 把「接受明文」从持有数据的 broker
#     上摘出来，交给本转发实例；转发实例不落任何数据，主 broker 从此不再监听明文。
#
#       现场 DTU --明文 8883--> [ecas-mqtt-relay：无持久化] --TLS 8884--> 主 broker
#
#   用法：  ./deploy-cloud-mqtt-relay.sh stage      # 渲染并安装 relay 配置/单元（不动现网监听）
#           ./deploy-cloud-mqtt-relay.sh test       # 临时 8885 口自检桥接（真机验证 TLS+账号）
#           ./deploy-cloud-mqtt-relay.sh swap       # 切换：主 broker 撤 8883 → relay 接管 8883
#           ./deploy-cloud-mqtt-relay.sh verify     # 自检（监听归属/桥接状态/数据是否继续入库）
#           ./deploy-cloud-mqtt-relay.sh status     # 看监听与两个服务的状态
#           ./deploy-cloud-mqtt-relay.sh logs       # 看 relay 日志
#           ./deploy-cloud-mqtt-relay.sh rollback   # 一键回滚（恢复主 broker 8883 + 停 relay）
#           ./deploy-cloud-mqtt-relay.sh all        # stage + test + swap + verify
#
#   来源白名单（2026-09-28 先生令「可以设定白名单」）：
#           ./deploy-cloud-mqtt-relay.sh fw stage      # 装 nft 规则文件 + unit（先 nft -c 校验，不生效）
#           ./deploy-cloud-mqtt-relay.sh fw on         # 启用并立刻生效（8883 只放行白名单来源）
#           ./deploy-cloud-mqtt-relay.sh fw off        # 关闭（8883 对所有来源开放 —— 仅排障用）
#           ./deploy-cloud-mqtt-relay.sh fw status     # 看 unit 状态 + 规则 + drop 计数
#           ./deploy-cloud-mqtt-relay.sh fw allow CIDR # 加白名单（改文件 + reload，运行期与文件一致）
#           ./deploy-cloud-mqtt-relay.sh fw deny CIDR  # 去白名单（同上）
#
#   凭据：桥接账号 ecas-bridge 的口令只在 /Volumes/keys 加密卷（ecas/mqtt.toml）与
#         macOS 钥匙串里；本脚本经 env 传入，绝不进 argv、绝不打印。
#         服务器上只有 /etc/mosquitto/relay/relay.conf（640 root:mosquitto）持有明文，
#         这是 mosquitto 桥接的硬要求，按「服务器 secret 文件源」惯例处理。
# ============================================================================
set -euo pipefail

HOST=${ECAS_CLOUD_SSH:-ecas}
MAIN_BROKER=${ECAS_MAIN_BROKER:-127.0.0.1:8884}   # 主 broker 与本转发实例同机 ⇒ 走回环（证书 SAN 含 IP:127.0.0.1）
CAFILE=${ECAS_MQTT_CAFILE:-/etc/mosquitto/certs/ca.crt}
BRIDGE_USER=${ECAS_BRIDGE_USER:-ecas-bridge}
MAIN_CONF=/etc/mosquitto/mosquitto.conf
RELAY_DIR=/etc/mosquitto/relay
UNIT=/etc/systemd/system/ecas-mqtt-relay.service
TEST_TOPIC=${ECAS_RELAY_TEST_TOPIC:-dpro66-bridge-test}
FW_NFT=/etc/nftables/ecas-relay.nft
FW_UNIT=/etc/systemd/system/ecas-relay-firewall.service
FW_SERVICE=ecas-relay-firewall.service
FW_SET=allowed_v4
VAULT_GET="$HOME/.files/dsh/bin/cred-keys-volume-sync.sh"

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
SRC="$REPO/web/deploy/cloud/mqtt-relay"
TPL="$SRC/relay-mosquitto.conf"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ControlPath=none)
remote() { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }
remote_stdin() { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }
die() { echo "✗ $*" >&2; exit 1; }

[ -f "$TPL" ] || die "找不到配置模板：$TPL"
[ -f "$SRC/relay-acl.conf" ] || die "找不到 ACL：$SRC/relay-acl.conf"
[ -f "$SRC/ecas-mqtt-relay.service" ] || die "找不到 unit：$SRC/ecas-mqtt-relay.service"
[ -f "$SRC/ecas-relay-firewall.nft" ] || die "找不到白名单规则：$SRC/ecas-relay-firewall.nft"
[ -f "$SRC/ecas-relay-firewall.service" ] || die "找不到白名单 unit：$SRC/ecas-relay-firewall.service"

# 桥接口令（env 传递，不上 argv、不打印）
bridge_pw() {
    local pw
    pw=$("$VAULT_GET" get ecas/mqtt.toml bridge_password) || die "取不到桥接口令（卷未挂载？）"
    [ "${#pw}" -ge 16 ] || die "桥接口令长度异常（${#pw}）"
    printf '%s' "$pw"
}

# 渲染 relay.conf：口令走 env，渲染结果直接管道给 ssh，本机不落盘
render() {
    local pw
    pw=$(bridge_pw)
    BRIDGE_PW="$pw" BRIDGE_USER="$BRIDGE_USER" MAIN_BROKER="$MAIN_BROKER" CAFILE="$CAFILE" TPL="$TPL" \
        python3 -c '
import os, pathlib, sys
t = pathlib.Path(os.environ["TPL"]).read_text(encoding="utf-8")
t = (t.replace("__BRIDGE_PASSWORD__", os.environ["BRIDGE_PW"])
      .replace("__BRIDGE_USER__", os.environ["BRIDGE_USER"])
      .replace("__MAIN_BROKER__", os.environ["MAIN_BROKER"])
      .replace("__CAFILE__", os.environ["CAFILE"]))
sys.stdout.write(t)'
}

cmd_stage() {
    echo "── 安装 relay 配置与 systemd 单元（不动现网监听）──"
    remote "install -d -m 750 -o mosquitto -g mosquitto $RELAY_DIR"
    scp -q "${SSH_OPTS[@]}" "$SRC/relay-acl.conf" "$HOST:$RELAY_DIR/acl.conf"
    scp -q "${SSH_OPTS[@]}" "$SRC/ecas-mqtt-relay.service" "$HOST:$UNIT"
    # 现场 DTU 的凭据：从主 broker 的口令文件里取同名记录（只搬哈希，不经明文）
    remote "grep '^dpro66-dtu:' /etc/mosquitto/passwd > $RELAY_DIR/passwd.tmp &&
            mv $RELAY_DIR/passwd.tmp $RELAY_DIR/passwd &&
            chmod 640 $RELAY_DIR/passwd &&
            chown mosquitto:mosquitto $RELAY_DIR/acl.conf $RELAY_DIR/passwd &&
            chmod 640 $RELAY_DIR/acl.conf"
    render | remote_stdin "umask 027; cat > $RELAY_DIR/relay.conf && chown root:mosquitto $RELAY_DIR/relay.conf && chmod 640 $RELAY_DIR/relay.conf"
    remote "systemctl daemon-reload && ls -l $RELAY_DIR $UNIT && echo '--- 桥接目标 ---' && grep -nE '^address|^remote_username|^bridge_cafile|^topic|^persistence|^listener' $RELAY_DIR/relay.conf"
}

cmd_test() {
    echo "── 桥接自检：临时 8885（仅本机）→ 主 broker $MAIN_BROKER（测试主题 $TEST_TOPIC）──"
    # 测试配置在本机渲染后管道给远端（口令走 env、不落本机磁盘、不进 argv）
    bridge_pw >/dev/null 2>&1 || die "取不到桥接口令"
    BRIDGE_PW="$(bridge_pw)" BRIDGE_USER="$BRIDGE_USER" MAIN_BROKER="$MAIN_BROKER" CAFILE="$CAFILE" TEST_TOPIC="$TEST_TOPIC" \
        python3 -c '
import os, sys
sys.stdout.write("""listener 8885 127.0.0.1
allow_anonymous true
persistence false
max_queued_messages 50
log_dest stderr
log_type all
connection ecas-main-selftest
address %(mb)s
bridge_cafile %(ca)s
bridge_insecure false
bridge_tls_version tlsv1.2
remote_username %(bu)s
remote_password %(bp)s
cleansession true
notifications false
restart_timeout 2 5
topic %(tt)s out 1
""" % {"mb": os.environ["MAIN_BROKER"], "ca": os.environ["CAFILE"],
       "bu": os.environ["BRIDGE_USER"], "bp": os.environ["BRIDGE_PW"],
       "tt": os.environ["TEST_TOPIC"]})' \
    | remote_stdin "umask 077; cat > /tmp/relay-selftest.conf && chmod 600 /tmp/relay-selftest.conf && echo '[test] 测试配置已就位（8885 仅本机 + 桥接到 $MAIN_BROKER 的 $TEST_TOPIC）'"
    remote bash -s -- "$TEST_TOPIC" <<'EOS'
set -uo pipefail
TOPIC=${1:-dpro66-bridge-test}
LOG=/tmp/relay-selftest.log
: > "$LOG"
/usr/sbin/mosquitto -c /tmp/relay-selftest.conf > "$LOG" 2>&1 &
PID=$!
sleep 6
mosquitto_pub -h 127.0.0.1 -p 8885 -t "$TOPIC" -q 1 -m "relay-selftest $(date -Is)" \
    && echo "[test] 已向 relay:8885 发布测试消息（qos1）"
sleep 5
echo "--- 桥接证据（判读：CONNACK (0) + Received PUBACK ⇒ TLS/账号/主 broker ACL 三关全通）---"
grep -aE "Connecting bridge|CONNACK|Sending PUBLISH|Received PUBACK|Bridge .* sending|error|Error|not authori" "$LOG" | tail -14 || true
kill $PID 2>/dev/null || true
sleep 1
pkill -f 'mosquitto -c /tmp/relay-selftest.conf' 2>/dev/null || true
rm -f /tmp/relay-selftest.conf
echo "[test] 测试实例已停（配置已删，日志留 $LOG 供查）"
EOS
}

cmd_swap() {
    echo "── 切换：主 broker 撤 8883 → relay 接管 8883 ──"
    remote bash -s <<EOS
set -euo pipefail
ts=\$(date +%Y%m%d-%H%M%S)
cp -p $MAIN_CONF $MAIN_CONF.bak-\$ts
echo "[swap] 主 broker 配置已备份：$MAIN_CONF.bak-\$ts"
python3 - <<'PY'
import pathlib, re
p = pathlib.Path("$MAIN_CONF")
t = p.read_text(encoding="utf-8")
n = len(re.findall(r'(?m)^listener 8883\s*$', t))
assert n == 1, f"期望恰好 1 处 'listener 8883'，实得 {n} 处（人工确认后再跑）"
t = re.sub(r'(?m)^listener 8883\s*\$',
           '# listener 8883 ← 2026-09-28 H-01：明文口移交 ecas-mqtt-relay（本 broker 不再接受明文）',
           t)
p.write_text(t, encoding="utf-8")
print("[swap] 已注释掉主 broker 的 8883 监听")
PY
systemctl restart mosquitto
systemctl enable --now ecas-mqtt-relay
sleep 3
echo "[swap] mosquitto=\$(systemctl is-active mosquitto) relay=\$(systemctl is-active ecas-mqtt-relay)"
ss -ltnp | grep -E ':8883|:8884|:1883' | sed 's/users:.*//'
EOS
}

cmd_verify() {
    echo "── 转发机自检 ──"
    remote bash -s <<EOS
set -uo pipefail
echo "① 监听："
ss -ltnp 2>/dev/null | grep -E ':8883|:8884|:1883' | sed 's/users:.*//' | sed 's/^/   /'
echo "② 服务： mosquitto=\$(systemctl is-active mosquitto)  ecas-mqtt-relay=\$(systemctl is-active ecas-mqtt-relay)"
echo "③ 8883 归属： \$(ss -ltnp 2>/dev/null | awk '/:8883 /{print \$NF}' | head -1)"
echo "④ 主 broker 是否还留明文口： \$(grep -cE '^listener 8883' $MAIN_CONF) （期望 0）"
echo "⑤ relay 日志（桥接相关）："
grep -aE "Connecting bridge|CONNACK|Connecting|error" /var/log/mosquitto/relay.log 2>/dev/null | tail -6 | sed 's/^/   /' || echo "   （暂无）"
echo "⑥ relay 日志尾部："
tail -4 /var/log/mosquitto/relay.log 2>/dev/null | sed 's/^/   /' || echo "   （暂无）"
EOS
    echo "── 数据面（Ubuntu 测试栈 PostgreSQL）──"
    ssh -o BatchMode=yes -o ConnectTimeout=10 -o ControlMaster=no -o ControlPath=none ubuntu-super \
        'docker exec ecas-db psql -U ecas -d ecas -tc "select count(*) as 行数, max(ts) as 最新 from raw_reading"' 2>/dev/null \
      || echo "（取不到数据库计数：改看测试栈采集器日志）"
    echo "── 采集器仍在消费（ubuntu-super 上 collector 日志尾部）──"
    ssh -o BatchMode=yes -o ConnectTimeout=10 -o ControlMaster=no -o ControlPath=none ubuntu-super \
        'docker logs --tail 4 ecas-collector 2>&1' 2>/dev/null || true
    echo "── relay 不落盘（persistence 必须 false；否则会覆盖主 broker 的 /var/lib/mosquitto/mosquitto.db）──"
    remote "grep -nE '^persistence|^autosave' /etc/mosquitto/relay/relay.conf"
}

cmd_status() {
    remote "systemctl is-active mosquitto ecas-mqtt-relay; ss -ltnp | grep -E ':8883|:8884|:1883' | sed 's/users:.*//'"
}

cmd_logs() { remote "tail -n ${1:-40} /var/log/mosquitto/relay.log"; }

cmd_rollback() {
    echo "── 回滚：停 relay，恢复主 broker 的 8883 明文口 ──"
    remote bash -s <<EOS
set -euo pipefail
systemctl disable --now ecas-mqtt-relay || true
latest=\$(ls -1t $MAIN_CONF.bak-* | head -1)
cp -p "\$latest" $MAIN_CONF
echo "[rollback] 已恢复 \$latest"
systemctl restart mosquitto
sleep 2
echo "[rollback] mosquitto=\$(systemctl is-active mosquitto) relay=\$(systemctl is-active ecas-mqtt-relay)"
ss -ltnp | grep -E ':8883|:8884|:1883' | sed 's/users:.*//'
EOS
}

# ── 来源白名单（8883 明文口收口：只放行现场 DTU 运营商段 + 回环）────────────────
cmd_fw_stage() {
    echo "── 安装 8883 来源白名单（nftables 规则 + systemd 单元，先校验不生效）──"
    remote "install -d -m 755 /etc/nftables"
    scp -q "${SSH_OPTS[@]}" "$SRC/ecas-relay-firewall.nft" "$HOST:$FW_NFT"
    scp -q "${SSH_OPTS[@]}" "$SRC/ecas-relay-firewall.service" "$HOST:$FW_UNIT"
    remote "chmod 644 $FW_NFT $FW_UNIT && systemctl daemon-reload && nft -c -f $FW_NFT && echo '✓ 规则校验通过（尚未生效）' && grep -n 'elements = ' $FW_NFT"
}

cmd_fw_on() {
    echo "── 启用白名单：8883 只放行白名单来源，其余 drop（限速记日志）──"
    remote "systemctl enable --now $FW_SERVICE >/dev/null 2>&1; systemctl is-enabled $FW_SERVICE; systemctl is-active $FW_SERVICE; nft list table inet ecas_relay"
}

cmd_fw_off() {
    echo "── 关闭白名单（仅排障用：关闭期间 8883 对所有来源开放）──"
    remote "systemctl disable --now $FW_SERVICE >/dev/null 2>&1 || true; nft delete table inet ecas_relay 2>/dev/null || true; echo '⚠️ 白名单已关闭'; nft list table inet ecas_relay 2>/dev/null || echo '（无 ecas_relay 表 = 确认关闭）'"
}

cmd_fw_status() {
    remote "printf 'unit enabled: '; systemctl is-enabled $FW_SERVICE 2>/dev/null || echo no
            printf 'unit active : '; systemctl is-active $FW_SERVICE 2>/dev/null || true
            echo '--- 规则与计数（counter 的 packets 即被拒次数）---'
            nft list table inet ecas_relay 2>/dev/null || echo '（无 ecas_relay 表 = 白名单未生效）'
            echo '--- 最近被拒来源（限速日志）---'
            journalctl -k --no-pager --since '24 hours ago' 2>/dev/null | grep -a 'ecas-relay-drop' | tail -5 || echo '（无）'"
}

# 改白名单：同时改规则文件的 elements 并 reload ⇒ 运行期与文件始终一致
fw_edit() {
    local action=$1 cidr=${2:-}
    [ -n "$cidr" ] || die "用法：fw $action <CIDR，例如 180.108.106.0/24>"
    case "$cidr" in
        *[!0-9a-fA-F:./]*) die "CIDR 含非法字符：$cidr" ;;
    esac
    remote bash -s -- "$action" "$cidr" "$FW_NFT" "$FW_SERVICE" <<'EOS'
set -euo pipefail
action=$1; cidr=$2; f=$3; svc=$4
python3 - "$action" "$cidr" "$f" <<'PY'
import re, sys
action, cidr, path = sys.argv[1:4]
t = open(path, encoding="utf-8").read()
m = re.search(r"elements = \{([^}]*)\}", t)
if not m:
    sys.exit("✗ 规则文件里找不到 elements = { … }")
items = [x.strip() for x in m.group(1).split(",") if x.strip()]
if action == "allow":
    if cidr in items:
        print(f"= {cidr} 已在白名单，无需改动"); sys.exit(0)
    items.append(cidr)
else:
    if cidr not in items:
        print(f"= {cidr} 不在白名单，无需改动"); sys.exit(0)
    if cidr == "127.0.0.1":
        sys.exit("✗ 拒绝移除 127.0.0.1（回环被挡会连累本机客户端与自检）")
    items.remove(cidr)
new = "        elements = { " + ", ".join(items) + " }"
t = t[:m.start()] + new + t[m.end():]
open(path, "w", encoding="utf-8").write(t)
print(f"✓ 白名单已{'加入' if action == 'allow' else '移除'} {cidr} ⇒ {', '.join(items)}")
PY
nft -c -f "$f"
if systemctl is-active --quiet "$svc"; then
    systemctl reload "$svc"
    echo "[fw] 已 reload ⇒ 运行期生效"
else
    echo "[fw] 单元未运行（尚未 fw on）⇒ 文件已改，下次 fw on 时生效"
fi
EOS
}

cmd_fw() {
    case "${1:-status}" in
        stage)  cmd_fw_stage ;;
        on)     cmd_fw_on ;;
        off)    cmd_fw_off ;;
        status) cmd_fw_status ;;
        allow)  fw_edit allow "${2:-}" ;;
        deny)   fw_edit deny "${2:-}" ;;
        *)      die "fw 子命令（可用：stage|on|off|status|allow CIDR|deny CIDR）" ;;
    esac
}

case "${1:-}" in
    stage)    cmd_stage ;;
    test)     cmd_test ;;
    swap)     cmd_swap ;;
    verify)   cmd_verify ;;
    status)   cmd_status ;;
    logs)     shift; cmd_logs "$@" ;;
    rollback) cmd_rollback ;;
    fw)       shift; cmd_fw "$@" ;;
    all)      cmd_stage; cmd_test; cmd_swap; cmd_verify ;;
    *)        die "未知子命令（可用：stage|test|swap|verify|status|logs|rollback|fw|all）" ;;
esac
