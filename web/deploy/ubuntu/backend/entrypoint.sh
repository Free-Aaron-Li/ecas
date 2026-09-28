#!/usr/bin/env bash
# ============================================================================
# 后端容器入口 —— 把「加密凭据解出来的 container secret」变成运行时需要的形态：
#   /run/secrets/pg-password    → /tmp/pgpass（0600）+ 导出 PGPASSFILE
#   /run/secrets/auth-token     → ECAS_AUTH_TOKEN（只存在于本进程环境）
#   /run/secrets/internal-token → ECAS_INTERNAL_TOKEN（账户内部接口专用，可选）
# 然后 exec 交棒给 CMD（uvicorn）。
#
# ⛔ 口令不落盘：/tmp 是 compose 给的 tmpfs；不进镜像、不进镜像层、不写任何日志。
#    这也是 `ECAS_DSN` 里不带口令的原因（libpq 会自己按 PGPASSFILE 找）。
# 🔒 fail-closed（安全审查 M-03）：**没有访问令牌就拒绝启动**，不再「打个警告继续跑」。
#    2026-09-28 之前的写法是 warning 后继续 ⇒ 一旦 secret 没挂上，服务会以「不校验」的姿态活着。
#    只有显式设 ECAS_ALLOW_NO_AUTH=1（本机开发）才允许裸跑。
# ============================================================================
set -euo pipefail

PG_PASSWORD_FILE="${PG_PASSWORD_FILE:-/run/secrets/pg-password}"
AUTH_TOKEN_FILE="${AUTH_TOKEN_FILE:-/run/secrets/auth-token}"
INTERNAL_TOKEN_FILE="${INTERNAL_TOKEN_FILE:-/run/secrets/internal-token}"
PGPASSFILE="${PGPASSFILE:-/tmp/pgpass}"
ALLOW_NO_AUTH="${ECAS_ALLOW_NO_AUTH:-0}"

if [ -r "$PG_PASSWORD_FILE" ] && [ -s "$PG_PASSWORD_FILE" ]; then
    printf '%s:%s:%s:%s:%s\n' \
        "${PG_HOST:-db}" "${PG_PORT:-5432}" "${PG_NAME:-ecas}" "${PG_USER:-ecas}" \
        "$(cat "$PG_PASSWORD_FILE")" > "$PGPASSFILE"
    chmod 600 "$PGPASSFILE"
    export PGPASSFILE
    echo "[entrypoint] 已就绪 pgpass（${PG_HOST:-db}:${PG_PORT:-5432}/${PG_NAME:-ecas}）"
else
    echo "[entrypoint] ⚠️ 没有 $PG_PASSWORD_FILE，按无口令连接（仅限本地调试）"
fi

if [ -r "$AUTH_TOKEN_FILE" ] && [ -s "$AUTH_TOKEN_FILE" ]; then
    ECAS_AUTH_TOKEN="$(cat "$AUTH_TOKEN_FILE")"
    export ECAS_AUTH_TOKEN
    echo "[entrypoint] 已载入访问令牌（长度 ${#ECAS_AUTH_TOKEN}，不打印内容）"
elif [ "$ALLOW_NO_AUTH" = "1" ]; then
    echo "[entrypoint] ⚠️ 没有 $AUTH_TOKEN_FILE，但 ECAS_ALLOW_NO_AUTH=1 ⇒ 裸跑（仅限本机开发）"
else
    echo "[entrypoint] ✗ 拒绝启动：$AUTH_TOKEN_FILE 缺失或为空 ⇒ 访问令牌没配好。" >&2
    echo "[entrypoint]   fail-closed 是 2026-09-28 安全审查 M-03 的修法：宁可起不来，也不要「以为有门其实全开」。" >&2
    echo "[entrypoint]   本机开发确实不想配令牌时，显式设 ECAS_ALLOW_NO_AUTH=1。" >&2
    exit 1
fi

if [ -r "$INTERNAL_TOKEN_FILE" ] && [ -s "$INTERNAL_TOKEN_FILE" ]; then
    ECAS_INTERNAL_TOKEN="$(cat "$INTERNAL_TOKEN_FILE")"
    export ECAS_INTERNAL_TOKEN
    echo "[entrypoint] 已载入内部账户令牌（长度 ${#ECAS_INTERNAL_TOKEN}，与对外令牌分离）"
else
    echo "[entrypoint] ⚠️ 没有 $INTERNAL_TOKEN_FILE：/internal/accounts* 沿用对外令牌（过渡期，建议尽快分离）"
fi

exec "$@"
