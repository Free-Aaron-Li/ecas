#!/usr/bin/env bash
# ============================================================================
# test_db_insert.sh —— 「落库自检」：发一帧**假数据**，看它有没有一路走进
#                      CSV → 监视日志 → PostgreSQL，然后自动把测试行删掉。
#
# ⭐ 作用：证明「MQTT → 监视脚本 → 数据库」这条链路是通的，与现场 DTU 无关。
#          现场等数据时，先用它确认后端没毛病，免得怀疑错方向。
#
# 用法（口令从 stdin 读一行，不进命令行/ps/日志）：
#     printf '%s\n' 'MQTT口令' | ssh ecas 'bash /root/ecas/scripts/test_db_insert.sh'
#     printf '%s\n' 'MQTT口令' | bash test_db_insert.sh              （已在服务器上时）
#
# 可覆盖：RAW=15000（默认 12345）｜ MODEL=DPRO-66 ｜ TOPIC=dpro66 ｜ KEEP=1（不删测试行）
# 退出码：0 = 三处都看到 ｜ 3 = 没进库 ｜ 4 = 没进 CSV/日志 ｜ 2 = 用法错
# ============================================================================
set -u

read -r MQTT_PASS || true
if [[ -z "${MQTT_PASS:-}" ]]; then
    echo "用法：printf '%s\\n' 'MQTT口令' | bash $0" >&2
    exit 2
fi
# 默认用**只写**的现场账号（ACL 里 dpro66-dtu 只有 topic write dpro66）
# 别再用管理账号 ecas —— 现役口令只存在加密卷里，脚本不该依赖它
export MQTT_PASS MQTT_USER="${MQTT_USER:-dpro66-dtu}"

RAW="${RAW:-12345}"
MODEL="${MODEL:-DPRO-66}"
TOPIC="${TOPIC:-dpro66}"
CSV="${CSV:-/root/ecas/data/dpro66_readings.csv}"
LOG="${LOG:-/root/ecas/logs/dpro66_monitor.log}"
KEEP="${KEEP:-0}"
FRAME=/tmp/ecas-testframe.bin

# 11 字节：000000000005 + Modbus 回帧 01 04 02 <高字节> <低字节>（大端，先高后低）
HEXV=$(printf '%04X' "$RAW")
printf '%b' "\\x00\\x00\\x00\\x00\\x00\\x05\\x01\\x04\\x02\\x${HEXV:0:2}\\x${HEXV:2:2}" > "$FRAME"
echo "测试帧    : $(od -An -tx1 "$FRAME" | tr -d ' \n')   （raw = ${RAW} = 0x${HEXV}）"
echo "主题/型号 : ${TOPIC} / ${MODEL}"
echo "发帧前    : 库里 ${MODEL} 共 $(psql -X -qtAw -h 127.0.0.1 -U ecas -d ecas -c "SELECT count(*) FROM raw_reading WHERE model='${MODEL}'") 条"

mosquitto_pub -h 127.0.0.1 -p 1883 -t "$TOPIC" -q 1 -u "$MQTT_USER" -P "$MQTT_PASS" -f "$FRAME"
pub_rc=$?
echo "发布      : rc=${pub_rc}（0 = 已被 broker 接收）"
[[ $pub_rc -eq 0 ]] || { echo "❌ 发布失败：认证或 broker 有问题（rc≠0）"; exit 2; }

sleep 3

echo "--- ① 数据库 ---"
psql -X -w -h 127.0.0.1 -U ecas -d ecas \
     -c "SELECT model, ts, raw FROM raw_reading WHERE model='${MODEL}' AND raw=${RAW} ORDER BY ts DESC LIMIT 3;"
n_db=$(psql -X -qtAw -h 127.0.0.1 -U ecas -d ecas -c "SELECT count(*) FROM raw_reading WHERE model='${MODEL}' AND raw=${RAW}")

echo "--- ② 监视日志尾部 ---"
tail -n 3 "$LOG"
echo "--- ③ CSV 尾部 ---"
tail -n 2 "$CSV"

echo
if (( n_db > 0 )); then
    echo "✅ 落库自检通过：测试帧已进数据库（${n_db} 条）"
else
    echo "❌ 落库自检失败：数据库里没有这条（链路 MQTT→脚本→PG 有问题）"
fi

if (( KEEP )); then
    echo "ℹ️ KEEP=1 ⇒ 测试行保留在库里，请手动删："
    echo "   psql -h 127.0.0.1 -U ecas -d ecas -c \"DELETE FROM raw_reading WHERE model='${MODEL}' AND raw=${RAW};\""
else
    psql -X -qw -h 127.0.0.1 -U ecas -d ecas -c "DELETE FROM raw_reading WHERE model='${MODEL}' AND raw=${RAW};"
    echo "🧹 测试行已删除（库里仍剩 $(psql -X -qtAw -h 127.0.0.1 -U ecas -d ecas -c "SELECT count(*) FROM raw_reading WHERE model='${MODEL}'") 条真实数据）"
fi

(( n_db > 0 )) && exit 0 || exit 3
