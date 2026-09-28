#!/usr/bin/env bash
# ============================================================================
# ECAS 公网边缘部署（腾讯云 124.222.122.146 / OpenCloudOS 9.4）
#
#   用法：  ./deploy-cloud-edge.sh all       # 打包 + 生成证书 + 起容器 + 自检
#           ./deploy-cloud-edge.sh stage     # 只渲染并推送 /opt/ecas-edge
#           ./deploy-cloud-edge.sh cert      # 只在云端生成自签证书（已存在则复用）
#           ./deploy-cloud-edge.sh up        # 只起/更新边缘容器
#           ./deploy-cloud-edge.sh verify    # 只跑自检（不依赖安全组）
#           ./deploy-cloud-edge.sh logs      # 看边缘访问日志
#           ./deploy-cloud-edge.sh down      # 停掉边缘（公网立刻恢复不可达）
#
#   前置：ssh 别名 ecas 指向该云主机（root 直连，无需 sudo 口令）
#   出口：本机 80/443 **只做 301 跳转** → https://gateway.xshky.org（Cloudflare → Worker → mesh）
#         2026-09-28 安全审查 H-03 之前是「反代 http://10.10.10.11:18080」；现在公网不再有
#         直通业务后端的路径，只保留本地 /healthz 供 compose 健康检查。
#   安全组：需放行 80 + 443（都只跳转）；本机 firewalld 未运行，无需额外放行
# ============================================================================
set -euo pipefail

HOST=${ECAS_CLOUD_SSH:-ecas}
PUBLIC_IP=${ECAS_PUBLIC_IP:-124.222.122.146}
# 正式入口域名（2026-09-28 安全审查 H-03 后：本机 443/80 只做 301 跳到它，不再反代业务）
PUBLIC_HOST=${ECAS_PUBLIC_HOST:-gateway.xshky.org}
MESH_IP=${ECAS_MESH_IP:-10.10.10.123}
UPSTREAM=${ECAS_UPSTREAM:-10.10.10.11:18080}
IMAGE=${ECAS_EDGE_IMAGE:-nginx:1.27-alpine}

REMOTE_DIR=/opt/ecas-edge
STAGE=/tmp/ecas-edge-stage
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
SRC="$REPO/web/deploy/cloud"
TPL="$SRC/nginx/ecas-edge.conf"

CERT_IN_CONTAINER=/etc/nginx/ssl/ecas-selfsigned.crt
KEY_IN_CONTAINER=/etc/nginx/ssl/ecas-selfsigned.key
HOST_LOG="$REMOTE_DIR/logs/ecas-access.log"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ControlPath=none)
remote() { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }
die() { echo "✗ $*" >&2; exit 1; }

[ -f "$TPL" ] || die "找不到配置模板：$TPL"
[ -f "$SRC/docker-compose.yml" ] || die "找不到 docker-compose.yml"

render() {
    sed -e "s|__PUBLIC_IP__|$PUBLIC_IP|g" \
        -e "s|__PUBLIC_HOST__|$PUBLIC_HOST|g" \
        -e "s|__UPSTREAM__|$UPSTREAM|g" \
        -e "s|__CERT__|$CERT_IN_CONTAINER|g" \
        -e "s|__KEY__|$KEY_IN_CONTAINER|g" "$TPL"
}

cmd_stage() {
    echo "── 打包边缘资产 ──"
    rm -rf "$STAGE"
    mkdir -p "$STAGE/nginx" "$STAGE/ssl" "$STAGE/logs" "$STAGE/acme"
    render > "$STAGE/nginx/ecas-edge.conf"
    cp "$SRC/docker-compose.yml" "$STAGE/"
    printf 'EDGE_IMAGE=%s\n' "$IMAGE" > "$STAGE/.env"
    chmod -R u=rwX,go=rX "$STAGE"
    chmod 755 "$STAGE"

    echo "── 推送到 $HOST:$REMOTE_DIR ──"
    remote "install -d -m 755 $REMOTE_DIR"
    rsync -a --delete --exclude ssl --exclude logs --exclude acme "$STAGE"/ "$HOST:$REMOTE_DIR/"
    # 日志目录给容器内 nginx 用户（alpine uid 101）可写
    remote "install -d -m 755 $REMOTE_DIR/ssl $REMOTE_DIR/acme &&
            install -d -m 755 -o 101 -g 101 $REMOTE_DIR/logs &&
            ls -la $REMOTE_DIR && echo ---- && grep -n 'server_name\|proxy_pass\|ssl_certificate ' $REMOTE_DIR/nginx/ecas-edge.conf"
}

cmd_cert() {
    echo "── 云端自签证书 ──"
    remote bash -s <<EOS
set -euo pipefail
cd $REMOTE_DIR
if [ ! -s ssl/ecas-selfsigned.key ] || [ ! -s ssl/ecas-selfsigned.crt ]; then
    openssl req -x509 -nodes -newkey rsa:2048 -days 397 \
        -keyout ssl/ecas-selfsigned.key -out ssl/ecas-selfsigned.crt \
        -subj "/C=CN/O=ECAS/CN=$PUBLIC_IP" \
        -addext "subjectAltName=IP:$PUBLIC_IP,IP:$MESH_IP,DNS:ecas.local" >/dev/null 2>&1
    chmod 600 ssl/ecas-selfsigned.key
    chmod 644 ssl/ecas-selfsigned.crt
    echo "[cert] 已生成自签证书（397 天，SAN=IP:$PUBLIC_IP,IP:$MESH_IP）"
else
    echo "[cert] 复用已有自签证书"
fi
openssl x509 -in ssl/ecas-selfsigned.crt -noout -subject -dates -ext subjectAltName
EOS
}

cmd_up() {
    echo "── 拉起边缘容器 ──"
    remote bash -s <<EOS
set -euo pipefail
cd $REMOTE_DIR
docker compose up -d --remove-orphans
sleep 3
docker compose ps
EOS
}

cmd_verify() {
    echo "── 边缘自检（在云主机本地发起，不依赖安全组放行） ──"
    remote bash -s <<EOS
set -uo pipefail
code() { curl -k -sS -o /dev/null --max-time 10 -w '%{http_code}' "\$@" 2>/dev/null || echo ERR; }
echo "① 容器              : \$(cd $REMOTE_DIR && docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | tr '\n' ' ')"
echo "② 监听 80/443       : \$(ss -lntp 2>/dev/null | awk '/:(80|443) /{print \$4}' | sort -u | tr '\n' ' ')"
echo "③ https 本机 /healthz      → \$(code https://127.0.0.1/healthz)   （期望 200：本地健康检查，不反代）"
echo "④ https 本机 /            → \$(code https://127.0.0.1/)   （期望 301：跳 https://$PUBLIC_HOST/）"
echo "⑤ https 本机 /docs        → \$(code https://127.0.0.1/docs)   （期望 301：不再是 200 的接口文档）"
echo "⑥ http 本机 /healthz       → \$(code http://127.0.0.1/healthz)   （期望 301：跳 https）"
echo "⑦ mesh 上游健康（仅供排障） → \$(curl -sS -o /dev/null --max-time 10 -w '%{http_code}' http://$UPSTREAM/healthz || echo 够不到)"
echo "⑧ 证书 SAN          : \$(openssl x509 -in $REMOTE_DIR/ssl/ecas-selfsigned.crt -noout -ext subjectAltName 2>/dev/null | tail -1 | xargs)"
echo "   证书指纹          : \$(openssl x509 -in $REMOTE_DIR/ssl/ecas-selfsigned.crt -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)"
echo "⑨ 最近一条访问日志   : \$([ -f $HOST_LOG ] && tail -1 $HOST_LOG || echo '（暂无访问）')"
EOS
}

cmd_logs() { remote "tail -n ${1:-40} $HOST_LOG 2>/dev/null || echo '（暂无访问日志）'"; }

cmd_down() {
    echo "── 停掉边缘容器 ──"
    remote "cd $REMOTE_DIR && docker compose down"
}

case "${1:-all}" in
    stage)  cmd_stage ;;
    cert)   cmd_cert ;;
    up)     cmd_up ;;
    verify) cmd_verify ;;
    logs)   shift; cmd_logs "$@" ;;
    down)   cmd_down ;;
    all)    cmd_stage; cmd_cert; cmd_up; cmd_verify ;;
    *)      die "未知子命令：$1（可用：all|stage|cert|up|verify|logs|down）" ;;
esac
