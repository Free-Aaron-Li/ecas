#!/usr/bin/env bash
# =============================================================================
# ECAS 浓度计 · Ubuntu Super Server 测试栈部署脚本（在 Mac 上跑）
#
# 用法：
#   web/deploy/ubuntu/deploy.sh stage    只在本机组装构建上下文到 /tmp/ecas-stage
#   web/deploy/ubuntu/deploy.sh push     组装 + rsync 到 ubuntu-super:/opt/ecas（不含凭据）
#   web/deploy/ubuntu/deploy.sh creds    从密钥卷现读，落 /opt/ecas/credentials/*（600 root）
#   web/deploy/ubuntu/deploy.sh build    目标机上 docker compose build
#   web/deploy/ubuntu/deploy.sh up       安装 systemd 单元 + enable + 启动
#   web/deploy/ubuntu/deploy.sh check    端到端自检
#   web/deploy/ubuntu/deploy.sh backup   立刻做一次数据库备份（平时由 ecas-backup.timer 每天 03:30 跑）
#   web/deploy/ubuntu/deploy.sh watchdog 立刻跑一次看门狗并打印巡检结果（平时每 5 分钟自动跑）
#   web/deploy/ubuntu/deploy.sh all      push + creds + build + up + check
#   web/deploy/ubuntu/deploy.sh logs     看采集器日志
#
# 数据库唯一副本在本机 /data/ecas/pgdata（2026-09-26 起云端只做中转），
#   故备份落在 /data/ecas/backups/，保留 30 天。
#
# 凭据纪律：口令**只经 ssh 的 stdin**（首行 sudo 口令，余下脚本正文）→ 目标机
#   /opt/ecas/credentials/*（600，root）。本脚本不落本地明文文件、不进 argv、不打屏，
#   只打印 sha256[:12] 指纹供与密钥卷 README 比对。
#
# 环境变量：HOST（默认 ubuntu-super）｜VAULT（默认 /Volumes/keys）｜STAGE（默认 /tmp/ecas-stage）
# =============================================================================
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
HOST=${HOST:-ubuntu-super}
VAULT=${VAULT:-/Volumes/keys}
STAGE=${STAGE:-/tmp/ecas-stage}
REMOTE=/opt/ecas
MESH_HOST=${MESH_HOST:-10.10.10.11}   # 目标机在 easytier 里的地址
WEB_PORT=${WEB_PORT:-18080}           # 宿主机映射端口（该机 8080 被生产容器占了）

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ControlPath=none)
RSYNC_SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 -o ControlPath=none"

log() { printf '\n\033[1;36m── %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠️  %s\033[0m\n' "$*" >&2; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

ssh_run() { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }

# ── 密钥卷取标量（不打印值）──────────────────────────────────────────────────
vault_get() { # vault_get <相对路径> <键，支持 a.b>
    [ -r "$VAULT/$1" ] || die "密钥卷读不到：$VAULT/$1（挂载了吗？）"
    python3 - "$VAULT/$1" "$2" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    d = tomllib.load(f)
for part in sys.argv[2].split("."):
    d = d[part]
sys.stdout.write(str(d))
PY
}

# ── 以 root 在目标机执行 stdin 上的脚本 ──────────────────────────────────────
sudo_script() {
    local pw; pw=$(vault_get ssh/ssh.toml ubuntu_Super_Server)
    { printf '%s\n' "$pw"; cat; } | ssh "${SSH_OPTS[@]}" "$HOST" "sudo -S -p '' bash -s"
}

# ── ① 组装构建上下文 ─────────────────────────────────────────────────────────
cmd_stage() {
    log "组装构建上下文 → $STAGE"
    [ -d "$REPO/web/frontend/dist" ] || die "缺少 $REPO/web/frontend/dist，先在前端目录 npm run build"
    rm -rf "$STAGE"
    mkdir -p "$STAGE"/{backend,collector,backup,watchdog,sql,secrets,frontend-dist,systemd}

    cp "$REPO/web/deploy/ubuntu/backend/Dockerfile" \
       "$REPO/web/deploy/ubuntu/backend/entrypoint.sh" "$STAGE/backend/"
    cp "$REPO/web/backend/pyproject.toml" "$REPO/web/backend/uv.lock" \
       "$REPO/web/backend/README.md" "$STAGE/backend/"
    cp -R "$REPO/web/backend/app" "$STAGE/backend/app"
    rm -rf "$STAGE/backend/app/__pycache__"

    cp "$REPO/web/deploy/ubuntu/collector/Dockerfile" \
       "$REPO/web/deploy/ubuntu/collector/entrypoint.sh" \
       "$REPO/web/deploy/ubuntu/collector/mqtt_tls_feeder.py" "$STAGE/collector/"
    cp "$REPO/scripts/485/dpro66_monitor.sh" "$STAGE/collector/"   # 仓库里那一份，原样复用

    cp "$REPO/scripts/485/sql/001_init.sql" "$STAGE/sql/"
    cp -R "$REPO/web/frontend/dist/." "$STAGE/frontend-dist/"
    cp "$REPO/web/deploy/ecas-mqtt-ca.crt" "$STAGE/secrets/ca.crt"
    cp "$REPO/web/deploy/ubuntu/docker-compose.yml" "$STAGE/"
    cp "$REPO/web/deploy/ubuntu/systemd/ecas-stack.service" \
       "$REPO/web/deploy/ubuntu/systemd/ecas-backup.service" \
       "$REPO/web/deploy/ubuntu/systemd/ecas-backup.timer" \
       "$REPO/web/deploy/ubuntu/systemd/ecas-watchdog.service" \
       "$REPO/web/deploy/ubuntu/systemd/ecas-watchdog.timer" "$STAGE/systemd/"
    cp "$REPO/web/deploy/ubuntu/backup/ecas-backup.sh" "$STAGE/backup/"
    cp "$REPO/web/deploy/ubuntu/watchdog/ecas-watchdog.sh" "$STAGE/watchdog/"

    # 非敏感配置：口令/令牌不在这个文件里
    cat > "$STAGE/.env" <<'ENV'
# ECAS 测试栈 · 非敏感配置（口令与令牌在 /opt/ecas/credentials/*，600 root）
PG_USER=ecas
PG_NAME=ecas
MODEL_NAME=DPRO-66
OFFSET_MODE=add
OFFSET_CONC=0.48
MQTT_HOST=10.10.10.123
MQTT_PORT=8884
MQTT_TOPIC=dpro66
MQTT_USER=ecas-monitor
# 8884 未放行腾讯云安全组 ⇒ 走 mesh 连云端的 mesh 地址 10.10.10.123:8884。
# 该地址不在 server.crt 的 SAN 里 ⇒ 关主机名校验，改用 leaf 证书 SHA-256 指纹钉扎
# （CA 链校验仍开着）。云端重签 server.crt 后必须更新此值，否则采集器拒绝订阅。
# 取法：ssh ecas 'openssl x509 -in /etc/mosquitto/certs/server.crt -noout -fingerprint -sha256'
#   = 2026-09-26 签发的那张（有效期至 2036-09-23，序列号 40DE409E26651EAA73E88FA588B98402D90FE2B2）
MQTT_CERT_SHA256=B6:4B:8D:3A:37:E4:39:AA:61:0B:BD:02:D1:9C:66:0D:EB:4C:11:CD:C8:77:DE:93:1C:AE:0E:72:5D:84:6D:41
BIND_IP=10.10.10.11
# 宿主机 8080 已被生产容器 side.base.apigateway 占用；测试栈用 18080，仍只绑 mesh 地址
ECAS_WEB_PORT=18080
PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
# compose 从这里读 container secret 的源文件
CREDENTIALS_DIRECTORY=/opt/ecas/credentials
ENV
    printf '  ✓ %s 个文件，%s\n' "$(find "$STAGE" -type f | wc -l | tr -d ' ')" "$(du -sh "$STAGE" | cut -f1)"
    # 源文件在仓库里可能是 600（如 scripts/485/sql/001_init.sql），rsync -a 会保模式，
    # 而容器里的 postgres 用户（uid 70）必须能读 /docker-entrypoint-initdb.d/001_init.sql
    # ⇒ 统一放开成「目录 755 / 文件 644」；本目录里没有口令文件（凭据在 credentials/）
    chmod -R u=rwX,go=rX "$STAGE"
    chmod 755 "$STAGE"
}

# ── ② 推送 ───────────────────────────────────────────────────────────────────
cmd_push() {
    cmd_stage
    log "推送 → $HOST:$REMOTE（凭据目录与数据目录不动）"
    ssh_run "mkdir -p $REMOTE"
    rsync -a --delete --exclude credentials --exclude pgdata \
        -e "$RSYNC_SSH" "$STAGE"/ "$HOST:$REMOTE"/
    ssh_run "cd $REMOTE && ls -1 && echo '--- backend ---' && ls -1 backend && echo '--- collector ---' && ls -1 collector"
}

# ── ③ 落凭据 ─────────────────────────────────────────────────────────────────
cmd_creds() {
    log "落凭据 → $REMOTE/credentials（600 root-only）+ 数据盘目录"
    local mqtt_pass pg_pass auth_token internal_token
    mqtt_pass=$(vault_get ecas/mqtt.toml monitor_password)
    pg_pass=$(vault_get ecas/ubuntu-test.toml pg_password)
    auth_token=$(vault_get ecas/ubuntu-test.toml auth_token)
    # M-02：账号内部接口单独一把令牌，Worker 经 VPC 服务绑定用它；对外令牌泄露也管不了账号
    internal_token=$(vault_get ecas/ubuntu-test.toml internal_token)
    # 值里若带单引号会破坏内嵌引号，先挡住（本卷口令按约定不含引号/逗号/反斜杠/空格）
    case "$mqtt_pass$pg_pass$auth_token$internal_token" in
        *"'"*) die "凭据含单引号，脚本内嵌不安全，请改 sudo_script 传值方式" ;;
    esac

    sudo_script <<EOF
set -euo pipefail
umask 077
install -d -m 700 -o root -g root $REMOTE/credentials
printf '%s' '$pg_pass'        > $REMOTE/credentials/pg-password
printf '%s' '$auth_token'     > $REMOTE/credentials/auth-token
printf '%s' '$internal_token' > $REMOTE/credentials/internal-token
printf '%s' '$mqtt_pass'      > $REMOTE/credentials/mqtt-pass
chmod 600 $REMOTE/credentials/*
chown root:root $REMOTE/credentials/*
echo "── 权限 ──"; ls -ld $REMOTE/credentials; ls -l $REMOTE/credentials
echo "── 指纹 sha256[:12]（与密钥卷 README 比对）──"
for f in pg-password auth-token internal-token mqtt-pass; do
    printf '  %-14s %s\n' "\$f" "\$(sha256sum $REMOTE/credentials/\$f | cut -c1-12)"
done
# 容器里 postgres 用户 uid=70 ⇒ 数据目录给他
mkdir -p /data/ecas/pgdata
chown 70:70 /data/ecas/pgdata
chmod 700 /data/ecas/pgdata
echo "── 数据盘 ──"; ls -ld /data/ecas/pgdata; df -hT /data | tail -1
EOF
}

# ── ④ 构建镜像 ───────────────────────────────────────────────────────────────
cmd_build() {
    log "目标机构建镜像（首次几分钟）"
    ssh_run "cd $REMOTE && docker compose build 2>&1 | tail -12"
}

# ── ⑤ 装单元并启动 ───────────────────────────────────────────────────────────
cmd_up() {
    log "安装 systemd 单元 + enable + 启动"
    sudo_script <<EOF
set -euo pipefail
install -m 644 -o root -g root $REMOTE/systemd/ecas-stack.service /etc/systemd/system/ecas-stack.service
install -m 644 -o root -g root $REMOTE/systemd/ecas-backup.service /etc/systemd/system/ecas-backup.service
install -m 644 -o root -g root $REMOTE/systemd/ecas-backup.timer   /etc/systemd/system/ecas-backup.timer
install -m 644 -o root -g root $REMOTE/systemd/ecas-watchdog.service /etc/systemd/system/ecas-watchdog.service
install -m 644 -o root -g root $REMOTE/systemd/ecas-watchdog.timer   /etc/systemd/system/ecas-watchdog.timer
install -m 755 -o root -g root $REMOTE/watchdog/ecas-watchdog.sh /opt/ecas/watchdog/ecas-watchdog.sh.new && mv -f /opt/ecas/watchdog/ecas-watchdog.sh.new /opt/ecas/watchdog/ecas-watchdog.sh
install -m 755 -o root -g root $REMOTE/backup/ecas-backup.sh /opt/ecas/backup/ecas-backup.sh.new && mv -f /opt/ecas/backup/ecas-backup.sh.new /opt/ecas/backup/ecas-backup.sh
systemctl daemon-reload
systemctl enable ecas-stack
systemctl restart ecas-stack
systemctl enable --now ecas-backup.timer
systemctl enable --now ecas-watchdog.timer
sleep 8
echo "── 单元状态 ──"
systemctl is-enabled ecas-stack
systemctl is-active ecas-stack || true
echo "备份定时器：\$(systemctl is-enabled ecas-backup.timer) / \$(systemctl is-active ecas-backup.timer) ｜ 下次 \$(systemctl show -p NextElapseUSecRealtime --value ecas-backup.timer)"
echo "看门狗定时器：\$(systemctl is-enabled ecas-watchdog.timer) / \$(systemctl is-active ecas-watchdog.timer) ｜ 下次 \$(systemctl show -p NextElapseUSecMonotonic --value ecas-watchdog.timer)"
cd $REMOTE && docker compose ps
EOF
}

# ── ⑤c 立刻跑一次看门狗 ──────────────────────────────────────────────────────
cmd_watchdog() {
    log "在目标机上跑一次看门狗（root 身份，与定时器一致）"
    sudo_script <<'EOF'
set -uo pipefail
/opt/ecas/watchdog/ecas-watchdog.sh
echo "── 近期状态变化事件（最近 10 条）──"
tail -10 /var/log/ecas/watchdog.jsonl 2>/dev/null || echo "  （暂无：一直是正常的）"
EOF
}

# ── ⑤b 立刻备份一次 ──────────────────────────────────────────────────────────
cmd_backup() {
    log "在目标机上跑一次数据库备份（root 身份，与定时器一致）"
    sudo_script <<EOF
set -euo pipefail
$REMOTE/backup/ecas-backup.sh
ls -lh /data/ecas/backups/ | tail -4
EOF
}

# ── ⑥ 自检 ───────────────────────────────────────────────────────────────────
cmd_check() {
    log "① 容器"
    ssh_run "cd $REMOTE && docker compose ps"

    log "② 端口只绑 mesh（应只见 ${MESH_HOST}:${WEB_PORT}，绝无 0.0.0.0:${WEB_PORT}）"
    ssh_run "ss -lnt 2>/dev/null | grep -E ':${WEB_PORT}\b' || echo '（没有 ${WEB_PORT} 监听？！）'"

    log "③ 后端健康（经 mesh 从本机访问 http://${MESH_HOST}:${WEB_PORT}）"
    curl -sS -m 8 -o /dev/null -w '   无令牌 GET /healthz → HTTP %{http_code}\n' \
        "http://${MESH_HOST}:${WEB_PORT}/healthz" || warn "本机经 mesh 访问失败，检查 easytier 路由"

    log "④ 令牌是否生效（无令牌应 401，带令牌应 200）"
    local token
    token=$(vault_get ecas/ubuntu-test.toml auth_token)
    curl -sS -m 8 -o /dev/null -w '   无令牌 GET /api/readings  → HTTP %{http_code}（应 401）\n' \
        "http://${MESH_HOST}:${WEB_PORT}/api/readings" || true
    printf 'header = "Authorization: Bearer %s"\n' "$token" \
        | curl -sS -m 8 -K - -o /dev/null -w '   带令牌 GET /api/readings → HTTP %{http_code}（应 200）\n' \
              "http://${MESH_HOST}:${WEB_PORT}/api/readings" || true

    log "⑤ 库里数据 + 换算抽查（浓度 = (raw-4000)/160 + 0.48）"
    ssh_run "cd $REMOTE && docker compose exec -T db psql -U ecas -d ecas -c \
        \"select count(*) as 行数, min(ts) as 最早, max(ts) as 最新 from raw_reading;\" && \
        docker compose exec -T db psql -U ecas -d ecas -c \
        \"select ts, raw, round((raw-4000)/160.0+0.48, 2) as 浓度 from raw_reading order by ts desc limit 5;\""

    log "⑥ 采集器日志（TLS/ACL 有无报错）"
    ssh_run "cd $REMOTE && docker compose logs --tail=15 collector"
}

cmd_logs() { ssh_run "cd $REMOTE && docker compose logs -f --tail=30 collector"; }

cmd_all() { cmd_push; cmd_creds; cmd_build; cmd_up; sleep 20; cmd_check; }

case "${1:-}" in
    stage) cmd_stage ;;
    push)  cmd_push ;;
    creds) cmd_creds ;;
    build) cmd_build ;;
    up)    cmd_up ;;
    check) cmd_check ;;
    backup) cmd_backup ;;
    watchdog) cmd_watchdog ;;
    logs)  cmd_logs ;;
    all)   cmd_all ;;
    *) sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
