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
./deploy-cloud-mqtt-relay.sh fw status  # 8883 来源白名单：unit 状态 + 规则 + 被拒计数 + 最近被拒来源
./deploy-cloud-mqtt-relay.sh fw stage   # 装白名单规则/单元（先 nft -c 校验，不生效）
./deploy-cloud-mqtt-relay.sh fw on      # 启用（8883 只放行白名单来源，其余 drop）
./deploy-cloud-mqtt-relay.sh fw off     # 关闭（仅排障：关闭期间 8883 对所有来源开放）
./deploy-cloud-mqtt-relay.sh fw allow 1.2.3.4/32   # 加白名单（改规则文件 + reload）
./deploy-cloud-mqtt-relay.sh fw deny 1.2.3.4/32    # 去白名单（不允许移除 127.0.0.1）
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

### 8883 来源白名单（2026-09-28 先生令「可以设定白名单」）

nftables 表 `inet ecas_relay`：`8883` 只放行 `127.0.0.1` + `180.108.106.0/24`，其余 `drop`（同时限速记日志，前缀 `ecas-relay-drop`）；
systemd `ecas-relay-firewall.service`（enabled、`Before=ecas-mqtt-relay.service`）负责开机持久化。

- **为什么是 /24 而不是单个 IP**：主 broker 近 3 天日志里 `dpro66-dtu` 的来源是 `180.108.106.44` / `.58` / `.59`（客户端 ID 恒为 `dpro66-01`）
  ⇒ 该段是现场出口**地址池**，写死单 IP 会在 DTU 重新拨号后断流。
- **为什么用 nft 而不是 iptables**：本机 iptables 是 **legacy** 后端（1017 行 Docker 规则），而 `nft list ruleset` 为空 ⇒ 两套命名空间互不干扰；
  且 nft 的 `drop` 是终局判决，Docker 的 `ACCEPT` 不会把包放过去。
- **实测（2026-09-28 14:29）**：回环 `127.0.0.1:8883` 连上；云机自连自己公网 IP、Mac 出口 `218.88.248.132` 连 8883 都超时；
  `counter packets 17`、内核日志 `ecas-relay-drop … SRC=218.88.248.132 … DPT=8883`；同期 `raw_reading` 继续入库（DTU 在白名单内，会话未断）。
- **误伤兜底**：`ubuntu-super` 上 `ecas-watchdog.sh` 的 `fresh` 判据（`STALE_MIN=10` 分钟，现场节拍 ~100s）会在 10 分钟内 `crit` 播报。

### 残留风险（未消除，如实记录）

明文口仍在公网（DTU 必须能连），只是不再由持有数据的 broker 承担。白名单已把随机来源的扫描/爆破挡在 TCP 层，
但**同运营商段内的主机**与运营商侧链路仍可看到明文；后续可做：轮换 `dpro66-dtu` 口令、采集侧加序号/时间戳做重放检测、
DTU 侧支持 TLS 后撤掉明文口（根治）。

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
| `tighten-security-group.py` | 腾讯云**安全组**收口（TC3 签名，纯标准库）：`show`/`plan` 只读，`apply --confirm` 才写并自动备份原规则；密钥只从环境变量读 |

## 三、历史备注（仍然有效）

1. **安全组**要在腾讯云控制台放行 `80`/`443`（脚本改不了）；云端 `firewalld` 未运行，不需要额外放行。
   ECAS 相关公网面收敛为 `80`/`443`（纯跳转）+ `8883`（DTU 必需，现由转发机承担）；EasyTier 的 `11010`/`12121` 按先生裁定不动。
   - **安全组收口（与主机侧同口径）**：主机侧 nftables 白名单已生效，安全组这一层用
     `python3 tighten-security-group.py show|plan` 看现状、`apply --confirm` 落地（把 `8883` 入站来源从 `0.0.0.0/0`
     改成 `180.108.106.0/24`，其余端口的规则原样保留，改动前自动备份 `tighten-sg-backup-<时间戳>.json`）。
     密钥只从 `TENCENTCLOUD_SECRET_ID/KEY` 读，建议用**只有 VPC 读写权限的 CAM 子用户**，用完即删。
   - **2026-09-28 已收口**：先生在控制台把 `8883` 入站来源从 `0.0.0.0/0` 收成现场运营商段。
     验证（本机没有腾讯云凭据也能判定）——**把测试源临时加进主机侧白名单再连**：
     `./deploy-cloud-mqtt-relay.sh fw allow 218.88.248.132/32` ⇒ 从该源连 `124.222.122.146:8883` **仍超时**
     ⇒ 挡在**安全组**这一层（而不是 nftables）；验证后 `fw deny 218.88.248.132/32` 还原
     （当前主机侧白名单 = `127.0.0.1, 180.108.106.0/24`）。同期数据面照旧（`raw_reading` 继续入库），
     公网入口不受影响（`http://124.222.122.146/` 仍 301、`gateway.xshky.org/login` 200）。
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
