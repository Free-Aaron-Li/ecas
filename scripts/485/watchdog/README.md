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
