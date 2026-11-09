# ECAS 浓度计在线监测系统 · VS Code 远程查看与编辑教程

> 建立日期：2026-11-05 ｜ 适用：Mac（VS Code）→ EasyTier → `ubuntu-super`（现网栈）
> 目标：在 VS Code 里**直接看现网源码、改完就地构建生效**，不用来回到服务器敲 vim。
> 本机已实测的部分在文中标 ✅；需要先生本人操作的步骤标 👉。
>
> 👉 **只想照着一步步敲命令？看 [`开发到上线-分步操作手册.md`](./开发到上线-分步操作手册.md)**
> （打开 → 本地跑 → 热加载 → 推服务器 → 回传，命令全部实测过）。

---

## 0. 一句话结论

现网代码就在 **`ubuntu-super:/opt/ecas`**，用 **VS Code Remote-SSH** 连过去、打开这个目录，
就是「远程编辑」的全部。改后端 = 改 `backend/app/*.py` → `docker compose build api` →
`sudo systemctl restart ecas-stack`；改前端在 Mac 上 `pnpm build` 后把 `dist` 同步过去。

前置只有两件事：**VS Code 升到 1.145.0**（现在的 1.144.0 连不上，见 §2.1）、**EasyTier 通**（`ssh ubuntu-super` 能进）。

> 💡 **升级前也能看代码**：现网后端（`/opt/ecas/backend/app`）与 Mac 的 `web/backend/app` **逐字节一致**，
> RTU 主站 / 流量计入库 / 采集器脚本在 Mac 的 `scripts/485/` 里也都有——**现在就能在本地 VS Code 读全量源码**。
> 远程连接的增量价值是「在服务器上直接改、直接构建、直接看日志」，这一步只卡 VS Code 版本。

---

## 1. 先看清楚：代码在哪、系统怎么跑

### 1.1 三台机器，各管什么

| 位置 | 角色 | 有什么代码 |
|---|---|---|
| `Mac:/Users/lijc/source/qt/ecas` | **开发机 / 全量仓库** | 全项目 + git（origin GitHub）+ 前端源码（唯一副本） |
| `ubuntu-super:/opt/ecas` | **现网运行目录** | 后端 / 采集器 / 流量计入库 / RTU 主站 / SQL / 编排 / 前端构建产物 |
| 腾讯云 `root@124.222.122.146:/root/ecas` | MQTT 中继 + 边缘 nginx | 只管收数与转发，**不含本系统页面代码**（2026-09-29 起 Web 栈已迁走） |

✅ 已核对：`Mac:web/backend/app/` 与 `ubuntu-super:/opt/ecas/backend/app/` 内容**逐字节一致**（只有 mtime 不同）。
也就是说两边是镜像关系——**在哪边改都行，但必须只认一边**（见 §7）。

### 1.2 现网在跑什么（`ubuntu-super`）

```text
DPRO-66 浓度计 ──RS485──┐
                        ├─ DTU(透传) ── ecas-rtu-master(容器) ──► rtu-live.jsonl ── ecas-flow-ingest(systemd) ──► flow_reading
FMC400 流量计 ──RS485───┘                      │
                                               └─ MQTT(127.0.0.1:1883) ──► ecas-collector(容器) ──► PostgreSQL(raw_reading)
                                                                                        │
                                                     ecas-api(容器, FastAPI + 前端静态) ─┘
                                                              │
                                                  10.10.10.11:18080（EasyTier 网内）
```

| 组件 | 形态 | 代码位置 | 改完怎么生效 |
|---|---|---|---|
| `ecas-api` | Docker 容器 | `/opt/ecas/backend/app/*.py` | 重建镜像 + 重启栈 |
| `ecas-collector` | Docker 容器 | `/opt/ecas/collector/` | 重建镜像 + 重启栈 |
| `ecas-rtu-master` | Docker 容器（**代码目录直接挂载**） | `/opt/ecas/rtu-master/rtu_master.py` | `docker restart ecas-rtu-master` |
| `ecas-flow-ingest` | systemd 常驻 | `/opt/ecas/flow/flow_ingest.py` | `sudo systemctl restart ecas-flow-ingest` |
| `ecas-db` | Docker 容器 | `/opt/ecas/sql/*.sql`（只在建库时执行） | 手工 psql |
| 前端页面 | 静态产物，挂进 api 容器 | `/opt/ecas/frontend-dist/`（源码在 Mac） | 同步 dist，刷新浏览器 |
| 看门狗 / 备份 | systemd 定时 | `/opt/ecas/watchdog/`、`/opt/ecas/backup/` | `sudo systemctl restart …` |

✅ 已核对：`docker compose build api` 在 `ubuntu` 用户下可直接跑（0.7 s、全缓存命中）。

### 1.3 `/opt/ecas` 目录导览（读代码从这看）

```
/opt/ecas
├── backend/app/          ⭐ FastAPI 后端（21 个路由，换算口径唯一实现）
│   ├── main.py           路由 + 前端静态托管 + WebSocket（最大，56 KB）
│   ├── convert.py        raw → mA → 浓度（+0.48 补偿）唯一实现 ← 读代码先看这个
│   ├── alarms.py         报警规则引擎（5 类事件、幂等、落在 alarm_event 表）
│   ├── accounts.py       账户 / 改密 / 会话
│   ├── auth.py           令牌 + 会话校验
│   ├── db.py             psycopg 连接池与查询助手
│   ├── report.py         班次 Excel 报表（openpyxl + matplotlib 画图）
│   ├── shifts.py         班次切分（白班 08–20 / 夜班 20–08）
│   ├── flow.py           流量计接口
│   ├── export_flow.py / export_xlsx.py   导出
│   ├── settings.py       配置（ECAS_ 前缀环境变量）
│   └── schemas.py        接口契约（Pydantic）
├── collector/            ⭐ MQTT TLS 采集器（dpro66_monitor.sh 落库 + mqtt_tls_feeder.py）
├── flow/flow_ingest.py   ⭐ 流量计 JSONL → flow_reading（断点续读 state.json）
├── rtu-master/rtu_master.py  ⭐ RS485 Modbus RTU 主站（两台从站：站8 FMC400 / 站1 浓度计）
├── sql/                  001_init / 002_queries / 003_migrate / 004_flow_reading
├── systemd/              单元文件留档（ecas-stack / flow-ingest / watchdog / backup）
├── watchdog/             数据断流 / 容器 / 看板 / 备份 / 磁盘巡检（变化写 /var/log/ecas/watchdog.jsonl）
├── backup/ecas-backup.sh 每日 03:30 pg_dump → /data/ecas/backups/ecas-YYYY-MM-DD.dump
├── mqtt/                 Mosquitto 证书与 broker 数据（certs/ data/）
├── secrets/              ca.crt（MQTT TLS 校验用，非口令目录）
├── frontend-dist/        前端构建产物（api 容器只读挂载它）
├── docker-compose.yml    栈定义（db / api / collector + secrets）
├── .env                  非敏感配置（口令在 credentials/，600 root）
├── credentials/          ⛔ 700 root：pg-password / auth-token / internal-token / mqtt-pass
├── rtu-master/logs/rtu-live.jsonl   采集原始帧（20 MB 量级且持续增长，别用 VS Code 全文打开）
└── README.md             部署说明
```

**30 分钟读代码路线**：
`convert.py`（口径）→ `main.py` 的 21 个路由 → `db.py` → `alarms.py` → `collector/dpro66_monitor.sh` → `rtu_master.py`。

---

## 2. 一次性准备

### 2.1 VS Code 版本：必须升到 1.145.0（1.144.0 连不上，已实测）

当前本机是 **1.144.0**（commit `2c33b50b…`）。⚠️ 实测**这一版的 Linux server 包在微软 CDN 上拿不到**
（服务端下载器走的是 `update.code.visualstudio.com/commit:<commit>/…`）：

```text
commit:2c33b50b…(1.144.0) → HTTP 404   # prss 直链同样 400；两个 URL 都取不到 ⇒ 连接必卡在
commit:261c84cb…(1.144.1) → 302 → 200  # 「Downloading VS Code Server」
commit:a717861c…(1.145.0) → 302 → 200  # ← 官方「检查更新」给出的目标版本
```

👉 先生手动升级一次（`/opt/homebrew` 装的也可以 `brew upgrade --cask visual-studio-code`）：

- 菜单 **Code → 检查更新…**（或 `Cmd+Shift+P` → `Code: Check for Updates`）；
- 本机现在的更新目标是 **1.145.0**（commit `a717861c07586a53d83b80d742cd3f59b20a6273`，✅ 已向 update API 核实）；
- 升级后 `code --version` 的 commit 会变，服务器上的 server 目录也跟着换（见 §2.4）。

### 2.2 Remote-SSH 扩展 ✅ 已装

```bash
code --list-extensions | grep remote        # ms-vscode-remote.remote-ssh 0.128.0
```

没装的话：`code --install-extension ms-vscode-remote.remote-ssh`（扩展市场直连会失败，需走 7897 代理：
`HTTPS_PROXY=http://127.0.0.1:7897 code --install-extension …`）。

### 2.3 SSH 免密通

`~/.ssh/config` 里已有 `Host ubuntu-super`（`10.10.10.11`，用户 `ubuntu`，`~/.ssh/id_ed25519`）。验证：

```bash
ssh ubuntu-super 'hostname; whoami'     # → ubuntu-Super-Server / ubuntu
```

> 不通只有一个常见原因：**EasyTier 叠加网断了**。Mac 侧看 `pgrep -x easytier-core`；
> 断网时先恢复叠加网，VS Code 这边不用改任何配置。

### 2.4 服务器侧的 VS Code Server（一次性，约 220 MB）

第一次连接时 VS Code 会自动把 server 传到 `ubuntu-super:~/.vscode-server/bin/<commit>/`。
✅ 已经**预置两个版本**（各 641 MB，磁盘 329 GB 富余），升完级首次连接**几秒即通**，不用等下载：

| 客户端版本 | commit（目录名） | 状态 |
|---|---|---|
| 1.145.0（官方更新目标） | `a717861c07586a53d83b80d742cd3f59b20a6273` | ✅ 已解开，`code-server --version` = 1.145.0 |
| 1.144.1（过渡版） | `261c84cb1eddf11235f9e2c921425e9663fbe556` | ✅ 已解开，`code-server --version` = 1.144.1（备用） |

✅ **2026-11-05 已升级到位**：本机 `code --version` = **1.145.0 / a717861c…**，与上表第二行**完全对上**；
server 端已做端到端自测（`--start-server` 后 `GET /version` 返回同一 commit、扩展宿主启动成功）。
远程侧还预装了 Python 扩展套件：`ms-python.python 2026.10.0` / `pylance 2026.4.1` / `debugpy 2026.6.0` / `python-envs 1.42.0`
（共 265 MB）。Docker / YAML 扩展没预装，进远程窗口后在扩展面板一键装即可。

- 查看/清理：`ssh ubuntu-super 'du -sh ~/.vscode-server'`（上面两版已占 1.3 GB，再装扩展还会涨；磁盘 329 GB 富余）
- 如果先生升到了别的版本，首次连接会自己下载；**下载放在服务器侧更快**（实测 ~5 MB/s，40–60 s）。
  可在 VS Code 设置里设 `remote.SSH.localServerDownload: "off"` 强制服务器侧下载（Mac 侧走代理只有 ~300 KB/s）。
- 版本对不上又总失败时，走 §10.1 手动预置。

---

## 3. 第一次连接（5 步）

1. `Cmd+Shift+P` → 输入 **`Remote-SSH: Connect to Host...`** → 选 **`ubuntu-super`**。
2. 新窗口右下角提示 "Downloading VS Code Server" / "Installing…"：等 5–60 秒（预置命中会很快）。
3. 左下角状态栏出现 **`SSH: ubuntu-super`**，说明已在远程。
4. `File → Open Folder…` → 输入 **`/opt/ecas`** → Open。
5. 首次会问 "Do you trust the authors…" → **Yes, I trust the authors**。

连接成功后，**终端（Ctrl+`）直接就是服务器上的 shell**，`pwd` 应是 `/home/ubuntu`；
所有命令都在服务器执行，不用再 `ssh`。

> 小贴士：远程窗口里按 `Cmd+O` 打开的也是服务器路径；
> 想回到本地开发，用 `File → Open Recent` 或关掉这个窗口，别在远程窗口里开本地目录。

### 3.1 在远程侧装扩展（Python / Docker）

远程窗口的扩展是**装在服务器上**的，跟本地那批互不相干。左侧扩展面板搜：

| 扩展 | 用途 |
|---|---|
| `ms-python.python`（含 Pylance） | 后端 Python 补全/跳转 |
| `ms-azuretools.vscode-docker` | 看容器、`compose` 文件高亮 |
| `redhat.vscode-yaml` | compose / systemd 文件校验 |

> ✅ 截至 2026-11-05：`ms-python.python` 套件**已在服务器侧装好**，连上就有补全和跳转；
> Docker / YAML 若在面板里装得慢，就是服务器到扩展市场的网络问题，不影响使用。

点 "Install in SSH: ubuntu-super"。若卡住（服务器到扩展市场慢），用 §10.2 的 VSIX 离线装。

---

## 4. 读懂系统：路由与数据口径

后端 21 个路由（`backend/app/main.py`），按用途分四组：

| 组 | 路由 |
|---|---|
| 存活/鉴权 | `GET /healthz`、`GET /api/auth` |
| 浓度计 | `/api/params` `/api/health` `/api/readings` `/api/summary` |
| 流量计 | `/api/flow/params` `/api/flow/latest` `/api/flow/readings` `/api/flow/summary` |
| 导出 | `/api/export.csv` `/api/export.xlsx` `/api/export/flow.csv` `/api/export/flow.xlsx` `/api/shifts` `/api/export/shifts` |
| 报警 | `GET /api/alarms`、`PUT/DELETE /api/alarms/config`、`POST /api/alarms/scan` |
| 实时 | `WS /ws/readings` |
| 账户（内部） | `/internal/accounts*`（另一把令牌 `ECAS_INTERNAL_TOKEN`） |

口径只实现一处（`convert.py`）：`浓度 = (raw − 4000) / 160 + 0.48`；
断线判据 `raw < 3600`、超量程 `raw > 20500`。前端只展示、不重算。

---

## 5. 改代码 → 生效（按改动类型）

> 以下命令都在 **VS Code 远程终端**里跑（等价于 `ssh ubuntu-super` 后执行）。

### 5.1 改后端（最常见）

```bash
cd /opt/ecas

# 1) 改完先跑语法自检（✅ 已验证可用）：镜像里没有 pytest，app 目录也是只读的
#    （compileall 会因写 __pycache__ 报 Read-only file system），所以用 ast 只解析不落盘
docker compose run --rm --no-deps --entrypoint python api \
  -c "import ast,pathlib; [ast.parse(p.read_text()) for p in pathlib.Path('app').rglob('*.py')]; print('syntax ok')"

# 2) 重建镜像（只重建 api，几秒到几十秒）
docker compose build api

# 3) 应用（root 读 credentials/，这是官方路径）
sudo systemctl restart ecas-stack

# 4) 验收
curl -s http://10.10.10.11:18080/healthz          # → ok
curl -s http://10.10.10.11:18080/api/health | head -c 300
docker compose ps                                  # api 应为 Up
docker compose logs -f --tail=50 api               # 看启动报错
```

> ✅ 实测 `docker compose build api` 在 `ubuntu` 用户下可跑（0.7 s 全缓存）。
> `docker compose up -d` 在非 root 下**可能因 `credentials/`（700 root）读不到而报权限错**——
> 不要跟它较劲，**应用统一走 `sudo systemctl restart ecas-stack`**（它会 `docker compose up -d`）。
> 这条命令要 sudo 口令（在密钥卷里，或先生本人在终端输）。

### 5.2 改采集器

```bash
cd /opt/ecas
docker compose build collector
sudo systemctl restart ecas-stack
docker compose logs -f --tail=80 collector
```

### 5.3 改 RTU 主站（代码是挂载进容器的，不用重建镜像）

```bash
docker restart ecas-rtu-master
docker logs --tail=50 ecas-rtu-master
tail -n 3 /opt/ecas/rtu-master/logs/rtu-live.jsonl    # 确认还在出帧
```

### 5.4 改流量计入库（宿主机 systemd）

```bash
sudo systemctl restart ecas-flow-ingest
systemctl status ecas-flow-ingest --no-pager | head -12
tail -n 20 /opt/ecas/flow/state.json
```

### 5.5 改前端（源码不在服务器上）

前端源码只有 Mac 有一份（`web/frontend/src/`），**推荐仍在 Mac 上改**（`pnpm dev` 有热更新）：

```bash
# Mac：
cd /Users/lijc/source/qt/ecas/web/frontend
pnpm dev                       # 本地开发，http://127.0.0.1:5173
pnpm build                     # 产出 dist/

# 同步到现网（api 容器只读挂载 frontend-dist，不用重启；浏览器强刷 Cmd+Shift+R）
cd /Users/lijc/source/qt/ecas
rsync -a --delete -e "ssh -o ControlPath=none" web/frontend/dist/ ubuntu-super:/opt/ecas/frontend-dist/
```

> 想在服务器上也改前端？见 §10.3（在服务器装 Node，把源码放 `/opt/ecas/frontend`，
> 代价是多一份源码副本，**默认不建议**）。

### 5.6 改 SQL

`sql/*.sql` **不会自动执行**（只有 `001_init.sql` 在首次初始化空库时挂进 postgres 容器）。
要改表结构就写成新文件（`005_xxx.sql`），然后手工执行：

```bash
docker exec -i ecas-db psql -U ecas -d ecas < /opt/ecas/sql/005_xxx.sql
```

---

## 6. 只看不改（纯浏览）

不需要构建、不影响线上。连上去直接 `File → Open Folder → /opt/ecas`，
或在远程窗口里用 `Cmd+P` 直接跳文件。**注意别打开** `rtu-master/logs/rtu-live.jsonl`
（20 MB 量级、持续追加的文件，编辑器会卡）和 `credentials/`（700 root，打不开是正常的）。

---

## 7. 改完回传 Mac（别让两边打架）

**规则：一次会话只认一边为主。** 服务器的 `/opt/ecas` 不是 git 仓库，Mac 的仓库才是源头；
两边靠 `web/deploy/ubuntu/deploy.sh` 单向同步（**Mac → 服务器**），所以远程改的东西必须**手动拉回**。

### 7.1 一张表看清「服务器路径 ↔ Mac 仓库路径 ↔ push 会不会覆盖」

`deploy.sh push` 的映射关系（摘自 `deploy.sh:100-172`，✅ 已核对）：

| 服务器（`/opt/ecas/…`） | Mac 仓库（`web/…`、`scripts/…`） | push 会覆盖吗 |
|---|---|---|
| `backend/app/` | `web/backend/app/` | ✅ 会 |
| `backend/{Dockerfile,entrypoint.sh,pyproject.toml,uv.lock}` | `web/deploy/ubuntu/backend/` + `web/backend/` | ✅ 会 |
| `collector/{Dockerfile,entrypoint.sh,mqtt_tls_feeder.py}` | `web/deploy/ubuntu/collector/` | ✅ 会 |
| `collector/dpro66_monitor.sh` | `scripts/485/dpro66_monitor.sh` | ✅ 会 |
| `sql/*.sql` | `scripts/485/sql/*.sql` | ✅ 会（只推文件，DDL 仍要手工执行） |
| `frontend-dist/` | `web/frontend/dist/`（构建产物） | ✅ 会 |
| `secrets/ca.crt` | `web/deploy/ecas-mqtt-ca.crt` | ✅ 会 |
| `systemd/` `watchdog/` `backup/` `docker-compose.yml` | `web/deploy/ubuntu/` 下同名 | ✅ 会 |
| **`flow/`（含 `flow_ingest.py`、`state.json`）** | `scripts/485/flow_ingest.py` | ❌ **排除**（只在目标机的运行时目录） |
| **`rtu-master/`（含 `rtu_master.py`、`mqtt-pass`、`logs/`）** | `scripts/485/rtu_master.py` | ❌ **排除** |
| **`mqtt/`（certs、data）** | 无（服务器独有） | ❌ 排除 |
| **`credentials/`（700 root）** | 无（服务器独有） | ❌ 排除 |
| **`.env`** | 模板写在 `deploy.sh` 里，落盘后以服务器为准 | ❌ 排除 |

⚠️ 两个方向的风险不一样：

- **push 会覆盖的**（上半张表）：远程改了忘拉回 → 下次从 Mac 部署**静默回滚**你刚才的改动。
- **push 不覆盖的**（`flow/`、`rtu-master/`）：在服务器改的**不会被打回**，但也不会自动回到 Mac ⇒
  时间一长两边就分叉了。所以改完**必须**执行下面这条拉回。

### 7.2 收工前的拉回命令（Mac 上执行）

```bash
cd /Users/lijc/source/qt/ecas
R='ssh -o ControlPath=none'

# 后端（push 会覆盖它，必须拉回）
rsync -av --exclude '__pycache__' -e "$R" ubuntu-super:/opt/ecas/backend/app/ web/backend/app/

# 采集器（可执行脚本可能在服务器上被手工调过）
rsync -av -e "$R" ubuntu-super:/opt/ecas/collector/dpro66_monitor.sh scripts/485/

# 流量计入库 + RTU 主站（push 不覆盖，最容易分叉）
rsync -av -e "$R" ubuntu-super:/opt/ecas/flow/flow_ingest.py scripts/485/
rsync -av -e "$R" ubuntu-super:/opt/ecas/rtu-master/rtu_master.py scripts/485/

# 看一眼到底差了哪些文件（不落盘）
git status --short web/backend/app scripts/485
```

> 拉回后 `git diff` 就是这次远程改动的全部内容——这也是「远程编辑」最终要落到的交付物：
> **改动进 Mac 仓库 → 提交 → 才能被下次部署带回去**。

### 7.3 改前快照（30 秒，建议养成）

```bash
ssh ubuntu-super 'cd /opt/ecas && tar -czf ~/ecas-src-$(date +%Y%m%d-%H%M%S).tgz \
  backend/app collector flow rtu-master/*.py docker-compose.yml .env'
```

出问题就 `tar -xzf` 解回去，再 `sudo systemctl restart ecas-stack`。

---

## 8. 红线

1. **`/opt/ecas/credentials/` 是 700 root**：不要 `chmod`、不要复制、不要打印。口令只经 systemd/compose 注入。
2. **不要 `docker compose down`**：会停掉 db/api/collector，网页直接 502。重建只用 `build` + `systemctl restart ecas-stack`。
3. **`.env` 不是秘密，但改错会出事**：`BIND_IP` 现在是 `10.10.10.11`（只对 EasyTier 网内开放；
   `docker-compose.yml` 注释与 `deploy.sh` 模板里写的 `127.0.0.1` 是 2026-10-26「只走 SSH 隧道」时期的旧口径，
   **以服务器上 `.env` 的实际值为准**，且 `push` 不覆盖 `.env`），
   改成 `0.0.0.0` 等于对办公网/公网开端口；`ECAS_WEB_PORT` 别撞宿主机 8080（已被生产容器占用）。
4. **数据库唯一副本**在 `/data/ecas/pgdata`，备份在 `/data/ecas/backups/ecas-*.dump`（每天 03:30，带 `.sha256`）。
   `/data/ecas` 属主 root、`ubuntu` 用户读不到（正常）；别删、别 `docker volume prune`、别改挂载。
5. **别在服务器上 `git` 操作**：`/opt/ecas` 不是 git 仓库；仓库在 Mac（`origin: Free-Aaron-Li/ecas`）。
6. 改 `backend/app/main.py` 的静态目录相对层级会 404：容器里固定是 `parents[2]/frontend/dist`（见 Dockerfile 注释）。

---

## 9. 排错

| 症状 | 原因 | 处理 |
|---|---|---|
| 连接卡在 "Downloading VS Code Server" 后报错 | 1.144.0 的 server 包在 CDN 上 404/400 | 升级到 1.145.0（已预置）；或 §10.1 手动预置 |
| 连接超时 / `ssh: connect to host 10.10.10.11 port 22` | EasyTier 叠加网断了 | Mac：`pgrep -x easytier-core`；恢复后重连 |
| 提示 `permission denied` 保存文件 | 文件属主是 root | 属主 ubuntu 的文件都能写；root 的（`credentials/`、`systemd/` 部分）本来就不该改 |
| 扩展装不上、一直转圈 | 服务器到扩展市场慢 | §10.2 VSIX 离线安装，或 `remote.SSH.localServerDownload: off` |
| 改了后端页面没变化 | 只 build 没 restart，或浏览器缓存 | `sudo systemctl restart ecas-stack` + 强刷 |
| 页面白屏 / 404 静态资源 | `frontend-dist` 没同步或版本不一致 | 重新 rsync dist，确认 `index.html` mtime |
| api 起来就退出 | 代码异常 / 依赖变动 | `docker compose logs --tail=100 api`；`docker compose build api --no-cache` |
| 中文报表变方块 | 镜像缺中文字体 | 别动 Dockerfile 里的 `fonts-noto-cjk`（是报表正确性依赖） |

日志位置：容器 `docker compose logs -f <svc>`；flow / watchdog / backup 用
`journalctl -u ecas-flow-ingest -u ecas-watchdog -u ecas-backup -f`；
看门狗的**状态变化**单独一行一条，落在 `/var/log/ecas/watchdog.jsonl`（一切正常时没有新行，不需要重启也不该空报）。

---

## 10. 附录

### 10.1 手动预置 VS Code Server（网络/版本折腾时的兜底）

在 Mac 上下载对应 commit 的包（走代理），传到服务器解开：

```bash
C=a717861c07586a53d83b80d742cd3f59b20a6273     # = VS Code 1.145.0 的 commit，用 `code --version` 查
curl -fL --max-time 600 -o /tmp/vscs.tgz \
  "https://update.code.visualstudio.com/commit:$C/server-linux-x64/stable"   # 注意是 commit: 冒号形式
ssh ubuntu-super "mkdir -p ~/.vscode-server/bin/$C"
scp /tmp/vscs.tgz ubuntu-super:/tmp/
ssh ubuntu-super "tar -xzf /tmp/vscs.tgz -C ~/.vscode-server/bin/$C --strip-components=1 && \
                  ~/.vscode-server/bin/$C/bin/code-server --version"
```

> 注意：目录名必须是**客户端 commit**（`code --version` 第 2 行），否则 VS Code 认不出来会重下。

### 10.2 扩展离线安装（VSIX）

在能上网的机器下 VSIX → `code --install-extension xxx.vsix`；远程窗口里则先
`Cmd+Shift+P → Remote-SSH: Install VSIX on Remote`（或把 vsix 传到服务器后用
`~/.vscode-server/bin/<commit>/bin/code-server --install-extension xxx.vsix`）。

### 10.3 可选：让服务器也能构建前端

只在「先生长期不在 Mac 边」时才值得做（多一份源码副本）：

```bash
# 服务器（用户级，不动系统 Node）
curl -fsSL https://deb.nodesource.com/... # 或直接 nvm：
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.nvm/nvm.sh && nvm install 22
corepack enable && corepack prepare pnpm@latest --activate

# 把 Mac 的前端源码同步过去（排除 node_modules）
rsync -a --exclude node_modules -e "ssh -o ControlPath=none" \
  web/frontend/ ubuntu-super:/opt/ecas/frontend/
# 服务器上构建 → 直接覆盖产物目录
cd /opt/ecas/frontend && pnpm install && pnpm build
rsync -a --delete dist/ /opt/ecas/frontend-dist/
```

### 10.4 SSH 保活（推荐加进 `~/.ssh/config` 的 `Host ubuntu-super` 段）

```sshconfig
Host ubuntu-super
    HostName 10.10.10.11
    User ubuntu
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

VS Code 长时间挂着不掉线，跑长任务也不怕。

### 10.5 从外网连（先生不在家时）

`ubuntu-super` 的 22 端口**不要**对公网开放。正确姿势：先让 Mac/笔记本加入 EasyTier 叠加网
（`10.10.10.x` 网段），再 Remote-SSH；或在 Mac 上开 SSH 隧道，把服务器 22 转发出来：

```bash
ssh -N -L 2222:10.10.10.11:22 lijc@<中转机>     # 然后把 VS Code 连 localhost:2222
```

---

## 11. 验收清单（照做一遍就算会了）

- [ ] `ssh ubuntu-super 'echo ok'` 通
- [ ] VS Code 版本 ≥ 1.145.0（或 1.144.1），`code --list-extensions | grep remote-ssh` 有输出
- [ ] Remote-SSH 连上 `ubuntu-super`，左下角显示 `SSH: ubuntu-super`
- [ ] 打开 `/opt/ecas`，能读 `backend/app/convert.py`
- [ ] 远程终端 `cd /opt/ecas && docker compose ps` 五个容器都在 Up（db / api / collector / rtu-master / mosquitto）
- [ ] 随便改一行后端注释 → `docker compose build api` → `sudo systemctl restart ecas-stack` → `curl healthz` 仍 ok
- [ ] `rsync` 把改动拉回 Mac，`git diff` 能看到
- [ ] 知道出事怎么回滚（§7.1 快照）
