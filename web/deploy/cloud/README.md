# ECAS 公网侧（腾讯云 124.222.122.146）

这一层只做两件事：**承受现场 DTU 的明文 MQTT 入口**（转发机）与**把公网 IP 入口 301 到正式域名**（边缘）。
数据的发布口是 Cloudflare Worker `gateway.xshky.org`（经 Workers VPC 直达 `10.10.10.11:18080`，不经过本机公网）。

```
现场 DPRO-66 ──▶ 塔石 DTU ── 明文 8883 ──▶ ecas-mqtt-relay（本机，只转发不落盘）
                                              └─ 桥接 TLS 8884（回环）─▶ 本机 mosquitto（持有数据）
                                                                              └─ 叠加网 ─▶ 10.10.10.11 采集器/数据库
浏览器 ──▶ https://gateway.xshky.org（CF Worker：账户/会话/只读中继）──▶ Workers VPC ──▶ 10.10.10.11:18080
浏览器 ──▶ http(s)://124.222.122.146（本机 ecas-edge，**纯 301**）──▶ https://gateway.xshky.org
```

## 一、MQTT 转发机 `ecas-mqtt-relay`（安全审查 H-01）

现场 DTU 只认明文 MQTT、端口写死 8883，**现场侧无法加密**。于是把「接受明文」从持有数据的 broker 上摘出来，
交给一个**不持有任何数据**的实例；主 broker 从此不再监听明文。

| 属性 | 值 |
| --- | --- |
| 监听 | `8883`（`0.0.0.0`，唯一监听；现场 DTU 入口） |
| 持久化 | **`persistence false` / `autosave_interval 0`** —— 不落盘、重启不留数据 |
| 队列 | `queue_qos0_messages false`、`max_queued_messages 100`、`max_inflight_messages 20`（只在内存里压，最多 100 条） |
| 认证/授权 | `allow_anonymous false`；口令文件只搬 `dpro66-dtu` 一行；ACL 默认拒绝，**只允许 `dpro66-dtu` 写 `dpro66`** |
| 桥接 | `connection ecas-main` → `127.0.0.1:8884`（**回环**，不出主机），TLS + `bridge_cafile /etc/mosquitto/certs/ca.crt`，账号 `ecas-bridge`（主 broker ACL 只给 `topic write dpro66`），只桥 `topic dpro66 out 1` |
| 服务 | `ecas-mqtt-relay.service`（`Type=notify`、`Restart=always`、`Before=mosquitto.service`） |
| 日志 | `/var/log/mosquitto/relay.log` |

安装位置：`/etc/mosquitto/relay/{relay.conf,passwd,acl.conf}`；`relay.conf` 是 **640 root:mosquitto**（其中含桥接口令明文——mosquitto 桥接的硬要求，按「服务器 secret 文件源」惯例处理）。

```bash
cd web/deploy/cloud
./deploy-cloud-mqtt-relay.sh stage      # 渲染并安装配置/单元（不动现网监听）
./deploy-cloud-mqtt-relay.sh test       # 临时 8885 口自检桥接（真机验证 TLS+账号+主 broker ACL 三关）
./deploy-cloud-mqtt-relay.sh swap       # 切换：主 broker 撤 8883 → relay 接管 8883
./deploy-cloud-mqtt-relay.sh verify     # 自检（监听归属 / 桥接 / 数据面是否继续入库）
./deploy-cloud-mqtt-relay.sh all        # stage + test + swap + verify
./deploy-cloud-mqtt-relay.sh rollback   # 回滚：停 relay 并恢复主 broker 的 8883 明文口
```

**自检判据**（`test` 输出）：日志出现 `Received CONNACK` **且** `Received PUBACK … RC:0`
⇒ TLS 证书链、`ecas-bridge` 账号、主 broker ACL 三关全通。

### 三个坑（都是实测踩出来的）

1. **桥接不能写 mesh 地址**：主 broker 服务器证书 SAN 只有 `IP:124.222.122.146`、`IP:127.0.0.1`、`DNS:VM-0-13-opencloudos`。
   写 `address 10.10.10.123:8884` 会 `Error: host name verification failed`（`SSL routines::certificate verify failed`）
   —— 主 broker 与转发机同机，**用 `127.0.0.1:8884`**（在 SAN 里）即可。
2. **`persistence` 必须保持 `false`**：mosquitto 默认 `persistence_location /var/lib/mosquitto/`，
   与主 broker 共用同一个 `mosquitto.db` —— 转发机一旦落盘就会**覆盖主 broker 的会话/队列库**。
3. **`conf.d/` 是坑区**：主 broker 的 `mosquitto.conf` 里 `include_dir` 那一行**是注释**（第 904 行），
   所以放 `conf.d/` 的配置**从未生效**（曾有一份 `allow_anonymous true` 的残留躺在那里）。改配置请认准 `/etc/mosquitto/relay/relay.conf`。

### 残留风险（未消除，如实记录）

明文口仍在公网（DTU 必须能连），只是不再由持有数据的 broker 承担。窃听者仍可能拿到 `dpro66-dtu` 口令并**向 `dpro66` 投毒**；
后续可做：轮换 `dpro66-dtu` 口令、给 8883 加安全组来源 IP 白名单（DTU 出口 IP `180.108.106.59`，需确认是否固定）、采集侧加序号/时间戳做重放检测。

## 二、公网 IP 入口 `ecas-edge`（安全审查 H-03）

自签证书阶段**不再反代**，只做 `301 → https://gateway.xshky.org`（并保留本机 `location = /healthz` 供容器健康检查，不依赖外网）：

```bash
./deploy-cloud-edge.sh all      # 打包 → 生成自签证书 → 起容器 → 自检
```

| 子命令 | 作用 |
| --- | --- |
| `stage` | 渲染配置并推送到云端 `/opt/ecas-edge/`（`rsync`） |
| `cert` | 在云端生成自签证书（已存在则复用），打印 SAN 与指纹 |
| `up` | `docker compose up -d`（幂等，可重复跑） |
| `verify` | 自检（期望 `/`·`/docs` 为 **301**、`/healthz` 为 **200**） |
| `logs` / `down` | 看边缘访问日志 / 停掉边缘 |

⚠️ **改挂载的 `ecas-edge.conf` 后必须 `docker compose restart edge`**：`up -d` 只对比 compose 定义，
配置文件变了**不会重建容器**，行为会一直是旧的（实测踩过：容器显示 `Up 38 hours`、配置已换但仍是旧行为）。

| 文件 | 说明 |
| --- | --- |
| `docker-compose.yml` | 边缘服务：`nginx:1.27-alpine` + `network_mode: host`（直绑 80/443）+ 健康检查 |
| `nginx/ecas-edge.conf` | 纯跳转模板：80/443 → 301 正式域名；只留本机 `/healthz` |
| `deploy-cloud-edge.sh` | 部署与自检脚本（在 Mac 上跑，`ssh ecas` 直达云端 root） |
| `mqtt-relay/*`、`deploy-cloud-mqtt-relay.sh` | MQTT 转发机（见上） |

## 三、历史备注（仍然有效）

1. **安全组**要在腾讯云控制台放行 `80`/`443`（脚本改不了）；云端 `firewalld` 未运行，不需要额外放行。
   ECAS 相关公网面收敛为 `80`/`443`（纯跳转）+ `8883`（DTU 必需，现由转发机承担）；EasyTier 的 `11010`/`12121` 按先生裁定不动。
2. **自签证书阶段故意不开 HSTS**（否则浏览器被钉在告警页）；HSTS 由正式域名侧（Worker `secHeaders()`）下发。
   未拿域名前，IP 入口的证书告警仍然存在，但**流量只到跳转为止**，不再承载会话与数据。
3. **为什么用容器而不是装 nginx 包**：这台云主机是 **OpenCloudOS 9.4**（RHEL 系），仓库里没有 nginx 包；
   转发机用的是主机自带的 `mosquitto 2.0.18` 二进制（`/usr/sbin/mosquitto`）+ systemd 单元。
4. **令牌别进日志**：边缘的 `log_format ecas` 只取 `$uri`（去掉 `?token=`），改配置时不要退回默认格式。

## 将来拿到域名（把 IP 入口换成可信证书）之后

1. 域名 A 记录指向 `124.222.122.146`，安全组保持 80/443 放行；
2. 云端用 certbot 走 webroot 签发（配置已预留 `location /.well-known/acme-challenge/` → `/var/www/certbot`）；
3. 换掉自签证书路径、打开 HSTS 注释，重新 `stage` + `up` + **`restart`**；
4. 那时可直接让 443 反代回 `10.10.10.11:18080`（或继续只跳转——两者的取舍取决于是否要保留「Worker 之外的第二条入口」）。
