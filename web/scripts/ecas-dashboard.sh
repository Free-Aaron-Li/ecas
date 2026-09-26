#!/usr/bin/env bash
# ============================================================================
#  ECAS 云端看板 · 一键打开（macOS / 任意终端，包括 Ghostty）
#
#  做什么：① 确保本机 18080 → 云端 127.0.0.1:8080 的 SSH 隧道存在
#          ② 从云端取访问令牌（不打印）
#          ③ 用默认浏览器打开带令牌的看板直链
#
#  为什么是 18080：本机 8080 被**本地开发后端**（web/backend）占着，
#  云端实时看板必须换一个本地端口，否则会打到本地那个库上。
#
#  用法：
#    ./ecas-dashboard.sh              起隧道并打开看板
#    ./ecas-dashboard.sh --no-open    只起隧道（不开浏览器）
#    ./ecas-dashboard.sh --status     看隧道状态
#    ./ecas-dashboard.sh --stop       关掉隧道
#    ./ecas-dashboard.sh --url        只打印看板 URL（含令牌，注意别外传）
#
#  环境变量：HOST_ALIAS（默认 ecas）、LOCAL_PORT（默认 18080）、REMOTE_PORT（默认 8080）
# ============================================================================
set -euo pipefail

HOST_ALIAS="${HOST_ALIAS:-ecas}"
LOCAL_PORT="${LOCAL_PORT:-18080}"
REMOTE_PORT="${REMOTE_PORT:-8080}"
ENV_FILE="/root/ecas-web/ecas-web.env"

SSH_OPTS=(-o BatchMode=yes -o ExitOnForwardFailure=yes
          -o ServerAliveInterval=30 -o ServerAliveCountMax=3
          -o ControlMaster=no -o ConnectTimeout=10)

die() { printf '✗ %s\n' "$*" >&2; exit 1; }

running() { lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; }

fetch_token() {
  ssh "${SSH_OPTS[@]}" "$HOST_ALIAS" \
    "grep -oP '(?<=^ECAS_AUTH_TOKEN=).*' $ENV_FILE" 2>/dev/null | tr -d '\r'
}

start_tunnel() {
  running && { echo "✓ 隧道已在 127.0.0.1:$LOCAL_PORT"; return 0; }
  echo "→ 建立隧道 127.0.0.1:$LOCAL_PORT → $HOST_ALIAS:127.0.0.1:$REMOTE_PORT"
  ssh "${SSH_OPTS[@]}" -f -N -L "$LOCAL_PORT:127.0.0.1:$REMOTE_PORT" "$HOST_ALIAS" \
    || die "隧道建立失败（先手动跑一次 ssh $HOST_ALIAS 看有没有报错）"
  for _ in $(seq 1 20); do running && break; sleep 0.2; done
  running || die "隧道进程起了但端口没监听"
  echo "✓ 隧道就绪（pid $(lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN -t | head -1)）"
}

health_check() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$LOCAL_PORT/healthz" || true)"
  [[ "$code" == "200" ]] || die "隧道在，但云端 /healthz 返回 $code（云端 ecas-web 服务没跑？）"
  echo "✓ 云端后端健康（/healthz 200）"
}

case "${1:-}" in
  --status)
    if running; then
      echo "✓ 隧道在跑：$(lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN | tail -1)"
      health_check
    else
      echo "✗ 没有隧道（本机 $LOCAL_PORT 无监听）"
    fi
    exit 0
    ;;
  --stop)
    pkill -f "$LOCAL_PORT:127.0.0.1:$REMOTE_PORT" && echo "✓ 已关隧道" || echo "（本来就没在跑）"
    exit 0
    ;;
  --url)
    TOKEN="$(fetch_token)"; printf 'http://127.0.0.1:%s/?token=%s\n' "$LOCAL_PORT" "$TOKEN"
    exit 0
    ;;
  --no-open|"") ;;
  *) die "未知参数：$1（见文件头用法）" ;;
esac

start_tunnel
health_check

if [[ "${1:-}" == "--no-open" ]]; then exit 0; fi

TOKEN="$(fetch_token)"
[[ -n "$TOKEN" ]] || die "没取到令牌（检查 $HOST_ALIAS:$ENV_FILE 里的 ECAS_AUTH_TOKEN）"
URL="http://127.0.0.1:$LOCAL_PORT/?token=$TOKEN"
if command -v open >/dev/null 2>&1; then
  open "$URL" && echo "✓ 已用默认浏览器打开看板（令牌已自动填入并会从地址栏消失）"
else
  printf '%s\n' "$URL"
fi
