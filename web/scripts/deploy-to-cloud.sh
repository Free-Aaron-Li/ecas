#!/usr/bin/env bash
# ⚠️ 已退役（2026-09-29 标注）—— 默认拒绝执行。见下方「退役闸门」。
#
# 这个脚本把 web/ 同步到腾讯云 `ecas:/root/ecas-web`，并安装/重启 systemd 单元
# `ecas-web.service`（只监听 127.0.0.1:8080）。**那已经不是现网了**：
#
#   · 腾讯云 VM-0-13-opencloudos：ecas-web.service 仍然存在，但已是
#     `disabled; inactive (dead)`。那台机器现在只做 MQTT 中继
#     （ecas-mqtt-relay.service）与边缘网关（docker 容器 ecas-edge = nginx，占 80/443）。
#   · 现网应用在 **ubuntu-super:/opt/ecas 的 Docker 栈**：compose 项目名 ecas，
#     systemd 单元 ecas-stack.service，容器 ecas-api 绑 10.10.10.11:18080 → 8000，
#     数据库 ecas-db（postgres:16-alpine，宿主机数据 /data/ecas/pgdata）。
#
# 对着旧目标跑它**不会报错**：只会 rsync 一份没人用的代码、enable 一个没人用的
# 单元 —— 也就是「静默部署到错的地方」。所以在这里直接拦下。
#
# 现网部署请用：
#   web/deploy/ubuntu/deploy.sh  stage|push|creds|build|up|check|preflight|all
#   说明见 web/deploy/ubuntu/README.md 与 docs/ECAS部署与踩坑手册.md
#
# 确实要对着旧目标跑（考古 / 回滚演练），显式加 ALLOW_RETIRED=1。
#
# ── 以下是退役前的原始说明，仅供理解当时的流程 ──────────────────────────────
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

# ── 退役闸门 ────────────────────────────────────────────────────────────────
# 只挡「对着旧目标真跑」这一件事；没设 ALLOW_RETIRED=1 时连前置检查都不做，
# 免得它顺手 ssh 一下旧机器。
if [ "${ALLOW_RETIRED:-0}" != "1" ]; then
  # 帮助要能读（它是「怎么改成正确用法」的入口），其余一律拦下。
  for arg in "$@"; do
    case "$arg" in
      -h|--help) sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d'; exit 0 ;;
    esac
  done
  cat >&2 <<'EOF'
❌ 本脚本已退役，不再执行。

   现网部署请用：
     web/deploy/ubuntu/deploy.sh  stage|push|creds|build|up|check|preflight|all
   说明：web/deploy/ubuntu/README.md

   本脚本指向的 systemd ecas-web.service（腾讯云 /root/ecas-web，127.0.0.1:8080）
   已停用。现网是 ubuntu-super:/opt/ecas 的 Docker 栈（单元 ecas-stack.service，
   容器 ecas-api 绑 10.10.10.11:18080 → 8000）。细节见本文件头部注释。

   确实要对着旧目标跑（考古 / 回滚演练），显式加 ALLOW_RETIRED=1：
     ALLOW_RETIRED=1 web/scripts/deploy-to-cloud.sh
EOF
  exit 1
fi

HOST_ALIAS="${HOST_ALIAS:-ecas}"
REMOTE_DIR="${REMOTE_DIR:-/root/ecas-web}"
UV_BIN="${UV_BIN:-/root/.local/bin/uv}"
BUILD=1
for arg in "$@"; do
  case "$arg" in
    --no-build) BUILD=0 ;;
    -h|--help) sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d'; exit 0 ;;
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

# 班次报表的曲线图靠 matplotlib 画中文。服务器一个中文字体都没有时**不会报错**，
# 只会静默把图上的标签换成英文 —— 这种事不主动查就永远发现不了，所以在部署前先看一
# 眼。**只警告不拦**：字体缺失不影响别的功能，不该堵住一次紧急修复。
CJK_FONTS=$(ssh "${SSH_OPTS[@]}" "$HOST_ALIAS" 'fc-list :lang=zh 2>/dev/null | wc -l' 2>/dev/null | tr -d '[:space:]')
CJK_FONTS="${CJK_FONTS:-0}"
if [ "$CJK_FONTS" -lt 1 ]; then
  echo "⚠️  云端没有中文字体 ⇒ 班次报表曲线图的标签会是**英文**（不报错，只是不像样）"
  echo "    修法：ssh $HOST_ALIAS 'dnf install -y google-noto-sans-cjk-sc-fonts && fc-cache -f'"
else
  echo "  中文字体：$CJK_FONTS 个 ✓"
fi

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
