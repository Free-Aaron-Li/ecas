#!/bin/bash
# 开机后自检（2026-11-10）：等各服务就绪后跑一次看门狗，把结论写 boot-check.jsonl 并立即播报。
# 由 ecas-boot-check.service（oneshot）在 ecas-stack / ecas-flow-ingest 之后调用。
set -u
WAIT="${BOOT_WAIT:-45}"
LOG_DIR="${LOG_DIR:-/var/log/ecas}"
OUT="$LOG_DIR/boot-check.jsonl"
mkdir -p "$LOG_DIR" 2>/dev/null || true
sleep "$WAIT"
rc=0
WD="${WATCHDOG:-/opt/ecas/watchdog/ecas-watchdog.sh}"
line=$("$WD" 2>&1) || rc=$?
python3 - "$OUT" "$rc" <<'PY' "$line"
import json, sys, datetime
out, rc, text = sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""
bad = [l for l in text.splitlines() if '"level": "warn"' in l or '"level": "crit"' in l]
rec = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
       "event": "boot_check", "watchdog_rc": int(rc), "bad": len(bad),
       "detail": bad[:6]}
with open(out, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
print(json.dumps(rec, ensure_ascii=False))
PY
exit "$rc"
