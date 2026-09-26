#!/usr/bin/env bash
# 把本机 web/ 这一套（后端 + 前端产物 + systemd 单元）部署到云端服务器，幂等可重复跑。
#
#   web/scripts/deploy-to-cloud.sh              # 构建前端 → 同步 → uv sync → 重启 → 自检
#   web/scripts/deploy-to-cloud.sh --no-build   # 跳过 pnpm build（前端产物没改时）
#   HOST_ALIAS=ecas web/scripts/deploy-to-cloud.sh
#
# 前置（首次部署时做一次，之后不用管）：
#   1. 云端已有 /root/ecas-web/ecas-web.env（600，含 ECAS_DSN 与 ECAS_AUTH_TOKEN）
#      —— 模板见 web/deploy/cloud.env.example
#   2. 云端已装 uv（curl -LsSf https://astral.sh/uv/install.sh | sh）
#
# 这个脚本**不动**旧的 ecas-api.service（那个跑的是早期的单文件 api.py）；
# 首次切换端口时手动：systemctl disable --now ecas-api
set -euo pipefail

HOST_ALIAS="${HOST_ALIAS:-ecas}"
REMOTE_DIR="${REMOTE_DIR:-/root/ecas-web}"
UV_BIN="${UV_BIN:-/root/.local/bin/uv}"
BUILD=1
for arg in "$@"; do
  case "$arg" in
    --no-build) BUILD=0 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
WEB="$REPO_ROOT/web"
SSH_OPTS=(-o ControlMaster=no -o ConnectTimeout=10)
RSYNC_SSH="ssh ${SSH_OPTS[*]}"
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "0/6 目标与前置检查"
echo "  主机别名：$HOST_ALIAS（$REMOTE_DIR）"
ssh "${SSH_OPTS[@]}" "$HOST_ALIAS" "test -f $REMOTE_DIR/ecas-web.env" \
  || { echo "❌ 云端缺 $REMOTE_DIR/ecas-web.env（模板见 web/deploy/cloud.env.example）" >&2; exit 1; }
ssh "${SSH_OPTS[@]}" "$HOST_ALIAS" "test -x $UV_BIN" \
  || { echo "❌ 云端缺 uv（$UV_BIN）：curl -LsSf https://astral.sh/uv/install.sh | sh" >&2; exit 1; }

if [ "$BUILD" = 1 ]; then
  say "1/6 本机构建前端（产物由后端单端口托管）"
  (cd "$WEB/frontend" && pnpm build)
else
  say "1/6 跳过前端构建（--no-build）"
fi

say "2/6 同步后端源码（不带 .venv / __pycache__ / .env）"
rsync -az --delete -e "$RSYNC_SSH" \
  --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.env' \
  "$WEB/backend/" "$HOST_ALIAS:$REMOTE_DIR/backend/"

say "3/6 同步前端产物"
rsync -az --delete -e "$RSYNC_SSH" "$WEB/frontend/dist/" "$HOST_ALIAS:$REMOTE_DIR/frontend/dist/"

say "4/6 云端装依赖（uv + 锁定版本 + 自管 CPython 3.13）"
ssh "${SSH_OPTS[@]}" "$HOST_ALIAS" "cd $REMOTE_DIR/backend && $UV_BIN sync --frozen --no-dev"

say "5/6 装单元文件并重启"
ssh "${SSH_OPTS[@]}" "$HOST_ALIAS" "cat > /etc/systemd/system/ecas-web.service" < "$WEB/deploy/ecas-web.service"
ssh "${SSH_OPTS[@]}" "$HOST_ALIAS" 'systemctl daemon-reload && systemctl enable --now ecas-web.service && systemctl restart ecas-web.service && sleep 3 && systemctl is-active ecas-web.service'

say "6/6 自检（只打状态码与行数，不打令牌）"
ssh "${SSH_OPTS[@]}" "$HOST_ALIAS" 'echo -n "/healthz      → "; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/healthz
echo -n "/api/auth     → "; curl -s http://127.0.0.1:8080/api/auth; echo
echo -n "视图（401）   → "; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/api/health
echo -n "首页          → "; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/'
echo
echo "✅ 完成。本机看云端看板：ssh -L 8080:127.0.0.1:8080 $HOST_ALIAS  → http://127.0.0.1:8080/"
