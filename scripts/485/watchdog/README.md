# ECAS 本地看门狗（2026-11-10 起）

事故背景：11-10 13:48 本地服务器突然宕机，17:00 重启后 `/opt/ecas/mqtt/` 三份配置
（mosquitto.conf / passwd / acl.conf）已丢失，Docker 只把缺失的挂载源建成空目录，
`ecas-mosquitto` 退出码 127，浓度数据断流到 19:20（3h11m）。旧看门狗只查 compose
三容器（db/api/collector）与 raw_reading 新鲜度，既没发现 broker 挂了，也没区分
「流量正常、浓度断流」。

本目录是部署到 `/opt/ecas/watchdog/` 的源（systemd timer 每 5 分钟，root 运行）：

| 文件 | 作用 |
|---|---|
| `ecas-watchdog.sh` | 11 项检查：collector / config / broker / rtu / db / fresh / conc / flow / api / backup / disk；状态变化写 `/var/log/ecas/watchdog.jsonl`，仍未恢复每 `REPEAT_SECS`(1800s) 重播一次 |
| `ecas-mqtt-selfheal.sh` | MQTT 配置自愈：缺失先从 `/opt/ecas/mqtt/backup/` 还原，再不行用 `/opt/ecas/credentials/mqtt-pass` 与 `rtu-master/mqtt-pass` 重建口令哈希（值只走管道），然后拉起容器并验证 1883/8884 监听 |
| `ecas-watchdog.service/.timer` | systemd oneshot + 5 分钟定时 |
| `../mqtt/mosquitto.conf`、`acl.conf` | 修复时写入的配置模板（口令哈希不在版本库，由 selfheal 生成） |

阈值（脚本头可改）：`CONC_STALE_MIN=5`、`FLOW_STALE_MIN=10`、`RTU_STALE_SEC=180`、
`REPEAT_SECS=1800`。Mac 侧 `~/.files/dsh/bin/ecas-health-watch.sh` 每 5 分钟拉这两台
机器的 jsonl 并用 `notify.sh` 播报；急件集合含 conc/flow/broker/rtu/config。

历史缺口回灌：`python3 /opt/ecas/rtu-master/backfill_from_jsonl.py --file
/opt/ecas/rtu-master/logs/rtu-live.jsonl --since 2026-11-10T13:40 --until 2026-11-10T19:25
| docker compose exec -T db psql -U ecas -d ecas -v ON_ERROR_STOP=1`（PK 冲突即跳过，可反复跑）。

## 开机可靠自启（2026-11-10 第二层：整机重启也能起干净）

事故里 broker 起不来的直接原因是配置被删；为防「重启后某个服务没起来、没人知道」，加了两个单元：

| 文件 | 作用 |
|---|---|
| `ecas-boot-prepare.service` + `ecas-boot-prepare.conf`（drop-in） | 开机在 `ecas-stack.service` **之前**先跑一次 `ecas-mqtt-selfheal.sh`（`Before=ecas-stack.service`，经 drop-in 的 `Wants/After` 接线），保证 compose 起来时 broker 配置已完好 |
| `ecas-boot-check.service` + `boot-check.sh` | 栈与 flow-ingest 起来后等 45s 跑一次 `ecas-watchdog.sh`，结论写 `/var/log/ecas/boot-check.jsonl`（`watchdog_rc` / `bad` / `detail`），非 0 即让 Mac 侧当轮就看见 |

容器本身都是 `--restart unless-stopped`，重启后 docker 会拉起；`ecas-rtu-master` 是容器不是 systemd 单元。

## 云端兜底缓存（2026-11-10 第三层：本地整机离线也不丢数据）

链路：DTU 4G → 腾讯云 :8883 → **DNAT 落点按本地心跳切换** → 本地 `10.10.10.11:8883`（direct）或云采集器 `10.10.10.123:18884`（capture，写 spool）。

| 文件 | 位置 | 作用 |
|---|---|---|
| `../cloud/ecas-gateway.sh` | 云端 `/usr/local/sbin/` | 两种模式：`direct`（原行为，DNAT 直达本地）/ `capture`（DNAT 直达云采集器），切换后重启云采集器丢掉残余连接 |
| `../cloud/ecas-heartbeat-server.service` + `ecas-hb-server.py` | 云端 | 只监听叠加网 `10.10.10.123:18889`，收本地 60s 心跳，落 `heartbeat.json`（带 `recv_ts`，不依赖本地时钟） |
| `../cloud/ecas-gateway-flip.service/.timer` + `ecas-gateway-flip.sh` | 云端，每 60s | 心跳 ≤240s 且浓度 ≤300s、流量 ≤300s → `direct`；否则 → `capture`；每次切换写 `/var/log/ecas/fallback-flip.jsonl` |
| `../cloud/ecas-fallback-capture.service` | 云端 | 采集器（部署版 `rtu_master.py` 副本）监听 `10.10.10.123:18884`，只写 `/var/lib/ecas/fallback/spool/rtu-live.jsonl`（append，不发布 MQTT） |
| `../cloud/ssh-spool.sh` | 云端 | 受限 SSH 入口：只允许 `pull` / `size` / `rotate`，无 shell（authorized_keys 用 `command=...,restrict`） |
| `ecas-heartbeat.timer` + `ecas-heartbeat.sh` | 本地，每 60s | 查库算浓度/流量新鲜度、容器状态，POST 到云端 |
| `ecas-fallback-pull.timer` + `ecas-fallback-pull.sh` | 本地，每 15min | 用受限密钥 `~ubuntu/.ssh/ecas-spool-key` 拉云端 spool → `backfill_from_jsonl.py`（PK 幂等）→ 成功才写游标并 `rotate` 清空云端 |

判定与取舍：
- **不使用 TCP 代理**：最初写过 `ecas-dtu-relay.py`（透传/接管代理），实测会把本地 `rtu_master` 的「注册包 + 单主连接」语义打乱（6s 重连风暴、`握手失败：DTU 关闭了连接`、数据停），已弃用，改为 DNAT 直接切换。
- 切换依赖 DTU 重连：本地整机宕机时 TCP 自然断，DTU 几秒内重连即落到 capture；实测用 `iptables REJECT` 打断既有连接验证（spool 增长、恢复后回灌 14 conc / 32 flow 行）。
- 回灌用 `backfill_from_jsonl.py` 而不是让云侧发 MQTT：collector 内部语义不可控，PK 幂等 INSERT 更稳。
- 云端 `conntrack` 工具缺失（无 apt/yum 可用路径），切换时用「重启采集器 + DTU 重连」代替 flush。

运维命令：
```bash
# 云端：看当前落点与切换历史
cat /etc/ecas/gateway-mode; tail -5 /var/log/ecas/fallback-flip.jsonl
# 手动切（演练）：bash /usr/local/sbin/ecas-gateway.sh capture|direct
# 本地：看心跳与回灌
journalctl -u ecas-heartbeat.service -n 5; cat /var/log/ecas/fallback-pull.jsonl
```

Mac 侧提示（新）：`~/.files/dsh/bin/ecas-cloud-mode-watch.sh` + launchd `com.user.ecas-cloud-mode-watch`（300s）——
`capture` 首次出现播 alert「本地离线，云端已接管」，恢复播 info「DTU 切回直连」，直连但心跳超时播 warn。

## 2026-11-16 事故：一次部署把守护脚本删了（部署链修复）

现象：11-15 23:50 部署（提交 `7d9a60c`）后，云端 flipper 于 11-16 01:00 切 `capture`；
本地数据停在 01:00:05，看板整夜空白，且**永远回不到 direct**（切回判据要求心跳 ≤240s，
而心跳脚本已被删）。01:03–08:48 的数据全在云端 spool（4100 行），未丢。

根因（`web/deploy/ubuntu/deploy.sh`）：
- `cmd_stage` 只把 `ecas-watchdog.sh` 一个脚本放进 `$STAGE/watchdog/`；
- `cmd_push` 的 rsync 带 `--delete`，于是 `/opt/ecas/watchdog/` 里另外 4 个**只在目标机存在**
  的脚本（`ecas-heartbeat.sh` / `ecas-fallback-pull.sh` / `boot-check.sh` /
  `ecas-mqtt-selfheal.sh`）被整片删掉；
- 后果：`ecas-heartbeat` / `ecas-fallback-pull` / `ecas-boot-prepare` 三个单元
  `status=203/EXEC`（`Failed to locate executable ... No such file or directory`），
  心跳断 → 切 capture → 回灌也断 → 本地无法自愈。

修复（已提交）：
1. **本目录是守护脚本与 watchdog 单元的唯一源**：`cmd_stage` 把 `scripts/485/watchdog/`
   的 `*.sh`、`*.service`、`*.timer`、`*.conf` 全部进 STAGE（watchdog 单元不再从
   `web/deploy/ubuntu/systemd/` 取，避免两处副本漂移），并 `chmod 755` 补执行位
   （`cmd_stage` 末尾的 `chmod -R u=rwX,go=rX` 只保 x、不新增，644 推上去照样 203/EXEC）。
2. `cmd_up` 安装全部 13 个单元/conf（heartbeat / fallback-pull / boot-check / boot-prepare +
   drop-in）与 5 个脚本，`enable` 全部 timer，并在末尾回显「守护脚本清单 + 各 timer 状态」，
   缺一个当场可见。
3. `cmd_up` 把 `/opt/ecas/watchdog`、`/opt/ecas/systemd` 目录 `chown ubuntu:ubuntu`
   （rsync 以 ubuntu 推，root 属目录会 `Permission denied`，08:51 实测）。
4. `cmd_up` 增加 `BIND_IP` 告警：必须 `127.0.0.1`（11-04 起曾被改成 `10.10.10.11`，
   绕开 SSH 隧道把看板暴露在叠加网，`com.ecas.tunnel` 因此一直 `Connection refused`）。
5. 删掉 `web/deploy/ubuntu/watchdog/` 与 `web/deploy/ubuntu/systemd/ecas-watchdog.*` 三个陈旧副本。

复现/验证：`bash web/deploy/ubuntu/deploy.sh stage` → `/tmp/ecas-stage/watchdog/` 应有 5 个
可执行脚本、`systemd/` 13 个文件；`push` 后 `/opt/ecas/watchdog/` 5 个脚本仍在；`up` 末段回显全绿。

教训：**任何 rsync `--delete` 之前，先确认 STAGE 覆盖目标目录全量**；只看「目标机存在即完整」
的假设，就是这次断联的起点。
