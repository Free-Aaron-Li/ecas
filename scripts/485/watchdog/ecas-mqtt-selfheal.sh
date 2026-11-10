#!/usr/bin/env bash
# =============================================================================
# ECAS · MQTT 配置自愈（2026-11-10 浓度断流事故后新增）
#
# 事故：/opt/ecas/mqtt/{mosquitto.conf,passwd,acl.conf} 三份文件丢失，运行中的
#   broker 仍握着已删除句柄继续跑；一次重启（13:48 宕机 → 17:00 开机）后
#   Docker 把缺失的挂载源建成空目录，ecas-mosquitto 退出码 127，浓度数据断流 3h11m。
#
# 本脚本幂等：三份文件都在就什么都不做；缺了先按 backup/ 还原，再不行就用
#   服务器上现存的权威明文（/opt/ecas/credentials/mqtt-pass、rtu-master/mqtt-pass）
#   重新生成口令哈希。**口令值只走管道，绝不上屏/进 argv。**
#
# 调用：ecas-watchdog.sh 每 5 分钟发现缺失时调用；也可手动 root 执行。
# 退出码：0 = 配置齐全（含本次修好）；1 = 修不好，需人介入。
# =============================================================================
set -uo pipefail

MQTT_DIR=${MQTT_DIR:-/opt/ecas/mqtt}
BAK_DIR="$MQTT_DIR/backup"
IMG=${MQTT_IMG:-eclipse-mosquitto:2}
CRED_DIR=${CRED_DIR:-/opt/ecas/credentials}
RTU_PASS_FILE=${RTU_PASS_FILE:-/opt/ecas/rtu-master/mqtt-pass}
CTR=ecas-mosquitto
say(){ echo "[selfheal] $*"; }

missing=""
for f in mosquitto.conf passwd acl.conf; do
    [[ -f "$MQTT_DIR/$f" ]] || missing+="$f "
done
[[ -z "$missing" ]] && { say "三份配置齐全，无需处理"; exit 0; }
say "缺失：$missing"

# ── 1. 先清掉 Docker 因挂载源缺失而建出的空目录 ──────────────────────────────
for f in $missing; do
    p="$MQTT_DIR/$f"
    if [[ -d "$p" ]]; then
        if [[ -n "$(ls -A "$p" 2>/dev/null)" ]]; then say "拒绝：$f 是非空目录"; exit 1; fi
        rmdir "$p" || { say "删不掉空目录 $p（需要 root）"; exit 1; }
    fi
done

# ── 2. 能从 backup/ 还原就还原 ───────────────────────────────────────────────
restored=0
for f in $missing; do
    if [[ -f "$BAK_DIR/$f" ]]; then
        cp -a "$BAK_DIR/$f" "$MQTT_DIR/$f" && restored=$((restored+1)) && say "已从 backup 还原 $f"
    fi
done

# ── 3. 还缺 passwd ⇒ 用现存明文重建（值不上屏）──────────────────────────────
if [[ ! -f "$MQTT_DIR/passwd" ]]; then
    [[ -r "$CRED_DIR/mqtt-pass" && -r "$RTU_PASS_FILE" ]] || { say "缺 passwd 且读不到明文凭据"; exit 1; }
    docker run --rm --user 0:0 -v "$MQTT_DIR":/m -v "$CRED_DIR":/cred:ro \
        -v "$(dirname "$RTU_PASS_FILE")":/rtu:ro --entrypoint sh "$IMG" -c '
        set -eu
        MON=$(cat /cred/mqtt-pass); RTU=$(cat /rtu/mqtt-pass)
        printf "%s\n%s\n" "$RTU" "$RTU" | mosquitto_passwd -c /tmp/p ecas-rtu
        printf "%s\n%s\n" "$MON" "$MON" | mosquitto_passwd /tmp/p ecas-monitor
        install -m 0640 -o 1883 -g 1883 /tmp/p /m/passwd' >/dev/null 2>&1 \
        && say "已按现存凭据重建 passwd" || { say "重建 passwd 失败"; exit 1; }
fi

# ── 4. 还缺 conf / acl ⇒ 写入与 2026-11-10 修复版一致的模板 ──────────────────
if [[ ! -f "$MQTT_DIR/mosquitto.conf" ]]; then
    cat > "$MQTT_DIR/mosquitto.conf" <<'EOF'
# ECAS local broker (self-heal template 2026-11-10)
allow_anonymous false
password_file /mosquitto/config/passwd
acl_file /mosquitto/config/acl.conf
persistence true
persistence_location /mosquitto/data/
autosave_interval 60
log_dest stdout
connection_messages true

listener 1883 127.0.0.1
protocol mqtt

listener 8884 10.10.10.11
protocol mqtt
cafile /mosquitto/certs/ca.crt
certfile /mosquitto/certs/server.crt
keyfile /mosquitto/certs/server.key
tls_version tlsv1.2
EOF
    say "已写入 mosquitto.conf 模板"
fi
if [[ ! -f "$MQTT_DIR/acl.conf" ]]; then
    cat > "$MQTT_DIR/acl.conf" <<'EOF'
# ECAS local ACL: publisher write-only, collector read-only
user ecas-rtu
topic write dpro66

user ecas-monitor
topic read dpro66
EOF
    say "已写入 acl.conf 模板"
fi
chmod 0644 "$MQTT_DIR/mosquitto.conf" "$MQTT_DIR/acl.conf" 2>/dev/null

# ── 5. 拉起 broker 并验证监听 ────────────────────────────────────────────────
st=$(docker inspect -f '{{.State.Status}}' "$CTR" 2>/dev/null || echo missing)
case "$st" in
    running) docker restart "$CTR" >/dev/null 2>&1 ;;
    *)       docker start "$CTR"   >/dev/null 2>&1 ;;
esac
for _ in $(seq 1 10); do
    if ss -ltn 2>/dev/null | grep -qE ':1883[[:space:]]' && ss -ltn 2>/dev/null | grep -qE ':8884[[:space:]]'; then
        say "broker 已恢复监听 1883/8884"; exit 0
    fi
    sleep 1
done
say "broker 仍未监听，见 docker logs $CTR"
exit 1
