#!/bin/bash
# 受限 SSH 入口（2026-11-10）：只允许本地服务器拉取/清空云端缓存 spool，不给 shell。
# authorized_keys 里配：command="/opt/ecas/fallback/ssh-spool.sh",restrict <pubkey>
set -u
SPOOL="${SPOOL:-/var/lib/ecas/fallback/spool/rtu-live.jsonl}"
case "${SSH_ORIGINAL_COMMAND:-pull}" in
  pull)   exec cat "$SPOOL" ;;
  size)   wc -c < "$SPOOL" ;;
  rotate) : > "$SPOOL"; echo "rotated" ;;
  *)      echo "denied: ${SSH_ORIGINAL_COMMAND}" >&2; exit 1 ;;
esac
