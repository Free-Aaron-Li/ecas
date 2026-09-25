#!/bin/bash
# ============================================================================
# ECAS 浓度计 —— MQTT broker（Mosquitto）部署脚本
# 目标系统：OpenCloudOS 9.4（RHEL 9 系，platform:oc9）
#
# 为什么不能用官方那套 `dnf install epel-release`：
#   EPEL 的 epel-release 包写死了 `Requires: redhat-release >= 9`，
#   而 OpenCloudOS 9 装的是 opencloudos-release —— 没有 redhat-release 这个包，
#   所以依赖不满足，EPEL 源挂不上，后面 mosquitto 自然全找不到。
#   （所有 RHEL 9 系「衍生发行版」都会撞上这个问题，跟系统本身无关。）
#
# OpenCloudOS 官方 FAQ 给的答案是：OC9 用 epol 源（注意拼写，不是 epel）
#   `sudo dnf install epol-release`
#   参考 https://docs.opencloudos.org/faq/   EPOL 仓库 https://mirrors.opencloudos.tech/epol/9/
#
# 用法：放到服务器上 chmod +x 后直接跑（需要 root）
# ============================================================================

set -u

step() { echo; echo "════════ $* ════════"; }

# ---------------------------------------------------------------------------
step "1/6 安装 OpenCloudOS 扩展源 EPOL"
# ---------------------------------------------------------------------------
dnf install -y epol-release || echo ">>> epol-release 也装不上，后面走手动挂仓库的兜底路径"

# ---------------------------------------------------------------------------
step "2/6 安装 mosquitto"
# ---------------------------------------------------------------------------
dnf makecache

if ! dnf install -y mosquitto; then
  echo
  echo ">>> EPOL 里没有 mosquitto，改用手动挂 EPEL 9 仓库（绕开 epel-release 的依赖）"
  curl -fsSL https://dl.fedoraproject.org/pub/epel/RPM-GPG-KEY-EPEL-9 \
       -o /etc/pki/rpm-gpg/RPM-GPG-KEY-EPEL-9

  # 注意：metalink 里写死 epel-9，不用 $releasever —— 免得 OC9 解析出 9.4 之类
  cat > /etc/yum.repos.d/epel-9-manual.repo <<'REPOEOF'
[epel-9-manual]
name=Extra Packages for Enterprise Linux 9 - $basearch (manual, no epel-release)
metalink=https://mirrors.fedoraproject.org/metalink?repo=epel-9&arch=$basearch
enabled=1
countme=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-EPEL-9
REPOEOF

  dnf clean all
  dnf makecache
  dnf install -y mosquitto
fi

# 客户端工具：Debian 系才把 mosquitto_pub/sub 拆成 mosquitto-clients，
# Fedora/EPEL 系通常在主包里自带 —— 所以做个兜底，装不上不致命。
command -v mosquitto_sub >/dev/null 2>&1 \
  || dnf install -y mosquitto-clients \
  || echo ">>> 没有单独的 mosquitto-clients 包（正常，主包应该已带）"

echo
echo "=== 安装结果 ==="
rpm -q mosquitto || echo "!!! mosquitto 仍未安装"
command -v mosquitto mosquitto_pub mosquitto_sub

# ---------------------------------------------------------------------------
step "3/6 写配置"
# ---------------------------------------------------------------------------
# ★ 关键：Mosquitto 2.x 起，如果不显式写 listener，默认只绑 127.0.0.1 ——
#   外网永远连不上。这一行是必须的。
mkdir -p /etc/mosquitto/conf.d
cat > /etc/mosquitto/conf.d/ecas.conf <<'CONFEOF'
listener 1883
allow_anonymous true
persistence true
persistence_location /var/lib/mosquitto/
log_dest file /var/log/mosquitto/mosquitto.log
CONFEOF

echo "--- /etc/mosquitto/conf.d/ecas.conf ---"
cat /etc/mosquitto/conf.d/ecas.conf

# ---------------------------------------------------------------------------
step "4/6 启动服务"
# ---------------------------------------------------------------------------
systemctl enable --now mosquitto
systemctl restart mosquitto
sleep 1
systemctl status mosquitto --no-pager | head -15

# ---------------------------------------------------------------------------
step "5/6 验收：监听端口 / 防火墙 / SELinux"
# ---------------------------------------------------------------------------
echo "--- ① 监听端口（必须是 0.0.0.0:1883 或 :::1883）---"
ss -tlnp | grep 1883 || echo "!!! 没有进程监听 1883——服务没起来或起错了"

echo
echo "--- ② 本机防火墙 ---"
if systemctl is-active --quiet firewalld; then
  firewall-cmd --add-port=1883/tcp --permanent && firewall-cmd --reload
  firewall-cmd --list-ports
else
  echo "firewalld 未运行 ✅ 少一道门（只要腾讯云控制台的防火墙开了就行）"
fi
echo "nftables 规则（看有没有拦 1883）："
nft list ruleset 2>/dev/null | grep -iE '1883|policy drop' | head -20 || echo "  （没读到 nft 规则）"

echo
echo "--- ③ SELinux ---"
getenforce

# ---------------------------------------------------------------------------
step "6/6 本机自测（下面应出现一行 test hello）"
# ---------------------------------------------------------------------------
timeout 4 mosquitto_sub -h 127.0.0.1 -p 1883 -t '#' -v &
sleep 1
mosquitto_pub -h 127.0.0.1 -p 1883 -t test -m hello
sleep 5

echo
echo "════════ 完成 ════════"
echo "接下来在您自己的电脑上验外网："
echo "  python3 scripts/485/mqtt_probe.py <这台服务器的公网IP> 1883"
echo "期待回复 4 字节: 20020000"
