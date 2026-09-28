# ECAS 浓度计监测系统 安全审查报告

| 项目 | 内容 |
| --- | --- |
| 审查日期 | 2026-09-28（08:00–09:00 CST） |
| 审查对象 | ECAS 浓度计采集链路（现场仪表 → DTU → MQTT → 采集器 → PostgreSQL）+ 网页发布层（FastAPI + nginx 边缘 + Cloudflare Worker） |
| 审查范围 | 公网暴露面、主机与容器加固、MQTT/数据库、后端与 Worker 代码、前端、凭据与仓库泄露面、备份 |
| 审查方法 | 外部端口/协议探测 → SSH 进入两台主机复核 → 容器/文件权限取证 → 源码逐行审查（以 `HEAD` 版本为准）→ 未授权面实测 |
| 审查人 | AI 助手（应先生指令执行） |
| 结论 | **架构分层与凭据管理整体做得很好**（令牌不进浏览器、容器只读、secrets 0600、ACL 最小权限、DB 不发布端口均已实证）。**但存在 3 项须尽快处理的高风险**：① 现场 DTU 到云端走**明文 MQTT**（8883）承载设备口令与数据；② ECAS 与另一套数十容器的生产栈**同机**且 `ubuntu ∈ docker`（爆炸半径过大）；③ 公网 IP 入口使用**自签证书**且 HSTS 被注释。另有 8 项中危、10 项低危。 |

---

## 0. 一页结论

### 0.1 必须优先处理的三件事

1. **H-01 采集侧明文 MQTT**：`mosquitto.conf:918 listener 8883` 是**无 TLS** 的现场入口，DTU 的用户名/口令在该连接上以明文传输，且 ACL 允许 `dpro66-dtu` 写主题 → 链路窃听者可**伪造/重放浓度数据**（数据投毒），也可窃取只读账号口令读走全部数据。
2. **H-02 共享主机**：测试机 `ubuntu-Super-Server`（10.10.10.11）上同时运行另一项目的数十个 `0.0.0.0` 发布容器（nacos/mysql/redis/rabbitmq/zlmedia/apigateway…），且 `ubuntu` 用户属于 `docker` 组 ⇒ **任一邻居容器被打穿 ≈ 拿到 ECAS 全库、令牌与备份**。ECAS 自身的容器加固（只读根、no-new-privileges）不能抵御宿主机层的横向移动。
3. **H-03 IP 入口的传输安全**：公网 `https://124.222.122.146` 使用**自签证书**（`CN=124.222.122.146`，2026-09-26 签发），nginx 里 HSTS 被注释掉；实测该链路中间还存在设备注入响应（见 O-01）。**建议：IP 入口只保留 301 跳转到 `https://gateway.xshky.org`，或直接关闭。**

### 0.2 风险统计

| 等级 | 数量 | 编号 |
| --- | --- | --- |
| 高 | 3 | H-01、H-02、H-03 |
| 中 | 8 | M-01 ~ M-08 |
| 低 | 10 | L-01 ~ L-10 |
| 观察/提示 | 3 | O-01 ~ O-03 |

### 0.3 做得好、请继续保持的部分（摘要）

- **真令牌永不出现在浏览器**：Worker 给浏览器看的只是占位符 `account-session`，上游请求由 Worker 注入 `Authorization: Bearer <ECAS_TOKEN>`；会话是 HMAC 签名 + `HttpOnly; Secure; SameSite=Lax` 的 cookie。
- **会话每请求回查数据库**，并用 `se` 版本号比对 ⇒ 改密/停用**即时失效**（worker.js:216）。
- **数据库端口三重取证未被发布**（主机无监听、compose 无 ports、容器 `PortBindings: {}`；Mac 连接 `ConnectionRefused`）。
- **内部账户接口 fail-closed**：`/internal/accounts*` 令牌未配置时直接 503（`accounts.py:328`），实测无令牌一律 401。
- **容器只读根 + no-new-privileges + 无额外 capability + 端口只绑 mesh 地址**（`BIND_IP:?` 强制）；secrets 均为 `0600 root`，`/opt/ecas/credentials` 为 `0700 root`。
- **MQTT 最小权限 ACL**：DTU 只写、采集器只读；匿名 CONNECT 在公网 8883、本机 1883/8883/8884 实测**全部 rc=5 拒绝**。
- **仓库无机密泄露**：git 历史无 `.env`/私钥（只有 `.env.example`），`.gitignore:98` 覆盖 `web/backend/.env`，Worker `vars` 为空（密钥走 secret store），前端无 `innerHTML/eval` 类 XSS sink。

### 0.4 处置进度（2026-09-28，先生令「立刻按照建议解决」）

| 项 | 处置 | 证据 |
|---|---|---|
| **M-01** 生产接口文档公开 | ✅ **已关闭** | `settings.enable_docs=False` + `_docs_kwargs()` 在构造 FastAPI 时就置 `docs_url/redoc_url/openapi_url=None`；线上实测 `/docs`·`/redoc`·`/openapi.json` 全 **404**（改前 200） |
| **M-02** 单令牌通吃对外与内部 | ✅ **已分离** | 新增 `ECAS_INTERNAL_TOKEN`；后端 `require_internal_token` 优先用它；Worker 只对 `/internal/accounts*` 注入内部令牌（`internalToken(env)`，未设则回退）；线上实测：无令牌 **401** / **对外令牌 401** / 内部令牌 **200**；`viewer` 真登录经网关 **302** |
| **M-03** 令牌缺失时 fail-open | ✅ **已改 fail-closed** | `token_ok()` 无期望值即 False；`/api/*` 未配置令牌返回 **503**（文案指路 `ECAS_ALLOW_NO_AUTH=1`）；`entrypoint.sh` 令牌文件缺失/为空 **`exit 1`**，不再「警告后裸跑」；本机四组裸跑 + 真进程探针复现通过；单测 77 passed |
| **H-03** IP 入口自签证书 + 无 HSTS | ✅ **已降级为纯跳转** | `web/deploy/cloud/nginx/ecas-edge.conf` 删掉全部 `proxy_pass`：80/443 一律 **301 → https://gateway.xshky.org**，只留本地 `location = /healthz` 返回 200（健康检查不能依赖外网）；云端实测 `/`·`/docs` **301**、`/healthz` **200**；HSTS 改由**正式域名**侧下发（Worker `secHeaders()` 增 `strict-transport-security: max-age=15552000`，不加 `includeSubDomains`），自签侧继续不开（写进注释说明原因） |
| **L-07** mosquitto `conf.d/ecas.conf` 匿名残留 | ✅ **已移出** | 云机 `/etc/mosquitto/conf.d/ecas.conf` → `/etc/mosquitto/ecas.conf.disabled-20260928-110154`；确认 `mosquitto.conf` 的 `include_dir` 仍是注释（该文件此前从未生效） |
| **H-01** DTU 明文 8883 | ✅ **已按先生方案处置（独立转发机）** | 明文口从持有数据的 broker 摘出，交给只转发、**不落任何数据**的 `ecas-mqtt-relay`（腾讯云机）：现场 DTU → 明文 8883 → relay（`persistence false`、无本地订阅者、ACL 只允许 `dpro66-dtu` 写 `dpro66`）→ **TLS 8884（桥接账号 `ecas-bridge`）** → 主 broker。主 broker 的 `listener 8883` 已注释（实测 `grep -c '^listener 8883'` = **0**）。线上实测：8883 归属 relay（pid 2635348）、`New bridge connected … u'ecas-bridge'`、DTU `dpro66-01 (u'dpro66-dtu')` 重新连上 relay、`raw_reading` 继续增长（11:13:32 → 11:18:32 共 +2 条，切换仅丢约 2 拍）。**追加：nftables 来源白名单**（8883 只放行 `127.0.0.1` + 现场段 `180.108.106.0/24`，其余 drop + 限速日志；实测 Mac 出口连接超时、`counter packets 17`、数据继续入库） |
| **H-02** 共享生产机 + `ubuntu ∈ docker` | ⛔ 先生明确不迁移 | 老项目太多、无法搬迁；缓解措施见 §4「短期」 |

---

## 1. 资产与信任边界

### 1.1 数据链路（现场 → 屏幕）

```
DPRO-66 浓度计 (4~20 mA)
   └─ 塔石 DTU (TAS-KS-301)  ── 明文 MQTT 8883 ──▶  ecas-mqtt-relay（腾讯云机，只转发）
       用户 dpro66-dtu（只写 dpro66）                   ├─ persistence false（不落盘）
                                                          ├─ ACL 只允许 dpro66-dtu 写 dpro66
                                                          └─ 桥接 TLS 8884（账号 ecas-bridge，只写 dpro66）
                                                                   │
                                                                   ▼
                          腾讯云 mosquitto 2.x（持有数据的一侧）
                                                          ├─ ~~8883 明文~~（已注释，不再监听）  ✅ H-01
                                                          ├─ 8884 TLS（叠加网入口）
                                                          └─ 1883 仅 127.0.0.1
                                                                   │
                                                                   用户 ecas-monitor（只读 dpro66）
                                                                   ▼
                        10.10.10.11:18080  ecas-collector  ── TLS 8884 + 证书指纹钉扎 ──┘
                             │  paho(mqtt_tls_feeder.py) → dpro66_monitor.sh --pipe（offset 0.48）
                             ▼
                       PostgreSQL 16（ecas-db，仅容器网络，未发布端口）
                             ▲
                             │  DSN 不含口令，走 PGPASSFILE
                        ecas-api（FastAPI，只读根 + tmpfs，10.10.10.11:18080）
                             ▲
                     ┌───────┴─────────────────────────────────┐
        腾讯云 nginx ecas-edge (80/443)              Cloudflare Worker gateway
        「IP 入口」→ proxy_pass 18080                 https://gateway.xshky.org（域名入口）
        ⚠️ H-03 自签证书 / 无 HSTS                    ✅ 有效证书(GTS WE1) + 登录页 + 账户层
        ⚠️ M-01 /docs、/openapi.json 无需认证可读      ✅ 未登录一律 401
```

### 1.2 三层访问入口（实测）

| 入口 | 地址 | TLS | 未登录可读 | 说明 |
| --- | --- | --- | --- | --- |
| 域名入口（正式） | `https://gateway.xshky.org` | ✅ GTS WE1（`*.xshky.org`，有效至 2026-12-26） | `/`、`/admin`、`/api/*`、`/docs`、`/openapi.json`、`/internal/*` **全部 401** | Worker 白名单转发，含登录页与账户会话 |
| IP 入口（备用） | `https://124.222.122.146` | ⚠️ 自签 | `/healthz`、**`/docs`、`/openapi.json`、`/redoc` 均 200**；`/api/*`、`/internal/*` 401 | nginx `location /` 全量代理，未屏蔽文档与内部路径 |
| 叠加网直连 | `http://10.10.10.11:18080` | — | 同上（`/docs` 200） | mesh 成员可达；ECAS 的「内网」即 mesh |

### 1.3 主机与容器清单（审查时）

| 主机 | 角色 | 关键事实 |
| --- | --- | --- |
| `124.222.122.146`（`VM-0-13-opencloudos`，OpenCloudOS 9.4） | 公网入口 + MQTT Broker | 运行 `ecas-edge`(nginx, host 网络)、`mosquitto`、`easytier`、`easytier-web`；`*:8888` 为 BT-Panel（仅安全组遮挡）；sshd 禁止口令、root 仅证书、`maxauthtries 3` + sshguard 2.4.2 |
| `10.10.10.11`（`ubuntu-Super-Server`，Ubuntu 22.04.5） | ECAS 应用主机（**同时是另一项目的生产机**） | `ecas-api`(read_only, no-new-privileges, 仅绑 10.10.10.11:18080)、`ecas-collector`、`ecas-db` + 数十个邻居容器；`docker:x:138:ubuntu` |
| Mac（审查发起端） | 运维终端 | FileVault 已开；密钥卷 `/Volumes/keys`（disk5s1）自身 `Encrypted: No` |

---

## 2. 发现清单

> 每条格式：**等级 · 编号 · 标题** → 证据 → 影响 → 建议。
> 「证据」均为本次实测或源码行号，可复现（复现命令见 §5）。

### 2.1 高风险

#### H-01 · 采集侧明文 MQTT 承载设备口令与数据

**证据**

- 云端 `mosquitto.conf:918`：`listener 8883` —— **无 `cafile/certfile/keyfile`**（对照 `:922-925 listener 8884` 带完整 TLS 配置）。
- `:908 allow_anonymous false`、`:912 password_file /etc/mosquitto/passwd` ⇒ 该端口要求用户名+口令，但**口令在明文 TCP 上传输**。
- ACL（`/etc/mosquitto/acl.conf`）：`user dpro66-dtu → topic write dpro66`（可写）；`user ecas-monitor → topic read dpro66`。
- 公网 8883 从外部**可连接**（TCP 通、握手后匿名 CONNECT 被 `rc=5` 拒绝 ⇒ 认证生效，但通道是明文）。

**影响**

- **数据完整性（主要）**：任何在 DTU↔云端路径上的观察者（运营商侧、中间网络设备、云主机前置链路）都可抓到 `dpro66-dtu` 的口令，然后**任意伪造/重放浓度与报警数据**。产线报警的可信度直接受损。
- **机密性（次要）**：同一路径可抓到 `ecas-monitor` 的只读口令（若两者口令相同），从而读走全部数据流；口令若复用还能横向到其它系统。
- 无 TLS 也意味着无完整性保护：即使不知口令，重放历史报文（若 Broker 无去重）也会污染数据。

**建议**

1. 现场入口迁到 TLS：让 DTU 走 `8884`（服务端证书已就绪），或给 DTU 加客户端证书 / 强口令；若固件不支持 TLS，则把 DTU 接入一条加密隧道（WireGuard/EasyTier 内部网）后只允许隧道内访问 Broker。
2. **每台设备独立凭据**（现在全网共用 `dpro66-dtu`），并为内容侧加时间戳/序号字段，采集端做重放与跳变检测（现有「冒名检测」watchdog 可作为基础）。
3. 轮换 `dpro66-dtu` / `ecas-monitor` 口令（假设 8883 期间的口令已可能被观察）。
4. Broker 侧打开审计日志（`log_type all`），保留 CONNECT/PUBLISH 来源，便于事后溯源。

**处置（2026-09-28 已落地，先生裁定「单独采用 1 台服务器做数据转发，不直接触碰原始数据」）**

DTU 固件只认明文 MQTT、连端口都写死 8883 ⇒ 现场侧无法加密，于是改**换手**：让一个不持有数据的实例去承受明文入口。

| 环节 | 事实 |
|---|---|
| 拓扑 | `现场 DTU --明文 8883--> ecas-mqtt-relay --TLS 8884(桥接账号 ecas-bridge)--> 主 broker --...--> 采集器` |
| 转发实例的硬性质 | `persistence false` / `autosave_interval 0`（**不落盘**，重启不留数据）；无本地订阅者；`queue_qos0_messages false` + `max_queued_messages 100`（内存最多压 100 条）；ACL 默认拒绝、只有 `dpro66-dtu` 能写 `dpro66` |
| 主 broker | `listener 8883` 已注释（`grep -c` = 0）；8884 TLS（`ecas-monitor` 只读采集、`ecas-bridge` 只写桥接）、1883 仍只绑 127.0.0.1 |
| 凭据 | 新增桥接账号 `ecas-bridge`：口令 32 字符 base64url 双存（`/Volumes/keys/ecas/mqtt.toml` + 钥匙串 `ecas/mqtt.toml#bridge_password`，sha256[:12]=`07b4e8712f52`）；服务器上只有 `/etc/mosquitto/relay/relay.conf`（640 root:mosquitto）持有桥接口令明文——mosquitto 桥接的硬要求 |
| 交付物 | `web/deploy/cloud/mqtt-relay/{relay-mosquitto.conf,relay-acl.conf,ecas-mqtt-relay.service,ecas-relay-firewall.nft,ecas-relay-firewall.service}` + `web/deploy/cloud/deploy-cloud-mqtt-relay.sh`（`stage/test/swap/verify/status/logs/rollback/all` ＋ `fw stage/on/off/status/allow/deny`） |
| 来源白名单（2026-09-28 追加上线） | nftables `table inet ecas_relay`：`8883` 只放行 `127.0.0.1` + `180.108.106.0/24`，其余 `drop` + 限速日志（`ecas-relay-drop`，6/分钟）；systemd `ecas-relay-firewall.service`（enabled，`Before=ecas-mqtt-relay.service`）持久化。依据：主 broker 近 3 天日志里 `dpro66-dtu` 来源为 `180.108.106.44/.58/.59`（客户端 ID 恒为 `dpro66-01`）⇒ 该段是现场出口**地址池**，写死单 IP 会在 DTU 重拨后断流。选 nft 不用 iptables 的原因：本机 iptables 是 legacy 后端（1017 行 Docker 规则）、`nft list ruleset` 为空 ⇒ 互不干扰，且 nft 的 `drop` 是终局判决。实测：回环连接成功、云机自连公网 IP 超时、Mac（出口 `218.88.248.132`）超时、`counter packets 17`、内核日志 `SRC=218.88.248.132 … DPT=8883`、同期 `raw_reading` 继续前进。**追加：腾讯云安全组同口径收口**（2026-09-28 先生在控制台把 `8883` 入站来源从 `0.0.0.0/0` 改为 `180.108.106.0/24`，备注 `DTU STL`，先生截图核对）—— 验证方法：把测试源临时加进**主机侧**白名单（`fw allow 218.88.248.132/32`）后仍连不上 ⇒ 包被**安全组**丢弃（第一道门确实收紧），验证后已 `fw deny` 还原为 `{ 127.0.0.1, 180.108.106.0/24 }`；同期数据面照旧（`raw_reading` 距 `now()` 27 秒）、公网入口不受影响（`http://124.222.122.146/` 301、`gateway.xshky.org/login` 200、无令牌 `/api/params` 401） |
| 线上证据 | 8883 socket 归属 `mosquitto` pid 2635348 = relay；relay 日志 `New client connected from 180.108.106.59 as dpro66-01 (…, u'dpro66-dtu')`；主 broker 日志 `New bridge connected from 127.0.0.1:45438 as VM-0-13-opencloudos.ecas-main (…, u'ecas-bridge')`；切换后 `raw_reading` 由 11:13:32（2160 行）继续到 11:18:32（2162 行）⇒ 数据面未断（切换仅丢约 2 拍） |
| 缩小暴露 | 明文口只存在于「无数据、单主题可写」的实例上；桥接走**回环** `127.0.0.1:8884`（不出主机）；ECAS 公网面收敛为 80/443（纯 301 跳转）+ 8883（DTU 必需） |

**残留风险（如实记录，未消除）**

- 明文口本身仍在公网（DTU 必须能连），只是不再由持有数据的 broker 承担 ⇒ 窃听者仍可拿到 `dpro66-dtu` 口令并**向 `dpro66` 投毒**（与处置前同级的完整性风险），减少的是「一台无数据实例被攻破 ⇒ 拿不到数据/拿不到高权凭据」。
- **已收窄但未消除**：2026-09-28 追加 nftables 来源白名单（只放行现场运营商段 `180.108.106.0/24` + 回环）⇒ 随机来源的扫描/爆破已被挡在 TCP 层（实测 Mac 出口 `218.88.248.132` 连接超时、`counter` 递增）；但**同段内主机**与运营商侧链路仍在明文覆盖范围内，且该段是地址池、DTU 换段即断流（靠 `ecas-watchdog.sh` 的 `fresh` 10 分钟判据兜底）。
- 建议后续：轮换 `dpro66-dtu` 口令（本次未做）、采集侧加序号/时间戳做重放与跳变检测、DTU 侧支持 TLS 后撤掉明文口（根治）。

#### H-02 · ECAS 与另一套多容器生产栈同机，`ubuntu ∈ docker`

**证据**

- 测试机 `docker ps` 中除 ECAS 三容器外，还有 `side.*`、nacos(8848/9848-9849)、mysql8(3306)、redis(6379)、rabbitmq(5672/15672)、zlmedia(1935/8081/17999-18029)、apigateway(8080)、xxljob(40000)、fastdfs-nginx(8099/22122/23000)、gadltools(8096)、radartools(38000)、cloudflared 等数十个容器，**大量以 `0.0.0.0` 发布**。
- `getent group docker` → `docker:x:138:ubuntu` ⇒ **ubuntu 用户等效 root**（本次审查的跳板访问正是依赖它在主机上执行 `docker exec`）。
- 同一主机上：PostgreSQL 数据目录 `/data/ecas/pgdata`、备份 `/data/ecas/backups`、容器 secrets `/opt/ecas/credentials`（0700 root）、`/opt/ecas/.env`、以及 ECAS 全部源码。

**影响**

- 邻居栈（第三方镜像、`latest`/旧版本、公开端口）一旦被入侵或容器逃逸，攻击者即获得宿主机控制权，**直接读取 ECAS 全库、`AUTH_TOKEN`、MQTT 口令与备份**，并可篡改采集器写入。
- 本次未发现任一邻居容器的具体漏洞（未做镜像扫描），但「同机 + 高权限 docker」本身就是把 ECAS 的安全等级拉平到该机器上**最弱的一环**。

**建议**

1. 中期：把 ECAS 迁到**独立主机**（或至少独立虚拟机/独立 Docker rootless 实例）。
2. 短期：收紧 `docker` 组成员（只允许运维账号，且用 sudo 白名单代替长期入组）；给邻居容器去掉不必要的 `0.0.0.0` 发布（改绑内网/反代）；为 ECAS 的容器加 `cap_drop: [ALL]`、`User:` 非 root。
3. 给 ECAS 的备份/数据目录设独立挂载与 ACL，实施「同一主机内的最小可见性」。

#### H-03 · IP 入口使用自签证书、HSTS 被注释、链路上存在响应注入

**证据**

- `openssl s_client -connect 124.222.122.146:443` → `subject=C=CN, O=ECAS, CN=124.222.122.146`（**自签**），`notAfter=Oct 28 12:46:00 2027 GMT`；私钥 `ssl/ecas-selfsigned.key` 为 `600 root` ✅（权限本身合格）。
- `web/deploy/cloud/nginx/ecas-edge.conf` 中 HSTS 行被注释：`# add_header Strict-Transport-Security "max-age=15552000" always`（现有安全头仅 `X-Content-Type-Options: nosniff`、`X-Frame-Options: SAMEORIGIN`、`Referrer-Policy: no-referrer`）。
- 从办公网/家宽侧访问该 IP 的 443，**全部返回** `HTTP/1.1 499 Request has been forbidden by antivirus`（`Expires: Mon, 04 Dec 1999 21:29:02 GMT`、`Content-Length: 0`、`Connection: close`）；同一地址从云端本机（`127.0.0.1:443` 与公网 IP 回环）均为 **200**；主机与容器内 grep 无该字符串、nginx 无第三方模块（见 O-01）。

**影响**

- 自签证书 + 无 HSTS ⇒ 用户会习惯性点击「继续访问」，中间人可无告警地替换页面/劫持会话（对 IP 入口而言，这不是理论风险：O-01 已证明链路上存在会改写 HTTP 响应的设备）。
- 相比之下**域名入口 `gateway.xshky.org` 证书有效（GTS WE1）、由 Cloudflare 终止**，风险显著更低。

**建议**

1. 把 IP 入口降级为「只有 301 跳转到 `https://gateway.xshky.org`」，删除对 API 的直接代理；或在接入正式运维网段前直接关闭 443。
2. 若必须保留：启用 HSTS（解开注释）、用 `acme/`（目录已存在，准备就绪）签发受信证书、加 CSP（见 M-04）。
3. 把「IP 直连」写进运维文档的**禁止清单**，避免日后被当作正式入口使用。

### 2.2 中风险

#### M-01 · 公网 IP 入口未授权暴露 API 文档，泄露全部路由（含内部账户接口）

**证据**（从云端主机实测，路径为公网 443 → nginx → mesh → API）

```
/healthz 200 | /docs 200 | /openapi.json 200 | /redoc 200
/api/params 401 | /internal/accounts 401 | /internal/accounts/viewer 401 | /login 404
```

`/openapi.json`（22 047 B）列出：`/api/{alarms,auth,health,params,readings,summary}`、`/api/alarms/{config,scan}`、`/api/export.csv`、`/internal/accounts`、`/internal/accounts/import`、`/internal/accounts/{username}`、`/internal/accounts/{username}/password`、`/internal/accounts/{username}/verify`。

**影响**：攻击者无需认证即可获得完整的接口清单、参数结构与内部账户管理路径，大幅缩短侦察时间；`/docs`、`/redoc` 还提供交互式调用界面（虽然调用仍需令牌）。

**建议**

1. 生产环境关闭文档：`FastAPI(..., docs_url=None, redoc_url=None, openapi_url=None)`（或按 `ECAS_ENV` 开关，仅在开发机开启）。
2. 边缘 nginx 加路径拦截（双重保险）：

```nginx
location ^~ /internal/ { return 404; }
location ~ ^/(docs|redoc|openapi\.json) { return 404; }
```

3. 域名入口已天然安全（未登录 401），保持 Worker 白名单不放行 `/internal/*`、`/docs`（当前实现正确）。

#### M-02 · 单一共享令牌同时用于只读 API 与会话代理的账户管理 API；边缘未屏蔽 `/internal/*`

**证据**

- 后端所有 `/api/*` 与 `/internal/accounts*` 都校验**同一个** `ECAS_AUTH_TOKEN`（`auth.py:50`、`accounts.py:328`）。
- Worker 正是用这个令牌替浏览器调用只读 API（`worker.js:673 headers.set('authorization','Bearer '+env.ECAS_TOKEN)`）。
- 边缘 nginx `location /` 全量转发 ⇒ 从公网 IP 入口 `POST /internal/accounts` **可达**（实测无令牌 401，有令牌即可用）。

**影响**：令牌一旦泄露（日志、误贴、宿主机被读取——见 H-02），攻击者不仅能读数据，还能**创建管理员账户 / 重置任意口令**，形成持久化后门；而该令牌的持有面本应只是「服务端到服务端」。

**建议**

1. 拆成两把令牌：`ECAS_TOKEN`（只读 API，供 Worker）+ `ECAS_INTERNAL_TOKEN`（账户管理，仅供 Worker→VPC，且不写进任何前端可见配置）。
2. 边缘 nginx 屏蔽 `/internal/`（见 M-01 的配置）。
3. 为 `/internal/accounts*` 的**写操作**加独立审计日志（操作者、目标账户、来源、时间），当前仅鉴权失败会记 warning。

#### M-03 · 令牌缺失时 fail-open（一次误配置 = 全站无鉴权）

**证据**

- `web/backend/app/auth.py:50`：`def token_ok(provided, expected): if not expected: return True` ⇒ 环境变量为空即**放行所有请求**。
- `web/deploy/ubuntu/backend/entrypoint.sh`：`/run/secrets/auth-token` 读不到时只打印
  `⚠️ 没有 $AUTH_TOKEN_FILE：ECAS_AUTH_TOKEN 为空 = 不校验令牌，对外不可这样跑` 然后**继续启动**。
- 对照：同一项目的 `/internal/accounts` 走的是 fail-closed（`accounts.py:328` 未配置令牌 → 503），说明团队已有正确范式。

**影响**：一次 secret 挂载失误/环境变量拼写错误，就会让公网 API 与 WebSocket 完全敞开，且**只在日志里留一行警告**。

**建议**

1. `entrypoint.sh`：缺少 `auth-token` 时 `exit 1`（装配失败必须让容器起不来）。
2. `auth.py`：`if not expected: return False`；`settings.auth_required` 为假时在 `/healthz` 与启动日志给出显著告警。

#### M-04 · 全站无 CSP（两处入口也均无 HSTS）

**证据**

- Worker `secHeaders()`（`worker.js:101`）只设 `x-robots-tag / referrer-policy / x-content-type-options / x-frame-options`；实测 `curl -I https://gateway.xshky.org/login` 返回：

```
HTTP/2 200
cache-control: no-store
referrer-policy: no-referrer
x-content-type-options: nosniff
x-frame-options: DENY
x-robots-tag: noindex, nofollow
server: cloudflare
```

无 `content-security-policy`、无 `strict-transport-security`。

**影响**：本次前端审查**未发现 XSS sink**（全仓无 `dangerouslySetInnerHTML/innerHTML/eval`，React 默认转义，Worker 登录页对 `?m=` 做了 `esc()`），因此当前不构成可直接利用的漏洞；但缺少纵深防御——任何未来引入的注入点都无第二道闸，且内联脚本（Worker 注入占位符的 `<script>`）会长期压制 CSP 的严格化。

**建议**

1. Worker/edge 加 CSP，起步建议：
   `default-src 'self'; script-src 'self' 'sha256-<占位符脚本哈希>'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'`
   （把 `injectPlaceholder()` 的内联脚本改为外链或加 hash/nonce。）
2. 在 Cloudflare 侧开启 HSTS，并把 edge 的 HSTS 注释解开。

#### M-05 · 12121 公网暴露 EasyTier Dashboard，而 mesh 正是 ECAS 的实际边界

**证据**

- `curl http://124.222.122.146:12121/` → **200**，`<title>EasyTier Dashboard</title>`；`api_meta.js` 内容为 `window.apiMeta = { api_host: "https://config-server.easytier.cn" }`（指向**第三方公共服务**）。
- 云端容器：`easytier`（`--config-server udp://124.222.122.146:21212/admin`）、`easytier-web`（`-a 12121 -l 12121 -c 21212`）。
- 公网 TCP 探测：11010 **OPEN**；11011 / 21212 / 41425 closed/filtered ✅。
- ECAS 的「内网」即该 mesh：从任一台 mesh 成员（含我的 Mac）都能直连 `10.10.10.11:18080`，并读到 `/docs`、`/openapi.json`。

**影响**：组网管理面暴露在互联网，且管理数据默认指向第三方服务；mesh 内**无微隔离**——加入 mesh 即可达 ECAS 的 API/文档（HTTP 层仍需令牌，但边界已被跨越）。一旦 mesh 加入凭据泄露，攻击面直接等于生产 API。

**建议**

1. 12121 收敛：改绑内网/仅隧道可达，或前置带鉴权的反代；确认 `api_host` 是否必须指向 `config-server.easytier.cn`（若要自持则改指自建配置服务）。
2. 轮换 mesh 网络密钥（`easytier` 启动参数中的 network name/secret，本次未打印），并纳入密钥管理记录。
3. 在 ECAS API 前置一层来源白名单（只允许采集器/Worker 地址），给 mesh 加 ACL 思路。

#### M-06 · 口令哈希参数偏低（PBKDF2 iter=100 000）

**证据**：`web/backend/app/accounts.py:49-52`

```python
ALGO = "pbkdf2-sha256"
ITER = 100_000
SALT_BYTES = 16
HASH_BYTES = 32
```

**影响**：算法与加盐方式正确（每用户独立 16 B salt、`hmac.compare_digest` 校验、明文永不落库/日志），但 100 k 次已低于当前主流建议（OWASP 2023 对 PBKDF2-HMAC-SHA256 建议 ≥ 600 k）。若 `web_account` 表或备份（见 M-08）泄露，口令被离线爆破的成本比预期低。

**建议**：`ITER = 600_000`，并在 `password_ok()` 中支持**渐进升级**（用旧迭代数校验通过后，按新参数重算并回写）。

#### M-07 · 测试栈 sshd 允许 root 登录与口令认证

**证据**（`ubuntu-Super-Server`）

```
PermitRootLogin yes
PasswordAuthentication yes
KbdInteractiveAuthentication no
```

对照腾讯云主机为 `permitrootlogin without-password` + `passwordauthentication no` + `maxauthtries 3` + sshguard（合规）。

**影响**：该主机同时是（H-02 所述的）多容器生产机，且 mesh 成员可达其 22 端口；口令认证 + 允许 root 显著提高了被暴力破解/撞库的后果等级。（`ufw` 处于 active，但无 sudo 无法读取规则集确认放行面。）

**建议**：改为 `PermitRootLogin prohibit-password`、`PasswordAuthentication no`，仅密钥登录；保留一个带 sudo 的普通账号，并装 fail2ban/sshguard。

#### M-08 · 数据库备份为标准 pg_dump，未加密

**证据**：`/opt/ecas/backup/ecas-backup.sh`

```
22: DEST=${DEST:-/data/ecas/backups}
33: docker compose exec -T db pg_dump -Fc -U ecas -d ecas > "$part"
37: docker compose exec -T db pg_restore -l < "$part" > /dev/null
```

备份目录 `/data/ecas` 为 `drwx------ root root`（仅 root 可达 ✅），产物带 sha256 校验并会在备份后做可恢复性检查（工程上很好）。

**影响**：dump 内含**全部浓度读数 + `web_account` 的口令哈希（`web_account.password_hash`）+ 报警配置**，以明文自定义格式落盘；一旦宿主机被入侵（H-02）或误拷贝到别处，等价于拿到离线爆破素材与完整业务数据。

**建议**：备份管道加对称/非对称加密（`age`/`gpg`），或直接写入加密的异地存储；把加密密钥与备份分离存放。

### 2.3 低风险

| 编号 | 问题 | 证据 | 建议 |
| --- | --- | --- | --- |
| L-01 | Worker 上游异常回显内部错误细节；登录失败日志含用户名与口令长度 | `worker.js:699` 502 分支 `detail: String(err.message)`；`:543 console.log('LOGIN-FAIL', JSON.stringify({user, len: password.length, nonascii, ua: slice(0,70)}))`（**不含口令**） | 502 返回泛化文案 + 日志内留 trace id；失败日志去掉口令长度 |
| L-02 | 会话 cookie 未用 `__Host-` 前缀；`/logout` 可被跨站 GET 触发 | `worker.js:208` `dm_session=…; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=…` | 可选改 `__Host-dm_session`；`/logout` 改 POST（无害但更规范） |
| L-03 | DB 容器内 `pg_hba.conf` 使用 `local trust` | `local all all trust`、`host all all 127.0.0.1/32 trust`、`host all all all scram-sha-256` | 容器内也改 scram；至少明确「进容器即全库」的边界假设 |
| L-04 | 容器以 root 运行，`collector`/`db` 未只读根文件系统 | `Config.User` 为空；`ecas-collector` `ReadonlyRootfs=false`、`ecas-db` `ReadonlyRootfs=false`（api 已是 `true` + `no-new-privileges` + 无 CapAdd） | 给 collector/db 设 `user:`；collector 尽量只读根 + tmpfs |
| L-05 | `/api/*` 与 `/internal/*` 无速率限制 | 实-measured：仅 Worker 登录路径有 KV 限速 | 令牌为 43 字符随机，爆破不可行；如需合规，可在 edge 加 `limit_req` |
| L-06 | Worker 登录限速基于 KV（最终一致） | `worker.js:527-547` `rl:<cf-connecting-ip>`，10 次/15 分钟 | 换 Durable Object 或 Cloudflare Rate Limiting 规则，避免多 colo 并发绕过 |
| L-07 | mosquitto `conf.d/ecas.conf` 残留匿名配置（当前未被加载） | `conf.d/ecas.conf:1-2` `listener 1883` + `allow_anonymous true`；`mosquitto.conf` 中**无 `include_dir`**（grep 无 include） | 删除该文件（惰性风险：日后一旦有人加 `include_dir` 即匿名开放） |
| L-08 | BT-Panel 监听 `*:8888`（另一项目） | 云端 `ss -ltn` 显示 `*:8888`（pid 1744），仅靠安全组遮挡 | 改绑内网；面板开 MFA、更新版本 |
| L-09 | 密钥卷自身非加密卷 | `diskutil info /Volumes/keys` → `Encrypted: No`、`FileVault: No`（主机 `fdesetup status` = FileVault is On；卷内文件均 `0600`、子目录 `0700` ✅） | 确认该卷是否位于已加密 APFS 容器内；否则改用加密 DMG/加密分区存放密钥 |
| L-10 | 服务器上存在 3 份字节相同的 `1ms-helper` 副本；`watchtower` 处于崩溃重启循环 | `docs/485仪表接入SOP.md:1669-1670`（`sha256 34c70962ee17c707…`；日志 `client version 1.25 is too old…`） | 属运维噪声：删除 `watchtower` 段、收敛 `1ms-helper` 为单一副本（减少被篡改面） |

### 2.4 观察与提示

- **O-01（网络层干预）**：从办公网/家宽访问 `124.222.122.146:443` 会收到 `HTTP/1.1 499 Request has been forbidden by antivirus`（`Expires: Mon, 04 Dec 1999 21:29:02 GMT`、`Content-Length: 0`）；TLS 对端确为源站自签证书（指纹 `DE:13:1B:59:71:FE:24:99:3E:BF:2E:00:5D:79:8C:B6:32:60:78:08:62:07:B3:DE:02:AB:AD:2D:5B:11:58:AF`），云端本机访问同一地址为 200，源站与容器内均无该字符串、nginx 无第三方模块 ⇒ **该响应由链路中间设备注入，不是 ECAS 的问题**。它同时说明：IP 型 HTTPS 入口在某些网络下**根本不可用**，进一步支持 H-03 的收敛建议。
- **O-02**：`gw` 路径上的「令牌不进浏览器」设计是本次审查中最值得保留的架构资产——请勿在后续迭代中为图方便让前端直接持有 `ECAS_TOKEN`。
- **O-03**：本次未对 `web/backend/app/alarms.py`、`schemas.py` 的工作树版本审查（此二文件当时含另一会话未提交的改动），报告结论以其 `HEAD` 版本为准。

---

## 3. 已确认的良好实践（实证）

1. **凭据最小暴露**：`/run/secrets/{auth-token,pg-password,mqtt-pass}` 均为 `0600 root`；`/opt/ecas/credentials` 为 `0700 root`；api 与 collector 的 secret 挂载**互不越权**（api 无 mqtt-pass，collector 无 auth-token）。
2. **容器加固**：`ecas-api` `read_only: true` + `tmpfs: /tmp:mode=1777,size=16m` + `no-new-privileges:true` + 无额外 capability；端口绑定写作 `"${BIND_IP:?…}:${ECAS_WEB_PORT:-18080}:8000"`，**变量缺失即启动失败**（避免误绑 `0.0.0.0`）。
3. **数据库不暴露**：主机无 5432 监听、compose 无 ports、容器 `PortBindings: {}`、Mac 侧 `ConnectionRefused`；DSN 不含口令（走 `PGPASSFILE`，`/tmp/pgpass` 0600）。
4. **内部接口 fail-closed**：`/internal/accounts*` 无令牌 401、未配置令牌 503（`accounts.py:328`），`hmac.compare_digest` 比较，全部 8 个端点统一 `GUARD`。
5. **口令学**：PBKDF2-HMAC-SHA256 + 每用户 16 B salt + 常量时间比较；明文口令永不落库、永不返回、校验只在 `/internal/accounts/{u}/verify` 一跳内出现（`accounts.py:8-12`）。
6. **MQTT 最小权限**：`allow_anonymous false`；ACL 中 DTU 只写、采集器只读、运维账号读写；`passwd` 640。匿名 CONNECT 实测：公网 8883 `rc=5`、本机 1883/8883 `rc=5`、8884 TLS `rc=5` ✅。
7. **内部一跳有完整性**：collector 通过 `8884` TLS 连接并用 `MQTT_CERT_SHA256` **证书指纹钉扎**；ca.crt 以 `:ro` 挂入。
8. **Worker 会话设计**：HMAC-SHA256 签名 cookie（`HttpOnly; Secure; SameSite=Lax; Max-Age=7d`）+ 每请求回查数据库 + `se` 版本比对（改密/停用即时失效）+ 手写常量时间比较（`eqConst`）；登录限速 10 次/15 分钟；`?m=` 经 `esc()` 无反射 XSS；路径预检拒绝 `..`、`\`、控制字符；转发白名单不含 `/internal/*` 与 `/docs`（实测域名入口这些路径均 401）。
9. **仓库与配置卫生**：git 历史无 `.env`/私钥（仅 `.env.example`），`.gitignore:98` 覆盖 `web/backend/.env`；Worker `vars` 为空（密钥走 Cloudflare secret）；`/opt/ecas-edge/.env` 仅 `EDGE_IMAGE`；`/opt/ecas/.env`（644）只含非敏感参数（`PG_USER/PG_NAME/MODEL_NAME/OFFSET_*/MQTT_HOST/PORT/TOPIC/USER/BIND_IP/ECAS_WEB_PORT/PIP_INDEX/APT_MIRROR/CREDENTIALS_DIRECTORY`）——**此前怀疑的第 22 行实为注释「compose 从这里读 container secret 的源文件」，该文件不含口令**（更正）。
10. **云主机 SSH**：禁止口令登录、root 仅证书、`maxauthtries 3`、sshguard 2.4.2 生效；安全组仅放行 22/80/443/8883/11010/12121（11011、21212、41425 TCP 实测关闭）；2026-09-28 起 `8883` 的**来源**已从 `0.0.0.0/0` 收成现场运营商段（与主机侧 nftables 白名单同口径，实测验证）。
11. **备份工程**：`pg_dump -Fc` + `pg_restore -l` 可恢复性校验 + sha256 清单 + 目录 `0700 root`（不足在未加密，见 M-08）。
12. **前端无 XSS sink**：全仓无 `dangerouslySetInnerHTML/innerHTML/eval`；仅把占位符写入 `localStorage`；React 默认转义。

---

## 4. 加固路线（按优先级）

### 立即（本周）

1. **H-01** ✅（2026-09-28 已做）：明文口从持有数据的 broker 摘出，交给 `ecas-mqtt-relay`（腾讯云机，只转发不落盘）；主 broker 不再监听 8883；桥接走回环 + 专用最小权限账号 `ecas-bridge`。
   —— 先生 2026-09-28 裁定：明文无法在 DTU 侧解决 ⇒ 改为「单独 1 台服务器做数据转发，不直接触碰原始数据」（转发机＝腾讯云机，其上已有 easytier 服务）。
   —— **已追加**：nftables 来源白名单（8883 只放行 `127.0.0.1` + 现场段 `180.108.106.0/24`；`fw status` 可查 drop 计数）。
   —— **未消除**：明文公网口仍在（DTU 必需）⇒ 仍需轮换 `dpro66-dtu` 口令、采集侧加序号做重放检测、DTU 侧支持 TLS 后撤掉明文口（根治）。
2. **H-03** ✅（2026-09-28 已做）：IP 入口改为「仅 301 跳域名」；HSTS 由正式域名侧（Worker）下发，自签侧不开。
3. **M-01 + M-02** ✅（2026-09-28 已做）：关闭生产文档；内部令牌与对外令牌分离；edge 不再有任何反代路径（`/internal/`·`/docs`·`/redoc`·`/openapi.json` 全部 301 走人）。
4. **M-03** ✅（2026-09-28 已做）：fail-closed（entrypoint 缺 secret 即 `exit 1`；`token_ok` 未配置返回 False）。

### 短期（两周）

5. **H-02**：把 ECAS 从共享生产机迁出（或至少收紧 docker 组 + 去掉邻居容器不必要的公网发布 + ECAS 容器非 root/`cap_drop: ALL`）。
6. **M-05**：收敛 12121，确认/替换第三方 `api_host`，轮换 mesh 密钥并记录。
7. **M-04**：两处入口加 CSP（含内联脚本 hash/nonce）与 HSTS。
8. **M-07**：测试机 sshd 改密钥专用 + 禁 root。
9. **M-08**：备份加密（age/gpg）并异地存放。

### 中期（一个月内）

10. **M-06**：PBKDF2 迭代提到 600 k + 渐进升级。
11. **L-* 逐条**：容器非 root、`pg_hba` 收紧、KV 限速换 Durable Object/Rate Limiting、清理 `conf.d/ecas.conf`、BT-Panel 与密钥卷加固。
12. **常态化**：给镜像加自动漏洞扫描（Trivy/Grype）、给 ECAS 加最小可用的安全回归测试（登录限速、路径穿越、`/internal` 拒绝），并把本报告 §2 的复现命令固化为检查脚本。

---

## 5. 审查限制与未覆盖项

| 项 | 说明 |
| --- | --- |
| 无 root/sudo | 云端 iptables/ufw 完整规则集、`/opt/ecas/credentials` 的文件权限（只能从容器内看到 `0600`）、systemd 单元的实际生效状态、BT-Panel 自身配置均**未验证** |
| 未做主动扫描/渗透 | 未运行 nmap/nuclei/sqlmap 等工具，未尝试口令爆破与漏洞利用（有意避免对生产的影响） |
| 未覆盖 | 塔石 DTU 固件与配置、EasyTier 服务端自身漏洞、Cloudflare 账户权限与 API Token 范围、第三方 DNS/邮件（postfix 仅 127.0.0.1） |
| 未覆盖 | 流量侧取证（未抓包复核 8883 明文与 mesh 内部通信） |
| 版本状态 | 未对镜像做 CVE 比对（`nginx:1.27-alpine`、`postgres:16-alpine`、`easytier:latest` 等） |
| 口径 | 代码审查以 `HEAD` 为准；`alarms.py`、`schemas.py` 当时含未提交 WIP，未审其工作树版本 |

---

## 6. 附录：关键复现命令

```bash
# 未授权面（经公网 443 → nginx → mesh → API）
ssh ecas 'for p in /healthz /docs /openapi.json /redoc /api/params /internal/accounts; do \
  printf "%-26s %s\n" "$p" "$(curl -sk -m 8 -o /dev/null -w "%{http_code}" https://124.222.122.146$p)"; done'

# 域名入口（对照：应全部 401）
for p in / /admin /api/params /internal/accounts /docs /openapi.json; do
  printf '%-22s → %s\n' "$p" "$(curl -sS -m 12 -o /dev/null -w '%{http_code}' https://gateway.xshky.org$p)"; done

# IP 入口证书
echo | openssl s_client -connect 124.222.122.146:443 2>/dev/null | openssl x509 -noout -subject -dates

# 匿名 MQTT（只 CONNECT，不发布）
python3 - <<'PY'
import socket
s=socket.create_connection(("124.222.122.146",8883),timeout=7)
s.sendall(b'\x10\x1a\x00\x04MQTT\x04\x02\x00\x3c\x00\x08probe-an')
print("CONNACK rc =", s.recv(4)[3], "(5 = 未授权，符合预期)")
PY

# 数据库端口（应 ConnectionRefused）
python3 -c "import socket;s=socket.socket();s.settimeout(5);\
print(s.connect_ex(('10.10.10.11',5432)))"

# 机密文件权限（容器内视角）
ssh ecas 'docker exec ecas-api ls -ln /run/secrets/'         # 期望 -rw------- 0 0
ssh ecas 'docker exec ecas-collector ls -ln /run/secrets/'

# 容器加固参数
ssh ecas 'docker inspect ecas-api --format "{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.SecurityOpt}} {{.HostConfig.PortBindings}}"'
```

---

## 7. 附录 B：腾讯云安全组规则清单与复核（2026-09-28 14:48 先生导出）

> 权威来源：先生从控制台导出的 `firewallRules_2026-09-28 14_48_16.csv`（27 条规则，列 `CidrBlock,Protocol,Port,Action,FirewallRuleDescription`）。
> 该安全组为**未放行即拒绝**：实测从公网（Mac，出口 `218.88.248.132`）连 `8883`/`8884`/`8888`/`1883` 全部**超时**，而 `22`/`80`/`443`/`11010`/`12121` 可连。

| 端口 | 协议 | 来源 | 备注 | 复核结论 |
| --- | --- | --- | --- | --- |
| 22 | TCP | 全部 IPv4 | — | 保留；云机 sshd 已硬化（禁口令、root 仅证书） |
| 80 | TCP | 全部 IPv4 | — | 保留（ACME + 301 跳转） |
| 443 | TCP | 全部 IPv4 | — | 保留（自签阶段仍需；证书就绪后换正式证书） |
| 8883 | TCP | `180.108.106.0/24` | `DTU STL` | ✅ 已收窄到 DTU 运营商段（H-01 处置，配合主机 nftables 白名单双层） |
| 1883 | TCP | （IPv4 允许规则**已删**，仅剩 `::/0` 拒绝） | `MQTT` | ✅ 此口只监听 `127.0.0.1`，删得对 |
| 8888 | TCP | 全部 IPv4 **拒绝** | 宝塔 | ✅ 维持拒绝（如 §2.3 L-08） |
| 11010 | TCP+UDP | 全部 IPv4 允许 | — | 按先生裁定保留（EasyTier 数据面核心口） |
| 11011 | TCP+UDP | 全部 IPv4 允许 | — | ⚠️ 当前**无 TCP 监听**（UDP 有）⇒ 规则仅在有需要时保留 |
| 11012 | TCP+UDP | 全部 IPv4 允许 | — | ⚠️ **无监听**，属冗余暴露面，建议删除 |
| 21212 | TCP+UDP | 全部 IPv4 允许 | — | UDP 必须保留（设备取配置）；**TCP 无监听**，可删 TCP 行 |
| 12121 | TCP(+UDP) | ~~全部 IPv4 允许~~ **已删** | — | ✅ 已于 2026-09-28 处置（见 7.2）：放行规则删除，改走 SSH 隧道 |
| ICMP | ALL | 全部 IPv4 拒绝 | — | 影响 `ping` 排障：**超时不等于端口被挡**，须用 TCP 探针区分 |

### 7.1 附录 B 发现：12121 = EasyTier 控制面，公网可达且注册未禁用

**证据**

- 云机容器 `easytier-web` 启动参数 `easytier-web -a 12121 -l 12121 -c 21212`（`-a` = REST API 口，`-c` = 设备 config server 口），`--api-server-addr` 未指定 ⇒ 默认 `0.0.0.0`，实测 `0.0.0.0:12121` 监听。
- 公网 `GET http://124.222.122.146:12121/` ⇒ **200**，页面标题 `EasyTier Dashboard`；前端包内出现 `/api/v1/auth/login`、`/auth/register`、`/auth/captcha`、`/auth/password`、`/machines`、`/networks` 等路径。
- 只读探针：`/api/v1/auth/check_login_status` ⇒ **401**、`/api/v1/summary` ⇒ **401**（数据接口确有会话校验），`/api/v1/auth/oidc/config` ⇒ `{"enabled":false}`，`/api/v1/auth/captcha` ⇒ 200（PNG）。
- `easytier-web --help` 存在 `--disable-registration` 开关，而容器启动参数**未传**该开关 ⇒ **注册入口默认开放**（有图形验证码，但不构成准入控制）。

**影响**

- 这是同机**另一项目**（EasyTier 叠加网）的控制面：任何人可访问 Dashboard、走注册流程尝试建立账号；账号一旦建立即可管理该实例上的网络配置。当前未见数据接口匿名可读（401 生效），故定性为**中风险暴露面**（非「已失陷」）。
- `21212/UDP` 是设备取配置的必需口（设计如此），不在本次整改范围。

**建议**（按性价比排序，均未执行——待先生裁定）

1. **推荐**：安全组删掉 `12121` 放行规则，改走隧道 `ssh -L 12121:127.0.0.1:12121 ecas`（与 8888 宝塔同一处理思路）；或
2. 保留公网但加 `--disable-registration`（可通过环境变量/命令行），并确保管理员口令强、关注登录失败日志；
3. 顺带清理：`11012` 与 `21212` 的 **TCP** 行（当前无监听），确认 `11011` 用途后决定去留。

### 7.2 执行进展（2026-09-28 14:56 先生二次导出 + 公网复测）

先生按建议 (a) 在控制台自行修改，14:56 的 `firewallRules_2026-09-28 14_56_15.csv`（10 条规则，全部为**放行**行）显示：

| 项 | 14:48 → 14:56 | 复核证据（Mac，出口 `218.88.248.132`） |
| --- | --- | --- |
| `12121` 放行 | 已删除 ✅ | TCP 探针：由「✅ 可连」→「⛔ 超时」 |
| `21212` TCP | 已删除 ✅（保留 UDP，设备取配置必需） | TCP 探针：⛔ 超时；云机 `ss` 确认无双栈 TCP 监听 |
| 各**显式拒绝**行（`1883`/`8888`/`ICMP`/各 `::/0`） | 已全部移除 ✅（**无害**） | 安全组为「未放行即拒绝」：实测 `8888`、`1883`、`8884` **仍超时** ⇒ 删拒绝行未新增任何暴露面 |
| `11010` TCP+UDP、`11011` TCP+UDP、`11012` TCP+UDP、`80`/`443`/`22`、`8883` 收窄段 | 保留 | `22`/`80`/`443`/`11010` 可连（设计内）；`11011`/`11012`/`21212` TCP 为 ⊘「拒绝连接」（**无监听**，非被安全组丢弃） |

**残留处置决定（先生 2026-09-28 15:0x 裁定：删）**：`11011` 的 **TCP** 行（云机仅 UDP `11011` 有监听：`easytier-core`）与 `11012` 的 **TCP+UDP** 两行（云机当前**无任何 11012 监听**；EasyTier 官方将该口留给 WSS/QUIC，将来启用时再把规则加回）。三行皆属「放行但无监听」的空门——删除不改变当前行为，保留则在将来有进程绑定该口时**立即对全网开放**。执行方式：由先生在控制台手工删除（不向本机交付腾讯云密钥）。删完后从公网复测这三口应为「拒绝连接/超时」，并确认 `21212/UDP`、`11011/UDP` 的 mesh 数据面不受影响。

自动化工具已就绪：`web/deploy/cloud/tighten-security-group.py` 的 `drop`/`trim` 子命令（离线用 27 条合成规则验证：命中即删且不误删，`UDP 21212`/`UDP 11011`/`UDP 11010`/`TCP 8883`/拒绝类规则一律保留），待先生提供仅含 VPC 读写权限的临时密钥后即可执行。

---

*报告完 · 2026-09-28 · 附录 B 增补于同日（安全组规则复核）· 7.2 执行进展据先生 14:56 二次导出复核*

