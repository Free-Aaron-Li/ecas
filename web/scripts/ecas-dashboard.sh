#!/usr/bin/env bash
# ============================================================================
#  ECAS 云端看板 · 一键打开（macOS / 任意终端，包括 Ghostty）
#
#  做什么：① 确保本机 18080 → 现网后端的 SSH 隧道存在
#          ② 从容器里取访问令牌（不打印）
#          ③ 用默认浏览器打开带令牌的看板直链
#
#  ⚠️ 2026-09-29 起现网换了机器，本脚本已跟着改：
#     旧：ssh ecas（腾讯云 124.222.122.146）→ 127.0.0.1:8080（systemd ecas-web.service）
#     新：ssh ubuntu-super（mesh 10.10.10.11）→ 10.10.10.11:18080（docker 容器 ecas-api）
#     旧机器现在只做 MQTT 中继与边缘网关；它的 ecas-web.service 是 disabled/inactive。
#     ⚠️ 2026-10-26 安全收口后：容器只绑 **127.0.0.1:18080**，隧道出口写 127.0.0.1；
#     叠加网/公网全关。隧道由 launchd com.ecas.tunnel 常驻，本脚本发现端口已在监听就不再起隧道；
#     作业不在时脚本才起临时 ssh 隧道，且失败后会再查一次端口（防与常驻作业抢端口）。
#     令牌也不再来自 /root/ecas-web/ecas-web.env，而是 entrypoint 从 container secret
#     导出到容器 PID 1 的环境里（见 web/deploy/ubuntu/backend/entrypoint.sh）。
#
#  为什么本机也是 18080：与远端端口一致便于记忆；本机 8080 被**本地开发后端**
#  （web/backend）占着，云端实时看板必须换端口，否则会打到本地那个库上。
#
#  用法：
#    ./ecas-dashboard.sh              起隧道并打开看板
#    ./ecas-dashboard.sh --no-open    只起隧道（不开浏览器）
#    ./ecas-dashboard.sh --status     看隧道状态
#    ./ecas-dashboard.sh --stop       关掉隧道
#    ./ecas-dashboard.sh --url        只打印看板 URL（含令牌，注意别外传）
#
#  环境变量：HOST_ALIAS（默认 ubuntu-super）、LOCAL_PORT（默认 18080）、
#            REMOTE_HOST（默认 127.0.0.1，2026-10-26 起）、REMOTE_PORT（默认 18080）、
#            API_CONTAINER（默认 ecas-api）
# ============================================================================
set -euo pipefail

HOST_ALIAS="${HOST_ALIAS:-ubuntu-super}"
LOCAL_PORT="${LOCAL_PORT:-18080}"
REMOTE_HOST="${REMOTE_HOST:-127.0.0.1}"     # 2026-10-26 起容器只绑回环（mesh 地址已不可达）
REMOTE_PORT="${REMOTE_PORT:-18080}"
API_CONTAINER="${API_CONTAINER:-ecas-api}"

SSH_OPTS=(-o BatchMode=yes -o ExitOnForwardFailure=yes
          -o ServerAliveInterval=30 -o ServerAliveCountMax=3
          -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=10)

die() { printf '✗ %s\n' "$*" >&2; exit 1; }

running() { lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; }

# 常驻隧道（launchd com.ecas.tunnel）在 mesh 抖动时会「断开 → 立刻重连」：
# 那几百毫秒里 18080 是空的。脚本若此时再 ssh -L，就会和它抢同一个端口，
# 报 `bind [127.0.0.1]:18080: Address already in use`（2026-11-04 先生实测）。
# 所以：装了常驻作业就只踢它、不另起 ssh；实在要临时隧道时也做一次竞态兜底。
TUNNEL_LABEL="gui/$(id -u)/com.ecas.tunnel"
have_launchd_tunnel() { launchctl print "$TUNNEL_LABEL" >/dev/null 2>&1; }

wait_port() {  # wait_port <次数> <间隔秒>
  local tries="${1:-20}" gap="${2:-0.3}"
  for _ in $(seq 1 "$tries"); do running && return 0; sleep "$gap"; done
  running
}

# 令牌只在容器 PID 1 的环境里（NUL 分隔）。经管道取回、本地抽行，
# 全程不落盘、不进 argv、不打屏；取不到就返回空串，由调用方给提示。
fetch_token() {
  local remote_cmd="docker exec $API_CONTAINER sh -c 'tr \"\\0\" \"\\n\" < /proc/1/environ'"
  ssh "${SSH_OPTS[@]}" "$HOST_ALIAS" "$remote_cmd" 2>/dev/null \
    | sed -n 's/^ECAS_AUTH_TOKEN=//p' | tr -d '\r' || true
}

start_tunnel() {
  running && { echo "✓ 隧道已在 127.0.0.1:$LOCAL_PORT"; return 0; }

  # ① 有常驻作业时，先等它一拍（它可能正在重连），再决定是否踢它
  if have_launchd_tunnel; then
    if wait_port 12 0.3; then
      echo "✓ 隧道已在 127.0.0.1:${LOCAL_PORT}（常驻作业 com.ecas.tunnel）"
      return 0
    fi
    echo "→ 常驻隧道未监听，踢一下 com.ecas.tunnel（不另起 ssh，避免抢端口）"
    launchctl kickstart -k "$TUNNEL_LABEL" >/dev/null 2>&1 || true
    if wait_port 30 0.5; then
      echo "✓ 常驻隧道已就绪（pid $(lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN -t | head -1)）"
      return 0
    fi
    echo "（常驻隧道仍未就绪，改起临时 ssh 隧道）"
  fi

  # ② 临时隧道：失败时再查一次端口 —— 可能刚被常驻作业抢走
  echo "→ 建立隧道 127.0.0.1:$LOCAL_PORT → $HOST_ALIAS:$REMOTE_HOST:$REMOTE_PORT"
  if ! ssh "${SSH_OPTS[@]}" -f -N -L "$LOCAL_PORT:$REMOTE_HOST:$REMOTE_PORT" "$HOST_ALIAS"; then
    if wait_port 3 0.3; then
      echo "✓ 端口已被另一条隧道占用（常驻作业先抢到），沿用它"
      return 0
    fi
    die "隧道建立失败（18080 被占用或 mesh 不通；先手动跑一次 ssh $HOST_ALIAS 验证）"
  fi
  wait_port 20 0.2 || die "隧道进程起了但端口没监听"
  echo "✓ 隧道就绪（pid $(lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN -t | head -1)）"
}

health_check() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$LOCAL_PORT/healthz" || true)"
  [[ "$code" == "200" ]] || die "隧道在，但云端 /healthz 返回 ${code}（容器 $API_CONTAINER 没跑？）"
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
    pkill -f "$LOCAL_PORT:$REMOTE_HOST:$REMOTE_PORT" && echo "✓ 已关隧道（launchd com.ecas.tunnel 会在数秒内自动重连；要长期停用请 bootout）" || echo "（本来就没在跑）"
    exit 0
    ;;
  --url)
    TOKEN="$(fetch_token)"
    [[ -n "$TOKEN" ]] || die "没取到令牌（容器 $API_CONTAINER 在 $HOST_ALIAS 上跑着吗？）"
    printf 'http://127.0.0.1:%s/?token=%s\n' "$LOCAL_PORT" "$TOKEN"
    exit 0
    ;;
  --no-open|"") ;;
  *) die "未知参数：$1（见文件头用法）" ;;
esac

start_tunnel
health_check

if [[ "${1:-}" == "--no-open" ]]; then exit 0; fi

TOKEN="$(fetch_token)"
[[ -n "$TOKEN" ]] || die "没取到令牌（容器 $API_CONTAINER 在 $HOST_ALIAS 上跑着吗？）"
URL="http://127.0.0.1:$LOCAL_PORT/?token=$TOKEN"
if command -v open >/dev/null 2>&1; then
  open "$URL" && echo "✓ 已用默认浏览器打开看板（令牌已自动填入并会从地址栏消失）"
else
  printf '%s\n' "$URL"
fi
