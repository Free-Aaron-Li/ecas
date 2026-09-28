#!/usr/bin/env bash
# ============================================================================
# ubuntu-sudo.sh —— 在 ubuntu-super（10.10.10.11）上以 root 执行一段脚本
#
# 怎么保证口令不进 argv / 环境 / 日志：
#   * 脚本正文经 scp 送到远端 /tmp（正文里没有任何口令）；
#   * sudo 的口令由本机从密钥卷现读，走 **ssh 的 stdin** 管道送给远端
#     ⇒ 只存在于内存与加密信道里，`ps` 看不到、history 里没有；
#   * 用完即删远端临时脚本。
#
# 用法：
#     printf '%s\n' 'apt-get install -y docker-compose-v2' | web/scripts/ubuntu-sudo.sh
#     web/scripts/ubuntu-sudo.sh <<'EOS'
#     ... 多条 root 命令 ...
#     EOS
#
# 环境变量：HOST（默认 ubuntu-super）、KEY（卷内键名，默认 ubuntu_Super_Server）、
#           VAULT（默认 /Volumes/keys/ssh/ssh.toml）
# ============================================================================
set -euo pipefail

HOST="${HOST:-ubuntu-super}"
KEY="${KEY:-ubuntu_Super_Server}"
VAULT="${VAULT:-/Volumes/keys/ssh/ssh.toml}"
REMOTE_TMP="/tmp/.ubuntu-sudo-$$.sh"

TMP="$(mktemp -t ubuntu-sudo.XXXXXX.sh)"
cleanup() { rm -f "$TMP"; ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" "rm -f $REMOTE_TMP" >/dev/null 2>&1 || true; }
trap cleanup EXIT

cat > "$TMP"                     # 脚本正文从本机 stdin 读入

PW="$(python3 - "$VAULT" "$KEY" <<'PY'
import pathlib, re, sys
text = pathlib.Path(sys.argv[1]).read_text()
m = re.search(r'^%s\s*=\s*"((?:[^"\\]|\\.)*)"' % re.escape(sys.argv[2]), text, re.M)
sys.stdout.write(m.group(1) if m else "")
PY
)"
[[ -n "$PW" ]] || { echo "✋ 卷里取不到 $KEY（$VAULT）" >&2; exit 3; }

scp -q -o BatchMode=yes -o ConnectTimeout=10 "$TMP" "$HOST:$REMOTE_TMP"
printf '%s\n' "$PW" | ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" \
    "sudo -S -p '' bash $REMOTE_TMP"
