#!/usr/bin/env bash
# ============================================================================
# start-monitor.sh —— 在服务器上常驻启动 DPRO-66 监视（口令从 stdin 读一行）
#
# ⭐ 为什么这样起：口令**不进命令行、不进 ps、不进日志**
#      用法：printf '%s\n' 'MQTT口令' | ssh ecas 'bash /root/ecas/scripts/start-monitor.sh'
#      或  ：printf '%s\n' 'MQTT口令' | bash /root/ecas/scripts/start-monitor.sh   （已在服务器上时）
#
# ⛔ 别用 pkill -f / ps -ef 清进程：前者经 ssh 会命中自己的 bash -c，后者会把 -P 口令打进输出
# ============================================================================
set -u

read -r MQTT_PASS || true
if [[ -z "${MQTT_PASS:-}" ]]; then
    echo "用法：printf '%s\\n' 'MQTT口令' | bash $0" >&2
    exit 2
fi
export MQTT_PASS MQTT_USER=ecas

LOG=/root/ecas/logs/dpro66_monitor.log
CSV=/root/ecas/data/dpro66_readings.csv
cd /root/ecas || exit 2

# --- 清掉可能残留的监视与订阅（按 PID；先 TERM 再 KILL）---
for p in $(pgrep -f 'dpro66_monito[r].sh' 2>/dev/null || true); do kill "$p" 2>/dev/null || true; done
sleep 1
for p in $(pgrep -x mosquitto_sub 2>/dev/null || true); do kill -9 "$p" 2>/dev/null || true; done

# --- 起新的（nohup + stdin=/dev/null，ssh 断开也不受影响）---
nohup /root/ecas/scripts/dpro66_monitor.sh -t dpro66 -c "$CSV" --model DPRO-66 \
      >> "$LOG" 2>&1 < /dev/null &
sleep 6

echo "monitor pid : $(pgrep -f 'dpro66_monito[r].sh' | tr '\n' ' ')"
echo "sub pid     : $(pgrep -x mosquitto_sub | tr '\n' ' ')"
echo "--- 日志尾部（应看到「数据来源 … 用户 ecas」）---"
tail -n 14 "$LOG"
