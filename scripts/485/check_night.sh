#!/usr/bin/env bash
# ECAS 一夜数据体检 —— 早上看一眼「有没有正常读入」
#
# 用法（Mac 上直接跑，免密 ssh 到 ecas）：
#     bash scripts/485/check_night.sh              # 全部检查
#     bash scripts/485/check_night.sh --hours 12   # 只看最近 12 小时
#
# 只读：不写库、不重启任何东西。
set -uo pipefail

HOST="ecas"                     # ~/.ssh/config 里的别名 → root@124.222.122.146
HOURS=24
while [ $# -gt 0 ]; do
  case "$1" in
    --hours) HOURS="$2"; shift 2 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
done

need() { command -v "$1" >/dev/null 2>&1 || { echo "❌ 本机缺 $1"; exit 2; }; }
need ssh

ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" "HOURS=$HOURS bash -s" <<'REMOTE'
set -uo pipefail
PSQL="psql -X -w -h 127.0.0.1 -U ecas -d ecas -P pager=off"
line() { printf '%s\n' "────────────────────────────────────────────────────────────"; }

line; echo "① 服务与端口"
systemctl is-active ecas-monitor ecas-api mosquitto postgresql postgresql 2>/dev/null | tr '\n' ' ' | sed 's/^/   单元 active: /'; echo
ss -tlnp 2>/dev/null | awk '/:1883|:5432|:8080/ {print "   "$4}' | sort -u

line; echo "② 入库总量与时间跨度（最近 ${HOURS} 小时）"
$PSQL -c "SELECT count(*) AS 行数, min(ts) AS 最早, max(ts) AS 最新,
                 now() - max(ts) AS 距今, round(avg(raw)) AS 平均raw
          FROM raw_reading WHERE ts > now() - make_interval(secs => $HOURS * 3600);"

line; echo "③ 每小时入库量（有没有整段断流）"
$PSQL -c "SELECT date_trunc('hour', ts) AS 小时, count(*) AS 行数, round(avg(raw)) AS 平均raw
          FROM raw_reading WHERE ts > now() - make_interval(secs => $HOURS * 3600)
          GROUP BY 1 ORDER BY 1;"

line; echo "④ 最近 12 条（含换算：浓度 = 未补偿 + 0.3）"
$PSQL -c "SELECT to_char(ts,'MM-DD HH24:MI:SS') AS 时间, raw,
                 round(raw/1000.0,3) AS ma,
                 round((raw-4000)/160.0,3) AS 未补偿,
                 round((raw-4000)/160.0+0.3,3) AS 浓度,
                 ts - lag(ts) OVER (ORDER BY ts) AS 间隔,
                 CASE WHEN raw < 3600 THEN '断线' WHEN raw < 4000 THEN '偏低'
                      WHEN raw > 20500 THEN '超量程' ELSE '正常' END AS 状态
          FROM raw_reading ORDER BY ts DESC LIMIT 12;"

line; echo "⑤ 上报节奏（间隔分布；DTU 应稳定在 100 秒）"
$PSQL -c "WITH g AS (SELECT ts - lag(ts) OVER (ORDER BY ts) AS gap FROM raw_reading)
          SELECT gap AS 间隔, count(*) AS 次数 FROM g WHERE gap IS NOT NULL
          GROUP BY 1 ORDER BY 2 DESC LIMIT 8;"

line; echo "⑥ 异常值计数（断线 / 超量程）"
$PSQL -c "SELECT count(*) FILTER (WHERE raw < 3600)  AS 断线,
                 count(*) FILTER (WHERE raw > 20500) AS 超量程,
                 count(*) FILTER (WHERE raw BETWEEN 4000 AND 20500) AS 有效
          FROM raw_reading WHERE ts > now() - make_interval(secs => $HOURS * 3600);"

line; echo "⑦ 接口自检（走 ecas-api）"
curl -s --max-time 5 http://127.0.0.1:8080/api/health | head -c 400; echo
curl -s --max-time 5 "http://127.0.0.1:8080/api/summary?hours=$HOURS" | head -c 300; echo

line; echo "⑧ 看门狗最新状态（只在变化时留痕）"
tail -n 3 /root/ecas/logs/health.log 2>/dev/null | cut -c1-200

line; echo "⑨ DTU 认证情况（最近 5 分钟被拒次数 / 最近一次连接）"
awk -F: -v now="$(date +%s)" '/not authorised/ && ($1+0) > now-300 {n++} END {printf "   最近 5 分钟被拒：%d 次\n", n+0}' \
    /var/log/mosquitto/mosquitto.log 2>/dev/null
grep -a "dpro66-01" /var/log/mosquitto/mosquitto.log 2>/dev/null | tail -n 2 | cut -c1-110

line; echo "⑩ 定时器与备份"
systemctl list-timers --no-pager 2>/dev/null | grep -E 'pgdump|healthcheck|NEXT' | cut -c1-110
ls -l /root/ecas/archive/db/ 2>/dev/null | tail -n 4
echo "   磁盘：$(df -h / | awk 'NR==2 {print $3" 已用 / "$2" 总量，可用 "$4}')"
line
REMOTE
