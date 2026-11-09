#!/usr/bin/env bash
# ============================================================================
# ECAS 现场采集抓包 —— 服务器侧启动包装（口令不落盘、不进命令行）
#
# 口子从哪里来：与 ecas-monitor.service 同一份 systemd 加密凭据
#   /root/ecas/credentials/mqtt-pass.cred（systemd-creds --with-key=host 加密）
# 本脚本把它现解到内存（变量），经环境变量交给 ecas_capture.py；
# 口令不打印、不落盘、不出现在 ps 里。
#
# 用法（在服务器上）：
#   /root/ecas/scripts/capture-run.sh --duration 300
#   /root/ecas/scripts/capture-run.sh --duration 600 --out /root/ecas/capture/$(date +%Y%m%d-%H%M).jsonl
# ============================================================================
set -uo pipefail

CRED="/root/ecas/credentials/mqtt-pass.cred"
TOOL="/root/ecas/scripts/ecas_capture.py"

[[ -r "$CRED" ]] || { echo "❌ 读不到凭据：$CRED" >&2; exit 2; }
[[ -r "$TOOL" ]] || { echo "❌ 读不到工具：$TOOL" >&2; exit 2; }
command -v systemd-creds >/dev/null || { echo "❌ 缺 systemd-creds" >&2; exit 2; }
command -v mosquitto_sub >/dev/null || { echo "❌ 缺 mosquitto_sub" >&2; exit 2; }

MQTT_USER="ecas-monitor"
MQTT_PASS="$(systemd-creds decrypt --name=mqtt-pass --with-key=host "$CRED" - 2>/dev/null | tr -d '\r\n')"
[[ -n "$MQTT_PASS" ]] || { echo "❌ 凭据解密失败" >&2; exit 2; }
export MQTT_USER MQTT_PASS

mkdir -p /root/ecas/capture
DEFAULT_OUT="/root/ecas/capture/$(date +%Y%m%d-%H%M%S)-dpro66.jsonl"

# 没显式给 --out 就用带时间戳的默认文件
has_out=0
for a in "$@"; do [[ "$a" == "--out" || "$a" == --out=* ]] && has_out=1; done
if [[ $has_out -eq 0 ]]; then
    set -- "$@" --out "$DEFAULT_OUT"
fi

exec python3 "$TOOL" "$@"
