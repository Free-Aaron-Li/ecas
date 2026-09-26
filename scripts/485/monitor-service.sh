#!/usr/bin/env bash
# ============================================================================
# ECAS 浓度计监视 —— systemd 服务的启动包装
#
# 为什么需要这个包装：
#   1) 口令**不明文落盘**：由 systemd 用 LoadCredentialEncrypted= 现场解密到
#      /run/credentials/<unit>/（tmpfs，仅 root 可读），本脚本从那里取。
#   2) 服务里不能写 EnvironmentFile（那是明文文件）；这样也算回应了
#      「口令要不要落服务器」的顾虑 —— 落的是用本机密钥加密过的密文。
#
# 由 /etc/systemd/system/ecas-monitor.service 调用，不要手动跑。
# ============================================================================
set -uo pipefail

CRED_DIR="${CREDENTIALS_DIRECTORY:-}"

if [[ -z "$CRED_DIR" || ! -r "$CRED_DIR/mqtt-pass" ]]; then
    echo "❌ 读不到解密后的凭据：\${CREDENTIALS_DIRECTORY}/mqtt-pass" >&2
    echo "   检查 ecas-monitor.service 里的：" >&2
    echo "   LoadCredentialEncrypted=mqtt-pass:/root/ecas/credentials/mqtt-pass.cred" >&2
    exit 2
fi

# 口令只在内存里过一手，不打印、不落盘
# 最小权限：采集器用 ecas-monitor（ACL 里只有 topic read dpro66），不再借用管理账号 ecas
MQTT_USER="ecas-monitor"
MQTT_PASS="$(cat "$CRED_DIR/mqtt-pass")"
export MQTT_USER MQTT_PASS

exec /root/ecas/scripts/dpro66_monitor.sh \
     -t dpro66 --model DPRO-66 \
     -c /root/ecas/data/dpro66_readings.csv
