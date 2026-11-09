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
#   web/deploy/ubuntu/deploy.sh preflight 只读体检：密钥卷、ssh、sudo 口令、与线上凭据指纹比对
#
# 数据库唯一副本在本机 /data/ecas/pgdata（2026-09-26 起云端只做中转），
#   故备份落在 /data/ecas/backups/，保留 30 天。
#
# 凭据纪律：口令**只经 ssh 的 stdin**（首行 sudo 口令，余下脚本正文）→ 目标机
#   /opt/ecas/credentials/*（600，root）。本脚本不落本地明文文件、不进 argv、不打屏，
#   只打印 sha256[:12] 指纹供与密钥卷 README 比对。
#
# 凭据卷不是每台机器都挂（/Volumes/keys 是外接卷）。没有它时的行为，各子命令不一样：
#   creds / backup / watchdog   做不到 —— 它们本质上要 root 口令，直接报错走人
#   up                          回落到「用户 ubuntu 直连 docker compose up」
#                               （该用户在 docker 组，等效于单元的 ExecStart；
#                                但**不装、不更新 systemd 单元与定时器**，那是 root 才能做的事）
#   check                       照跑，只跳过「带令牌应 200」那一项（无令牌 401 已够说明鉴权开着）
#   all                         跳过 creds 并大声警告（凭据已就位时这一步本来就是幂等的）
#   stage / push / build / logs 完全不受影响
# 所以只改后端再上线，其实一条 `push` + `build` +（无卷时）`up` 就够了。
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

# 凭据卷在不在：ssh/ssh.toml 是唯一存放目标机 root 口令的地方。
# 它决定 up / check / all 走哪条路，见文件头「凭据卷不是每台机器都挂」那一段。
VAULT_OK=0
if [ -r "$VAULT/ssh/ssh.toml" ]; then
    VAULT_OK=1
fi

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

# ── sha256[:12]（只为比对指纹，绝不回显原值）─────────────────────────────────
hash12() { # hash12 <值>
    if command -v shasum >/dev/null 2>&1; then
        printf '%s' "$1" | shasum -a 256 | cut -c1-12
    else
        printf '%s' "$1" | sha256sum | cut -c1-12
    fi
}

# ── 必须先有凭据卷的子命令，在动手之前就挡下来（说清为什么，而不是深处才报错）────
vault_need() { # vault_need <子命令名>：没有 /Volumes/keys 就带原因退出
    if [ "$VAULT_OK" != 1 ]; then
        die "「$1」需要凭据卷（$VAULT/ssh/ssh.toml 读不到）才能拿到目标机的 root 口令。
     挂载后重跑；不需要 root 的部分可以单独跑 push / build / logs。"
    fi
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
    mkdir -p "$STAGE"/{backend,collector,backup,watchdog,sql,secrets,frontend-dist,systemd,flow}

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

    # 全部迁移脚本（001 建库表、004 建 flow_reading …）：只推文件，DDL 仍由人工/部署步骤执行
    cp "$REPO/scripts/485/sql/"*.sql "$STAGE/sql/"
    # 流量计入库器：宿主机常驻（systemd unit ecas-flow-ingest.service），不进容器
    cp "$REPO/scripts/485/flow_ingest.py" "$STAGE/flow/"
    cp -R "$REPO/web/frontend/dist/." "$STAGE/frontend-dist/"
    cp "$REPO/web/deploy/ecas-mqtt-ca.crt" "$STAGE/secrets/ca.crt"
    cp "$REPO/web/deploy/ubuntu/docker-compose.yml" "$STAGE/"
    cp "$REPO/web/deploy/ubuntu/systemd/ecas-stack.service" \
       "$REPO/web/deploy/ubuntu/systemd/ecas-backup.service" \
       "$REPO/web/deploy/ubuntu/systemd/ecas-backup.timer" \
       "$REPO/web/deploy/ubuntu/systemd/ecas-watchdog.service" \
       "$REPO/web/deploy/ubuntu/systemd/ecas-watchdog.timer" \
       "$REPO/web/deploy/ubuntu/systemd/ecas-flow-ingest.service" "$STAGE/systemd/"
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
# 2026-11-03 起 broker 在本机（ecas-mosquitto）：1883 只听回环，8884/TLS 听 10.10.10.11；
# 云端 10.10.10.123 只做 DTU 的内核转发，8884 上早已没有 broker 监听。
# ⛔ 不要写云端 mesh 地址 10.10.10.123：collector 连过去会被立刻 EOF
#    （2026-11-03 断采 19 小时、2026-11-04 又断 15 分钟，都是这一行写错）。
MQTT_HOST=10.10.10.11
MQTT_PORT=8884
MQTT_TOPIC=dpro66
MQTT_USER=ecas-monitor
# server.crt 沿用 2026-09-26 签发的那张（SAN 只有 124.222.122.146/127.0.0.1/DNS，**没有** mesh 地址）
# ⇒ 关掉主机名校验、改用 leaf 证书 SHA-256 指纹钉扎（CA 链校验照旧）；换证书必须同步改这里，
# 否则采集器 fail closed 拒绝订阅。
# 取法：ssh ubuntu-super 'openssl x509 -in /opt/ecas/mqtt/certs/server.crt -noout -fingerprint -sha256'
MQTT_CERT_SHA256=B6:4B:8D:3A:37:E4:39:AA:61:0B:BD:02:D1:9C:66:0D:EB:4C:11:CD:C8:77:DE:93:1C:AE:0E:72:5D:84:6D:41
BIND_IP=127.0.0.1
# 2026-10-26 先生令：网站只允许先生本人这台 Mac 访问，其他一律禁止
#   ⇒ 只绑回环；唯一入口＝本机 launchd SSH 隧道 com.ecas.tunnel（-L 18080:127.0.0.1:18080）。
#   ⇒ 叠加网其它成员 / 公网 / VPC 隧道一律连不上；改回 10.10.10.11 即重新对叠加网开放。
# 宿主机 8080 已被生产容器 side.base.apigateway 占用；测试栈用 18080
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
    log "推送 → ${HOST}:${REMOTE}（凭据目录与数据目录不动）"
    ssh_run "mkdir -p $REMOTE"
    # ⚠️ `rtu-master` / `flow` / `mqtt` 是**只在目标机上存在**的运行时目录（采集器代码、入库断点、
    #    mosquitto 证书与消息库），都不在 STAGE 里；不加 exclude，`--delete` 会把它们一起删掉
    #    （2026-11-04 实测：`mqtt/certs/*.key` 属 root，删不动 ⇒ 整个 push 以非零退出）。
    rsync -a --delete --exclude credentials --exclude pgdata \
        --exclude rtu-master --exclude flow --exclude mqtt \
        --exclude .env \
        -e "$RSYNC_SSH" "$STAGE"/ "$HOST:$REMOTE"/
    # `.env` 是**目标机侧**配置（MQTT_HOST 指向哪台 broker 因机器而异），模板只用于首次落地：
    # 旧版没排除它 ⇒ 每次 push 都用模板覆盖，2026-11-04 把手工改对的
    # `MQTT_HOST=10.10.10.11` 覆盖回云端 10.10.10.123，collector 随即 TLS 失败断采 15 分钟。
    if ssh_run "test -f $REMOTE/.env"; then
        log "  .env 已存在 ⇒ 保留目标机版本（不覆盖）"
    else
        ssh_run "cat > $REMOTE/.env" < "$STAGE/.env"
        log "  .env 缺失 ⇒ 已落模板（改 broker 地址请改目标机上的 .env，别改这里）"
    fi
    ssh_run "cd $REMOTE && ls -1 && echo '--- backend ---' && ls -1 backend && echo '--- collector ---' && ls -1 collector"
}

# ── ③ 落凭据 ─────────────────────────────────────────────────────────────────
cmd_creds() {
    vault_need "落凭据"
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
    if [ "$VAULT_OK" != 1 ]; then
        # 没有 root 口令 ⇒ 装不了单元。退而求其次：用目标机上的 ubuntu 用户直接
        # 拉容器。该用户在 docker 组，所以与单元的 ExecStart 等效；差别只在
        # 「不落单元文件、不动 enable 状态、不重载定时器」。
        warn "凭据卷没挂（$VAULT/ssh/ssh.toml 读不到）⇒ 回落到直连 docker"
        warn "  能做的：docker compose up -d --remove-orphans"
        warn "  做不到：安装/更新 systemd 单元与定时器、systemctl enable（都要 root）"
        warn "  若这次改动涉及 systemd/ 或定时器文件，必须挂上卷再跑 up，否则改动不生效"
        log "直连 docker 拉起容器（${HOST}:${REMOTE}）"
        ssh_run "cd $REMOTE && docker compose up -d --remove-orphans && docker compose ps"
        return 0
    fi

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
    vault_need "立刻跑看门狗"
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
    vault_need "立刻备份"
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

    log "② 端口只绑回环（应只见 127.0.0.1:${WEB_PORT}，绝无 0.0.0.0:${WEB_PORT} —— 2026-10-26 安全收口）"
    ssh_run "ss -lnt 2>/dev/null | grep -E ':${WEB_PORT}\b' || echo '（没有 ${WEB_PORT} 监听？！）'"

    log "③ 后端健康（经本机 SSH 隧道 http://127.0.0.1:${WEB_PORT}，隧道由 launchd com.ecas.tunnel 维持）"
    curl -sS -m 8 -o /dev/null -w '   无令牌 GET /healthz → HTTP %{http_code}\n' \
        "http://127.0.0.1:${WEB_PORT}/healthz" || warn "隧道不通：launchctl print gui/$(id -u)/com.ecas.tunnel 看状态"

    log "④ 令牌是否生效（无令牌应 401，带令牌应 200）"
    curl -sS -m 8 -o /dev/null -w '   无令牌 GET /api/readings  → HTTP %{http_code}（应 401）\n' \
        "http://127.0.0.1:${WEB_PORT}/api/readings" || true
    if [ "$VAULT_OK" = 1 ]; then
        local token
        token=$(vault_get ecas/ubuntu-test.toml auth_token)
        printf 'header = "Authorization: Bearer %s"\n' "$token" \
        | curl -sS -m 8 -K - -o /dev/null -w '   带令牌 GET /api/readings → HTTP %{http_code}（应 200）\n' \
              "http://${MESH_HOST}:${WEB_PORT}/api/readings" || true

    else
        warn "   凭据卷没挂 ⇒ 跳过「带令牌应 200」这一项（无令牌 401 已经说明鉴权是开着的）"
    fi

    log "⑤ 库里数据 + 换算抽查（浓度 = (raw-4000)/160 + 0.48）"
    ssh_run "cd $REMOTE && docker compose exec -T db psql -U ecas -d ecas -c \
        \"select count(*) as 行数, min(ts) as 最早, max(ts) as 最新 from raw_reading;\" && \
        docker compose exec -T db psql -U ecas -d ecas -c \
        \"select ts, raw, round((raw-4000)/160.0+0.48, 2) as 浓度 from raw_reading order by ts desc limit 5;\""

    # 这一条是 2026-11-03（断 19 小时）/ 11-04（断 15 分钟）两次事故的体检项：
    # 「容器 Up + 网页 200」都不代表在采数，只有 raw_reading 的最新一条才会说话。
    log "⑥ 采集新鲜度（最新一条距今 < 300 秒才算在采数）"
    local lag
    lag=$(ssh_run "cd $REMOTE && docker compose exec -T db psql -U ecas -d ecas -tAc \
        \"select coalesce(extract(epoch from now() - max(ts))::int, -1) from raw_reading;\"" 2>/dev/null | tr -dc '0-9-')
    if [ -n "$lag" ] && [ "$lag" -ge 0 ] && [ "$lag" -lt 300 ]; then
        printf '   采集正常：最新读数距今 %ss\n' "$lag"
    else
        warn "   采集断了：最新读数距今 ${lag:-?}s ⇒ 先查 /opt/ecas/.env 的 MQTT_HOST 是否为 10.10.10.11（云端 10.10.10.123 自 2026-11-03 起已无 broker，连过去会 EOF）"
    fi

    log "⑦ 采集器日志（TLS/ACL 有无报错）"
    ssh_run "cd $REMOTE && docker compose logs --tail=15 collector"
}

cmd_logs() { ssh_run "cd $REMOTE && docker compose logs -f --tail=30 collector"; }

cmd_all() {
    if [ "$VAULT_OK" = 1 ]; then
        cmd_push; cmd_creds; cmd_build; cmd_up; sleep 20; cmd_check
    else
        warn "凭据卷没挂 ⇒ all 跳过 creds（凭据已就位时这一步本来就是幂等的）"
        warn "  本次只做：push + build + up（直连 docker）+ check"
        cmd_push; cmd_build; cmd_up; sleep 20; cmd_check
    fi
}

# ── ⓪ 只读体检：把「会挡住部署」的前置条件一次问清楚 ────────────────────────
# 故意只读：不写远端文件、不重启任何东西、不打印任何口令值（只打 sha256[:12]）。
# 存在的理由：creds 会把线上凭据换成卷里的值 —— 值不一致时那是**静默轮换**，
# 会把 Worker/看板手上的令牌弄失效。跑 all 之前先在这里看一眼，比事后排障便宜。
cmd_preflight() {
    log "① 凭据卷"
    if [ "$VAULT_OK" = 1 ]; then
        echo "  ✓ $VAULT/ssh/ssh.toml 可读 ⇒ creds / backup / watchdog / up 都能走完整路径"
    else
        warn "$VAULT/ssh/ssh.toml 读不到"
        warn "  creds / backup / watchdog 会拒绝执行；up 回落到直连 docker；check 跳过带令牌那一项"
    fi

    log "② ssh 可达性 + 目标机的 docker compose"
    ssh_run "printf '  ✓ %s / %s\n' \"\$(hostname)\" \"\$(id -un)\"" || die "ssh $HOST 不通"
    ssh_run "docker compose version >/dev/null 2>&1 && echo '  ✓ docker compose 可用' || echo '  ✗ docker compose 不可用'"

    log "③ 线上现有凭据 与 密钥卷当前值 是否一致（只比 sha256[:12]）"
    if [ "$VAULT_OK" != 1 ]; then
        warn "  凭据卷没挂 ⇒ 无从判断 creds 会不会改掉线上凭据，跳过"
        return 0
    fi

    local names=(pg-password auth-token internal-token mqtt-pass)
    local want=(
        "$(hash12 "$(vault_get ecas/ubuntu-test.toml pg_password)")"
        "$(hash12 "$(vault_get ecas/ubuntu-test.toml auth_token)")"
        "$(hash12 "$(vault_get ecas/ubuntu-test.toml internal_token)")"
        "$(hash12 "$(vault_get ecas/mqtt.toml monitor_password)")"
    )
    # 在目标机以 root 读现有文件，只把哈希带回来
    local have
    have=$(sudo_script <<'EOF'
for f in pg-password auth-token internal-token mqtt-pass; do
    if [ -r "/opt/ecas/credentials/$f" ]; then
        sha256sum "/opt/ecas/credentials/$f" | cut -c1-12
    else
        printf 'MISSING\n'
    fi
done
EOF
    )

    local i h ok=1
    for i in 0 1 2 3; do
        h=$(printf '%s\n' "$have" | sed -n "$((i + 1))p")
        if [ "$h" = "${want[$i]}" ]; then
            printf '  ✓ %-16s %s\n' "${names[$i]}" "$h"
        else
            printf '  ✗ %-16s 线上 %s ≠ 卷 %s\n' "${names[$i]}" "$h" "${want[$i]}"
            ok=0
        fi
    done
    echo
    if [ "$ok" = 1 ]; then
        echo "  ⇒ 一致：creds 是幂等的，重跑不会改掉线上任何凭据（all 可以放心跑）"
    else
        warn "  ⇒ 不一致：跑 creds 会把线上凭据换成卷里的值"
        warn "     这不是你要的轮换就先别跑 creds —— 它不影响 push / build / up"
    fi
}

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
    preflight) cmd_preflight ;;
    *) sed -n '2,36p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
