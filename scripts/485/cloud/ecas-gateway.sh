#!/bin/bash
# ECAS 云侧网关（2026-11-10 改造：DTU 落点从"直接 DNAT 到本地"改为"云内 relay"）
#   DTU 4G → 公网 :8883 → DNAT → 本地 10.10.10.11:8883（direct）或云采集器 10.10.10.123:18884（capture）
#     direct（本地在线）：DNAT 直达 10.10.10.11:8883
#     capture（本地离线）：DNAT 直达云侧采集器 10.10.10.123:18884，写 spool，恢复后回灌
#   来源白名单：180.108.106.0/24（现场 DTU）
# 用法：ecas-gateway.sh [direct|capture]   默认读 /etc/ecas/gateway-mode（缺省 direct）
#   direct = 回滚到改造前形态（DNAT 直达本地 10.10.10.11:8883）
set -u
DTU_NET="180.108.106.0/24"
LOCAL="10.10.10.11"
RELAY_IP="10.10.10.123"
CAP_PORT="18884"
MODE="${1:-$(cat /etc/ecas/gateway-mode 2>/dev/null || echo direct)}"

purge() {  # 按行号删除本脚本打的所有规则（幂等重放）
  local tbl=$1 chain=$2 num
  while iptables -t "$tbl" -L "$chain" --line-numbers -n 2>/dev/null | grep -q 'ecas-gateway'; do
    num=$(iptables -t "$tbl" -L "$chain" --line-numbers -n | grep 'ecas-gateway' | head -1 | awk '{print $1}')
    iptables -t "$tbl" -D "$chain" "$num" || return 1
  done
}
purge nat PREROUTING; purge nat POSTROUTING; purge filter FORWARD; purge filter INPUT

if [ "$MODE" = "direct" ]; then
  iptables -t nat -I PREROUTING 1 -i eth0 -s "$DTU_NET" -p tcp --dport 8883 \
    -m comment --comment ecas-gateway -j DNAT --to-destination "$LOCAL:8883"
  iptables -t nat -I POSTROUTING 1 -o tun0 -p tcp -d "$LOCAL" --dport 8883 \
    -m comment --comment ecas-gateway -j MASQUERADE
  iptables -I FORWARD 1 -i eth0 -o tun0 -s "$DTU_NET" -p tcp -d "$LOCAL" --dport 8883 \
    -m conntrack --ctstate NEW,ESTABLISHED -m comment --comment ecas-gateway -j ACCEPT
  iptables -I FORWARD 1 -i tun0 -o eth0 -p tcp -s "$LOCAL" --sport 8883 \
    -m conntrack --ctstate ESTABLISHED -m comment --comment ecas-gateway -j ACCEPT
else
  # capture：DTU 直连云侧采集器（不再经过 TCP 代理——代理路径实测会打乱本地"单主连接+注册包"语义）
  iptables -t nat -I PREROUTING 1 -i eth0 -s "$DTU_NET" -p tcp --dport 8883 \
    -m comment --comment ecas-gateway -j DNAT --to-destination "$RELAY_IP:$CAP_PORT"
  iptables -I INPUT 1 -i eth0 -s "$DTU_NET" -p tcp -d "$RELAY_IP" --dport "$CAP_PORT" \
    -m conntrack --ctstate NEW,ESTABLISHED -m comment --comment ecas-gateway -j ACCEPT
fi
iptables -C FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null \
  || iptables -I FORWARD 1 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
# 切换落点后重启云采集器：丢掉上一轮的残余连接，避免"已经切回 direct 了采集器还在写 spool"，
# 也保证切到 capture 时采集器是干净的单主连接状态（DTU 短连接会自动重连到新落点）。
systemctl try-restart ecas-fallback-capture.service 2>/dev/null || true
mkdir -p /etc/ecas && echo "$MODE" > /etc/ecas/gateway-mode
exit 0
