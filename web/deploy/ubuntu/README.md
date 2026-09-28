# ECAS · Ubuntu Super Server 测试栈（A 方案：Docker Compose）

把「采集 → 后端 → 看板」整套搬到 **10.10.10.11（ubuntu-super，easytier mesh 内）**，三容器 + systemd 自启。
只绑 mesh 地址，公网/办公网都到不了。详细背景与踩坑见 [[ECAS部署与踩坑手册]] §5.7。

> **2026-09-26 架构切换（ADR 18）**：云端退居中转（只留 mosquitto + easytier + nginx 边缘），
> **数据库与前后端的唯一权威副本就在这台机**。云端历史 783 条已补全到这里并 md5 对账一致。
> 因此 **每日备份也在这里**（见下方「备份」一节）——它不再是「测试栈」，而是生产库。

## 一条命令

```bash
./deploy.sh all       # stage → push → creds → build → up → check
```

子命令：`stage`（本机组装 /tmp/ecas-stage）、`push`（rsync 到 /opt/ecas）、`creds`（从 /Volumes/keys 落三个 600 凭据）、
`build`、`up`（装 systemd 单元 + enable + restart）、`check`（六步自检）、`backup`（立刻备份一次）、
`watchdog`（立刻巡检一次）、`logs`、`all`。

## 结构

| 路径 | 说明 |
| --- | --- |
| `docker-compose.yml` | db（PostgreSQL 16）/ api（FastAPI + 前端静态产物）/ collector（TLS 取数 → dpro66_monitor.sh --pipe → 写库） |
| `backend/` | 后端镜像的 Dockerfile + `entrypoint.sh`（从 container secret 现造 `/tmp/pgpass`、导出 `ECAS_AUTH_TOKEN`） |
| `collector/` | 采集器镜像的 Dockerfile + `entrypoint.sh` + `mqtt_tls_feeder.py`；`dpro66_monitor.sh` 由 `deploy.sh` 从 `scripts/485/` 原样拷入 |
| `systemd/ecas-stack.service` | `Type=oneshot` + `docker compose up -d`，已 enable（开机自启） |
| `systemd/ecas-backup.{service,timer}` | 每天 **03:30**（`Persistent=true`）跑一次数据库备份；`enable --now` 由 `deploy.sh up` 负责 |
| `backup/ecas-backup.sh` | 备份本体：`pg_dump -Fc` → 校验 → 就位 + `sha256` → 轮转 30 天 |
| `systemd/ecas-watchdog.{service,timer}` | 每 **5 分钟**巡检一次（`OnBootSec=3min` + `OnUnitActiveSec=5min`） |
| `watchdog/ecas-watchdog.sh` | 看门狗本体：六项检查 + 状态变化落事件（详见下方「看门狗」） |
| `.env.example` | 有哪些非敏感键；真值由 `deploy.sh` 从密钥卷写入 `/opt/ecas/.env`（600） |

## 备份（库是唯一副本，这条最重要）

```bash
./deploy.sh backup                      # 立刻做一次（以 root 跑，与定时器同路径）
ssh ubuntu-super 'ls -lh /data/ecas/backups/'          # 产物
ssh ubuntu-super 'journalctl -u ecas-backup -n 30'     # 日志
```

产物 `/data/ecas/backups/ecas-YYYY-MM-DD.dump` + 同名 `.sha256`，保留 **30 天**；
先写 `.part`、用 `pg_restore -l` 验一遍可读、才 `mv` 就位 —— **绝不拿半截 dump 盖掉上一份好备份**。
恢复演练（另起临时库，不动生产库）：

```bash
ssh ubuntu-super 'cd /opt/ecas \
  && docker compose exec -T db createdb -U ecas ecas_drill \
  && docker compose exec -T db pg_restore -U ecas -d ecas_drill < /data/ecas/backups/ecas-<日期>.dump \
  && docker compose exec -T db psql -U ecas -d ecas_drill -Atc "select count(*), max(ts) from public.raw_reading" \
  && docker compose exec -T db dropdb -U ecas ecas_drill'
```

**待办**：备份目前只有本机一份，异地副本还没做（手册 §12.2 记着）。

## 看门狗（替云端 healthcheck）

```bash
./deploy.sh watchdog                            # 立刻巡检一次，打印六项结果 + 最近事件
ssh ubuntu-super 'journalctl -u ecas-watchdog -n 40'          # 历史汇总
ssh ubuntu-super 'tail -20 /var/log/ecas/watchdog.jsonl'      # 事件流水（只在状态变化时写）
```

盯六件事：`db`（库可达）、**`fresh`（最新数据距今 > 10 分钟＝断流，现场节拍 100s）**、
`collector`（三容器 running / 不 unhealthy / 15 分钟内重启 ≤ 3 次）、`api`（本机 `/healthz`=200）、
`backup`（最新 dump > 26 小时未更新）、`disk`（`/data`、`/`：85% 警告 / 92% 严重）。
阈值靠环境变量覆盖（`STALE_MIN`、`BACKUP_MAX_HOURS`、`DISK_WARN`、`DISK_CRIT`），想演练就临时调小：

```bash
ssh ubuntu-super 'sudo STALE_MIN=0 /opt/ecas/watchdog/ecas-watchdog.sh'   # 立刻报一次断流
```

事件同时落到本地库 `public.health_event`（看门狗自己建表），**只在「正常→异常 / 异常→恢复」时落一行**，
不刷屏；退出码恒为 0（现场 DTU 夜间停发属正常，不把 unit 打成 failed）。
**推送还没接**：`/opt/ecas/.env` 里设 `WATCHDOG_NOTIFY_CMD=/path/to/脚本` 即可在状态变化时收到一行消息，
现在没设 ⇒ 只发现+记录，不打扰。

## 三件容易踩的事

1. **端口 18080，不是 8080**：这台是生产机，`0.0.0.0:8080` 被 `side.base.apigateway` 占用。
   compose 用 `${BIND_IP:?...}:${ECAS_WEB_PORT:-18080}:8000`，`BIND_IP` 缺失时**故意启动失败**，防止退化成 0.0.0.0。
2. **采集器走 mesh 连云端**：8884 **未放行腾讯云安全组**（云端配置：「公网不可达，仅作就绪备用」），
   而 `server.crt` 的 SAN 里**没有** mesh 地址 `10.10.10.123` ⇒ 用「CA 链校验 + leaf 证书 SHA-256 指纹钉扎」
   替代主机名校验（`MQTT_CERT_SHA256`）。**云端重签 `server.crt` 后必须更新这个值**，否则采集器 fail closed 拒绝订阅：

   ```bash
   ssh ecas 'openssl x509 -in /etc/mosquitto/certs/server.crt -noout -fingerprint -sha256'
   # 把新值写进 deploy.sh 的 .env heredoc 与 .env.example，再 ./deploy.sh push && ./deploy.sh up
   ```
3. **`ssh host 'sudo bash -s'` 喂进来的脚本里，`docker compose exec -T` 要加 `< /dev/null`**：
   否则 exec 会把后续脚本流当自己的 stdin 吃掉，后面几条命令静默不执行。

## 凭据

`/opt/ecas/credentials/{pg-password,auth-token,mqtt-pass}`（目录 700、文件 600、root）。
目标机是 Ubuntu 22.04 / systemd 249，**没有 `systemd-creds`** ⇒ 先生同意此测试机破例用 600 明文
（记录在密钥卷 `/Volumes/keys/ecas/README.md` 与安全审计 #284）；升到 24.04+ 后改回
`systemd-creds encrypt --name=… --with-key=host` + `LoadCredentialEncrypted=`，compose 不用改。
