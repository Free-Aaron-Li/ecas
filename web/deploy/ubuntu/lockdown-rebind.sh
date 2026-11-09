#!/usr/bin/env bash
# ECAS 测试栈 · 端口收口：把后端从 mesh 地址改绑回环 127.0.0.1
#
# 背景（2026-10-26 先生令）：网站只允许先生本人这台 Mac 访问，其他一律禁止。
#   - 后端只绑 127.0.0.1:18080 ⇒ 叠加网内其它成员、公网、VPC 隧道都连不上；
#   - 先生通过本机 launchd 常驻 SSH 隧道 `-L 18080:127.0.0.1:18080` 访问 http://127.0.0.1:18080
#     （身份＝本机 SSH 私钥）。
#
# 本脚本在目标机（ubuntu-Super-Server）以 root 运行；由 deploy.sh 的 sudo_script
# 同款方式喂进来（口令走 stdin，不进 argv）。幂等：已是 127.0.0.1 时不重复重建。
#
# 用法（在 Mac 上）：
#   pw=$(python3 -c 'import tomllib;print(tomllib.load(open("/Volumes/keys/ssh/ssh.toml","rb"))["ubuntu_Super_Server"])')
#   { printf '%s\n' "$pw"; cat web/deploy/ubuntu/lockdown-rebind.sh; } \
#     | ssh ubuntu-super "sudo -S -p '' bash -s"
set -euo pipefail

ENV_FILE=/opt/ecas/.env
STACK=/opt/ecas
PORT=$(grep -E '^ECAS_WEB_PORT=' "$ENV_FILE" | cut -d= -f2)
TS=$(date +%Y%m%d-%H%M%S)

echo "── 1) 备份 .env"
cp -p "$ENV_FILE" "$ENV_FILE.bak-lockdown-$TS"
echo "   备份 → $ENV_FILE.bak-lockdown-$TS"

echo "── 2) 改写 BIND_IP"
old=$(grep -E '^BIND_IP=' "$ENV_FILE" || true)
sed -i 's/^BIND_IP=.*/BIND_IP=127.0.0.1/' "$ENV_FILE"
new=$(grep -E '^BIND_IP=' "$ENV_FILE")
echo "   $old → $new"
# 把注释里那句「仍只绑 mesh 地址」也改成实情，避免下一个人误读
sed -i 's/仍只绑 mesh 地址/只绑回环，外部经本机 SSH 隧道访问/' "$ENV_FILE" || true

echo "── 3) 只重建 api 容器（db/collector 不动）"
cd "$STACK"
docker compose up -d --no-deps api

echo "── 4) 等健康"
for i in $(seq 1 20); do
  if docker compose ps --format '{{.Name}} {{.State}}' | grep -q 'ecas-api running'; then break; fi
  sleep 1
done
sleep 2

echo "── 5) 验证"
echo -n "   监听: "; ss -lntp | grep ":$PORT" || echo "(无监听！)"
echo -n "   容器: "; docker compose ps --format '{{.Name}} {{.State}} {{.Publishers}}' | grep api || true
echo -n "   本机 /healthz: "
curl -sS -m 5 -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:$PORT/healthz" || echo "(curl 失败)"
echo -n "   mesh 地址应拒连: "
if curl -sS -m 3 -o /dev/null "http://10.10.10.11:$PORT/healthz" 2>/dev/null; then
  echo "❌ 仍可从 mesh 地址访问！"
  exit 1
else
  echo "✓ 已拒绝"
fi

echo "── 完成：后端只绑 127.0.0.1:$PORT"
