---
tags: [easytier, 组网, ubuntu, 运维, 服务器]
created: 2026-09-26
来源: 先生《easytier 部署》经验笔记（GUI / CLI / config 模板）+ 2026-09-26 腾讯云服务器实测巡检
---

# EasyTier 在 Ubuntu 服务器上的部署与使用

> [!info] 本文定位
> 把「笔记里那份配置模板 + 三种部署方式」落到 **Ubuntu 服务器（22.04 / 24.04）** 上，并补上服务器场景独有的部分：**端口与云安全组、systemd 托管、Web 控制台的安全暴露面、升级与数据备份**。
> 实测部分来自本项目那台腾讯云服务器（OpenCloudOS 9.4），差异单列在 §8；巡检全过程见 [[easytier巡检-2026-09-26]]。

---

## 一、动手前先定三件事

| 决策 | 选项 | 建议 |
|---|---|---|
| **这台机器是什么角色** | ① 客户端（只接入）② 服务端 / 中转（有公网 IP，给别人当 peer / 配置服务） | 公网机器才需要放行端口、才需要考虑 Web 控制台暴露 |
| **怎么装** | ① 二进制 + systemd ② Docker Compose ③ GUI（仅桌面） | 服务器优先 ①（最轻、开机自启最干净）；已经在用 Docker 的（如本项目那台）用 ② |
| **网络三要素** | `network_name` / `network_secret` / 虚拟网段（如 `10.10.10.0/24`） | 三者**全网必须一致**；`network_secret` 等同入网凭据，别进 git、别贴聊天记录 |

> [!warning] 隐私值一律放密钥卷（2026-09-26 先生定的规则）
> `network_secret` 以及任何口令 / 令牌 / 私钥的**真实值只存在密钥卷** `/Volumes/keys/easytier/easytier.toml`（Mac 侧完整实盘配置存档：`/Volumes/keys/dsh/easytier-mac-config.toml`）。
> 本文档、仓库、记忆、日志、聊天记录里**只写指向该路径的路径**，示例一律用 `$network_secret` 这类占位符。
> 实盘配置保留原件不动（密钥卷**登录后才挂载**，把实盘改成软链会导致开机起不来）——详见卷根 README「使用约定」。

> [!tip] 一句话原理
> EasyTier 让每台机器多出一块虚拟网卡（`tun`），节点之间靠 11010 打洞直连、打不通就走中继；`network_name + network_secret` 决定「谁能进这个网」。

---

## 二、配置文件（先懂它，后面所有部署方式都用同一份）

### 2.1 先生笔记里的模板（原文保留）

```toml
# 宿主机名称
instance_name = "xsh"
# 宿主机 ID，程序生成
instance_id = "a53c334e-c957-4b2b-ae66-4184a2bd6dc1"

# 宿主机虚拟 IPv4 地址，亦可以使用DHCP，但不建议。（用于创建虚拟局域网）
ipv4 = "10.10.10.22/24"
# 关闭 DHCP，使用静态 IP
dhcp = false

# 监听端口（程序默认）
listeners = [
    "tcp://0.0.0.0:11010",
    "udp://0.0.0.0:11010",
    "wg://0.0.0.0:11011",
]

# 虚拟局域网名称和密码，用于明确访问权限
[network_identity]
network_name = "$network_name"
network_secret = "$network_secret"

# 初始节点，这里确定服务端外网 IP
[[peer]]
uri = "$tcp_uri"

[[peer]]
uri = "$udp_uri"

# 高级设置选项
[flags]
# 启用魔法 DNS
accept_dns = true
# 启用 KCP 代理，通过将 TCP 流量转为 KCP 流量，降低时延
enable_kcp_proxy = true
# 启用延迟优先模式，忽略中转跳数，选择总延迟最低的路径
latency_first = true
# 启用私有模式，不允许不属于本网络名称和密码的节点通过本节点握手或中转
private_mode = true
```

### 2.2 每个字段是什么（服务器视角）

| 字段 | 作用 | 服务器上的建议 |
|---|---|---|
| `instance_name` | 这台机在 mesh 里的显示名 | 起个能认出机器的名字（`tencent-vm`、`aliyun-hk`） |
| `instance_id` | 程序自动生成的机器 ID | **不用手改**；一个配置文件复制到多台机器时要删掉让程序重生成 |
| `ipv4` | 虚拟网卡地址（静态） | 静态分配，例如 `10.10.10.123/24`；和别人的网段别撞（别用 `192.168.1.0/24` 这类常见私网） |
| `dhcp` | 是否让 mesh 分配地址 | 服务器上 **`false`**（笔记的结论一致），地址固定才好写文档、才好做白名单 |
| `listeners` | 本机在哪些口上收 peer | 默认三行就够；`wg://` 是给 WireGuard 门户用的，不用可去掉 |
| `[network_identity]` | 网络名 + 密码 | **最关键的机密**；`network_secret` 泄露 = 别人能进你的内网 |
| `[[peer]]` | 初始节点（引导地址） | 填**有公网 IP 的那台**的 `tcp://IP:11010` 与 `udp://IP:11010`；多填几个做冗余 |
| `[flags]` | 高级开关 | 见下 |

**`[flags]` 四个开关**：`accept_dns`（魔法 DNS，能用主机名互访，建议开）｜ `enable_kcp_proxy`（TCP 转 KCP 降延迟，国内链路建议开）｜ `latency_first`（按总延迟选路，建议开）｜ `private_mode`（禁止非本网节点经我中转，**服务器上建议开**）。

### 2.3 两个省事技巧

- **不会写就用配置助手**：<https://easytier.cn/assistant/>，选完生成 TOML 然后 `-c` 加载。
- **反查配置文件**：带着参数运行一次 `easytier-core`，程序会把等价配置**打印在终端**（不带参数运行则打印最小配置）⇒ 想核对「某个 CLI 参数对应哪一行配置」就用这招。配置文件参数**可以被命令行覆盖**（命令行优先）。
- 一个进程可以加载多个网络：`-c` 传多份配置文件。

---

## 三、方式 A：二进制 + systemd（Ubuntu 服务器推荐）

### 3.1 官方脚本替你做了什么（`script/install.sh`，main 分支）

> [!note] 上游事实（2026-09-26 读脚本确认）
> 脚本按 init 系统自动选 **systemd / OpenRC**，干这些事：
> 1. 把发行包解压到 `$INSTALL_PATH`（脚本变量；用 `systemctl cat easytier@default | grep WorkingDirectory` 可查实际路径），`chmod +x easytier-core easytier-cli`；
> 2. 建 `$INSTALL_PATH/config/`，先写一份 `config/default.conf`；
> 3. 建 **模板服务** `/etc/systemd/system/easytier@.service`，`systemctl enable --now easytier@default`；
> 4. 软链 `/usr/sbin/easytier-core`、`/usr/sbin/easytier-cli`；
> 5. 脚本内还带 `UPDATE()`（先备配置再换二进制，配置不会被覆盖）与 `UNINSTALL()`（disable + 删目录与 unit、清理旧版残留）。

它写的默认 `config/default.conf` 是（原文）：

```toml
instance_name = "default"
dhcp = true
listeners = [
    "tcp://0.0.0.0:11010",
    "udp://0.0.0.0:11010",
    "wg://0.0.0.0:11011",
    "ws://0.0.0.0:11011/",
    "wss://0.0.0.0:11012/",
]
exit_nodes = []
rpc_portal = "0.0.0.0:0"
```

模板 unit 的关键几行（**节选**，`$INSTALL_PATH` 为脚本变量；另有 `[Install]` 段供 `enable`）：

```ini
[Unit]
Description=EasyTier Service
Wants=network.target
After=network.target network.service
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$INSTALL_PATH
ExecStart=$INSTALL_PATH/easytier-core -c $INSTALL_PATH/config/%i.conf
Restart=always
RestartSec=1s
```

> [!important] 模板服务的用法是「一网一实例」
> `%i` 对应 `config/<名字>.conf`：`systemctl start easytier@default` 用的是 `config/default.conf`；
> 想让这台机同时进两个网，就再写 `config/home.conf`，然后 `systemctl enable --now easytier@home`。

### 3.2 手工等价步骤（不想跑脚本 / 想完全掌控时）

```bash
# 1) 依赖：tun 设备与转发
ls -l /dev/net/tun                      # 必须存在；Ubuntu 默认有
sudo sysctl -w net.ipv4.ip_forward=1    # 这台要做中转/子网代理时开

# 2) 下载发行包（GitHub Releases，选 easytier-linux-x86_64-*.zip）
#    解压到 /opt/easytier
sudo mkdir -p /opt/easytier/{bin,config}
sudo unzip -o ~/easytier-linux-x86_64-*.zip -d /tmp/et
sudo mv /tmp/et/easytier-linux-x86_64/* /opt/easytier/bin/
sudo chmod +x /opt/easytier/bin/easytier-core /opt/easytier/bin/easytier-cli
sudo ln -sf /opt/easytier/bin/easytier-core /usr/sbin/easytier-core
sudo ln -sf /opt/easytier/bin/easytier-cli  /usr/sbin/easytier-cli

# 3) 写配置（用 §2 那份模板）
sudo tee /opt/easytier/config/default.conf >/dev/null <<'EOF'
instance_name = "ubuntu-srv"
ipv4 = "10.10.10.123/24"
dhcp = false
listeners = ["tcp://0.0.0.0:11010", "udp://0.0.0.0:11010", "wg://0.0.0.0:11011"]
[network_identity]
network_name = "你的网络名"
network_secret = "你的网络密码"
[[peer]]
uri = "tcp://<对端公网IP>:11010"
[[peer]]
uri = "udp://<对端公网IP>:11010"
[flags]
accept_dns = true
enable_kcp_proxy = true
latency_first = true
private_mode = true
EOF
sudo chmod 600 /opt/easytier/config/default.conf     # 里面有 network_secret

# 4) 建 unit（与官方同名，便于以后换官方脚本）
sudo tee /etc/systemd/system/easytier@.service >/dev/null <<'EOF'
[Unit]
Description=EasyTier Service
Wants=network.target
After=network.target network.service
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=/opt/easytier
ExecStart=/opt/easytier/bin/easytier-core -c /opt/easytier/config/%i.conf
Restart=always
RestartSec=1s

[Install]
WantedBy=multi-user.target
EOF

# 5) 起服务 + 开机自启
sudo systemctl daemon-reload
sudo systemctl enable --now easytier@default

# 6) 验证（见 §6）
systemctl status easytier@default --no-pager
ip -br addr show | grep -i -E 'tun|easytier'
easytier-cli peer
```

> [!warning] 权限：TUN 需要能力，官方 unit 是 root 跑的
> 建虚拟网卡需要 `CAP_NET_ADMIN`。官方 unit **没有**写 `User=`，所以是 root 跑（简单但非最小权限）。
> 想降权：加 `User=easytier` + `AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW` + `CapabilityBoundingSet=...`，并确认 `/dev/net/tun` 对它有权限（这属于加固，先跑通再收）。

---

## 四、方式 B：Docker Compose（本项目服务器正在用的形态）

```yaml
services:
  easytier:
    image: easytier/easytier:v2.6.4          # ⚠️ 固定 tag，别用 latest（否则升级是隐式的）
    container_name: easytier
    hostname: ubuntu-vm
    network_mode: host                        # 必须：要在宿主网络上打洞/监听
    cap_add: [NET_ADMIN, NET_RAW]
    devices: ["/dev/net/tun:/dev/net/tun"]
    volumes:
      - /etc/easytier:/root                   # 配置与数据落宿主，重建容器不丢
      - /etc/machine-id:/etc/machine-id:ro    # 让 instance_id 稳定（可选）
    command: --config-server udp://<公网IP>:21212/admin
    entrypoint: easytier-core
    restart: unless-stopped

  easytier-web:                               # 需要 Web 控制台 / 托管配置时才装
    image: easytier/easytier:v2.6.4
    container_name: easytier-web
    network_mode: host
    entrypoint: easytier-web-embed
    command: -a 12121 -l 12121 -c 21212
    volumes:
      - /opt/et-web-db:/app/db                # ⚠️ 必须挂！不挂 = 账号与托管配置随容器一起没
    restart: unless-stopped
```

要点：

- `network_mode: host` 不能省，否则打洞与固定端口监听都会失效。
- `--config-server` 的地址**必须写完整 URL**（`udp://IP:21212/admin`）——上游 2.7.0 起已删除「裸 token」兜底写法。
- `easytier-web` 的 `/app/et.db` 若**没有挂载**，`docker compose up -d` 重建容器时账号与托管配置全部丢失（本项目那台就是这个状态，靠 §9 的备份法兜底）。
- 固定 tag（`v2.6.4`）后，升级变成显式动作；用 `watchtower` 之类自动升级务必谨慎（见 §9）。

常用 Docker 运维：

```bash
docker compose -f /opt/easytier/docker-compose.yml config -q   # 语法自检
docker compose up -d                                            # 应用（会瞬断组网几秒）
docker logs -f easytier
docker exec easytier easytier-cli peer
```

---

## 五、端口与防火墙（Ubuntu 的 `ufw` + 云安全组）

| 端口 / 协议 | 进程 | 作用 | 公网是否必须 |
|---|---|---|---|
| `11010` **TCP + UDP** | `easytier-core` | P2P 主口（打洞/数据面） | **必须** |
| `11011` UDP | `easytier-core` | WireGuard 门户 / 备用 | 要用 WG 门户才必须 |
| `15888` TCP | `easytier-core` | CLI 的 RPC 口 | 一般**不需要**公网 |
| `41425` TCP 等 | `easytier-core` | 内核/NAT 辅助（随机端口） | 不需要 |
| `21212` **UDP** | `easytier-web-em` | 配置下发（节点用 `--config-server` 取配置） | **仅当这台做配置服务** |
| `12121` **TCP** | `easytier-web-em` | **Web 控制台 + REST API（人机界面）** | ⛔ **不要公网**，走 SSH 隧道 |

```bash
# Ubuntu ufw：只放组网必需的口
sudo ufw allow 11010/tcp && sudo ufw allow 11010/udp
sudo ufw allow 11011/udp            # 用 WG 门户才需要
sudo ufw allow 21212/udp            # 只有这台做 config-server 才需要
sudo ufw enable && sudo ufw status verbose
```

> [!danger] 云服务器上的「真正的门」是**云安全组**，不是 ufw
> 腾讯云/阿里云/AWS 的入站规则在 ufw 之前生效：**ufw 放行了、安全组没放 = 外面进不来**；反之安全组放行了、ufw 拦着 = 也进不来。两边都要对。
> ⚠️ 控制台里 `11010 / 12121 / 21212` 很可能**挤在同一行规则**里 —— 想关 12121 只删那一项，**别整行删**（会连带断掉配置下发与打洞）。

### 5.1 Web 控制台只走隧道（推荐姿势）

```bash
# 本地 → 服务器，把 12121 映射到本机回环
ssh -N -L 12121:127.0.0.1:12121 user@<服务器IP>
# 浏览器开 http://127.0.0.1:12121/
# ⚠️ 登录页的 API Host 必须填 127.0.0.1:12121，否则前端仍去连公网 IP，隧道路由白配
```

---

## 六、验证与日常运维

```bash
ip -br addr show | grep -iE 'tun|easytier'   # 虚拟网卡是否建起来、地址对不对
easytier-cli peer                            # 对端清单：cost=Local 是本机，其它应为 p2p 或 relay
easytier-cli route                           # 路由/网关
easytier-cli node                            # 本机信息
ping -c2 10.10.10.21                         # 用虚拟 IP 互相验证
systemctl status easytier@default --no-pager # 二进制方式的日志与状态
journalctl -u easytier@default -f            # 跟日志
ss -lntup | grep -i easytier                 # 核对实际监听端口
```

**自检表（现象 → 判据 → 处置）**

| 现象 | 判据 / 原因 | 处置 |
|---|---|---|
| `ip addr` 里没有 tun | `/dev/net/tun` 不存在或没有 `CAP_NET_ADMIN` | 补设备/能力；Docker 记得 `--device /dev/net/tun` |
| 一直 `relay` 不 `p2p` | 打洞失败（对称 NAT / 防火墙挡 UDP） | 放行 `11010/udp`；让一端做「有公网 IP 的中转」 |
| `peer` 里看不到对端 | `network_name`/`network_secret` 不一致，或 peer 地址写错 | 三要素全网比对；`ping` 不通先看监听与安全组 |
| 虚拟 IP ping 不通但 peer 在 | 路由/网段冲突（两端 `ipv4` 网段不一致或与真实私网撞车） | 统一网段，避开 `192.168.1.0/24` 这类常见段 |
| Web 控制台打不开 | 安全组没放 12121，或**已按建议关掉** | 用 §5.1 隧道访问（这才是常态） |
| 容器重建后账号全没了 | `easytier-web` 的 `/app/et.db` 没挂卷 | 见 §9 备份法；最好直接补上卷挂载 |
| 升级后 CLI 参数报错 | 2.7.0 起 `-w/--config-server` 必须完整 URL | 把 `udp://IP:21212/admin` 写全 |

---

## 七、安全：装完必须做的四件事

1. **改掉 Web 控制台默认口令**。上游 `easytier-web/migrations/20241026_init.sql` 里硬编码了 `admin/admin`、`user/user`，且**修复 PR 未合并**（默认弱口令不会被任何一次升级解决）。改口令 + 确认「注册」入口是否需要邀请码。
2. **`12121` 绝不放公网**，管理一律走 SSH 隧道。原因：控制台一旦可达 + 旧版本已知越权漏洞（v2.6.4 未修的 `GET /api/v1/sessions`，任意已登录用户可读**所有**会话的 token 与 `client_url`）⇒ 拿到一个登录态就可能读到 `user_running_network_configs` 里的 **`network_secret`**，等同入网凭据。
3. **`network_secret` 当密码管**：配置文件 `chmod 600`；不要进 git、不要贴聊天记录；泄露了就**换密码**（全网节点都要改）。
4. **`private_mode = true`**：不让不属于本网的节点穿过你这台中转。

> [!note] 值得升级的两个理由（详见 [[easytier巡检-2026-09-26]] §四）
> ① `#2445` 会话越权读取；② `#2570` ACL 绕过（双向 ping 命中临时放行、绕过 inbound default-drop）。此外主干已 bump 到 **2.7.0**（111 commits / 113 PR，含 QUIC 断流、冻结死锁等多处修复），**下一版一发布就值得升**。

---

## 八、和本项目那台服务器的差异（OpenCloudOS 9.4，2026-09-26 快照）

| 维度 | 本项目服务器（实际） | 纯 Ubuntu 服务器（本文） |
|---|---|---|
| 发行版 | **OpenCloudOS 9.4**（RHEL 系，无 `ufw`，用 firewalld / 直接用安全组） | Ubuntu 22.04/24.04（`ufw`） |
| 部署方式 | **Docker Compose**（project `et`，容器 `easytier` + `easytier-web`） | 优先二进制 + systemd（§3），Docker 亦可用（§4） |
| 版本 | `easytier-core 2.6.4-8428a89d` = 上游最新正式版（`latest` 与 `v2.6.4` 镜像 digest 逐字节相同 `sha256:91d6732ea5ff…`） | 同上（固定 tag） |
| 组网实况 | 5 节点：`tencent-vm`(本机 `10.10.10.123`) + `DESKTOP-12PNJE0`/`oppc.local`/`CWKY-SXT`/`oranpear`，**全部 `p2p`**、loss 0.0% | — |
| 端口 | 放行 `11010/11011/21212`；**`12121` 已在云安全组删除**，管理走隧道 | 同左 |
| 附加件 | `watchtower` 已停用（客户端 API 过老，从未真正工作；它会自动升级且 `--cleanup` 删旧镜像 ⇒ 无回滚） | 建议**不用**自动升级工具 |
| 文件落位 | 原件 `/root/easytier/docker-compose.yml`（1812 B，08-26）＋ 生效副本 `/root/ET/docker-compose.yml`（2973 B，含 `name: et`、固定 tag、watchtower 进 profile）＋ 说明 `/root/ET/NOTES.md` | — |

> 两份可直接照抄的清单：[[easytier巡检-2026-09-26]]（巡检结论、端口职责分层、回滚清单）、[[服务器安全加固]] §6（安全组逐条）。

---

## 九、升级与备份（两条血泪）

> [!danger] 坑一：`et.db` 是 WAL 模式，**单拷 `et.db` 得到的是空库**
> 实测：`/app/et.db` 本体只有 **4096 B**，真正的数据全在 **`et.db-wal`（696 312 B）**。
> 只 `docker cp et.db` 出来，读它只会得到 `sqlite3.OperationalError: no such table: users`。
> **正确做法**：① `docker stop easytier-web`（干净关停会触发 WAL checkpoint 回主库）**或** ② 把 `et.db` + `et.db-wal`（+`et.db-shm`）**一起**拷出来。
> 判定备份成功 = **能读出来**（`users` 表有行、托管配置有条），不是「文件大小看着差不多」。

> [!danger] 坑二：升级会**瞬断组网**，且配置与数据要分开看
> - 二进制方式：`UPDATE()`/手工换二进制，`systemctl restart` 几秒断流。
> - Docker 方式：`up -d` 重建容器 ⇒ 几秒断流；**配置在宿主 `/etc/easytier` 不会丢**，但 `easytier-web` 的数据在容器里（坑一）。
> - 请在无人依赖组网的时段做；升级前先按坑一备份 `et.db*`。

```bash
# 升级前（Docker 方式）备份控制台数据
docker stop easytier-web
mkdir -p /opt/et-web-db-backup/$(date +%F)
docker cp easytier-web:/app/et.db     /opt/et-web-db-backup/$(date +%F)/
docker cp easytier-web:/app/et.db-wal /opt/et-web-db-backup/$(date +%F)/ 2>/dev/null || true
docker start easytier-web
```

---

## 十、客户端侧（顺带记一下笔记里的做法）

| 平台 | 做法 |
|---|---|
| **Ubuntu 桌面** | 同 §3（二进制 + 可选 GUI）；桌面版也可直接下 GUI 包 |
| **Windows** | 解压 zip → 写 `config.toml` → `.\easytier-core.exe -c D:\easytier\config.toml` 测试；开机自启用 **Servy**：`winget install servy` → `servy-cli install --name="EasyTierService" --path="D:\easytier\easytier-core.exe" --startupDir="D:\easytier" --params="-c D:\easytier\windows-config.toml" --startupType="Automatic"` → `servy-cli start --name="EasyTierService"`（也有图形界面 `Servy.exe`） |
| **macOS** | 解压 zip → 写 `mac-config.toml` → 用笔记里那套 `easytier-start/stop/status/logs/restart` 函数（`nohup … &` + PID 文件 `/tmp/easytier.pid` + 日志 `/tmp/easytier.log`）。⚠️ 日志与 PID 放在 `/tmp` 会被系统清理，长期用建议改到 `~/Library/Logs/easytier/`；更稳的是做成 **LaunchAgent**（本项目看板隧道就是这么做的，见 [[ECAS部署与踩坑手册]]） |
| **手机** | 官方 Releases 里有 `app-arm-release.apk` |

---

## 十一、十条浓缩经验

1. **静态 IP（`dhcp = false`）**：地址固定，文档、白名单、排错才可复现。
2. **`network_secret` 就是内网门票**：配置 `chmod 600`，泄露即换（全网同步）。
3. **有公网 IP 的那台写进所有人的 `[[peer]]`**，并**同时给 tcp 和 udp** 两条，打洞成功率明显更高。
4. **`11010` 必须 TCP+UDP 都放**；只放 TCP 会退化成中继。
5. **`12121` 永远别放公网**，管理走 `ssh -L`；登录页 API Host 记得填 `127.0.0.1:12121`。
6. **云安全组与 ufw 是两道门**，少放一道都不通；删规则时确认那一行里只有你想删的端口。
7. **镜像/二进制固定到具体版本**，`latest` + 自动升级工具 = 计划外变更 + 无回滚。
8. **升级前备份 `et.db` 要连 `-wal` 一起**，并**验证能读**；容器里没挂卷的数据等于随时会没。
9. **改配置前先留一份能用的**：`config/*.conf` 复制成 `.bak-日期`，改坏了 5 秒能回退。
10. **排错顺序**：`ip addr`（网卡）→ `easytier-cli peer`（对端）→ `ss -lntup`（监听）→ 安全组（外网）→ 日志。

---

## 相关

- [[easytier巡检-2026-09-26]] —— 本项目服务器的巡检、`12121` 收口与升级分析
- [[服务器安全加固]] —— 安全组逐条、端口现状
- [[ECAS部署与踩坑手册]] —— 同机上的 ECAS 采集链（隧道、systemd、凭据）
- 官方文档：<https://easytier.cn/guide/installation.html> ｜ 配置助手 <https://easytier.cn/assistant/>
