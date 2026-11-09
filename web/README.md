# Web 前后端（本地开发）

ECAS 阶段 0 的**本地验证栈**：把现场 485/DTU 采集到的原始值，在开发机上跑通
「PostgreSQL → FastAPI → Web 看板」这条链路，验证没问题再谈对外发布。

> ⚠️ 这个目录**不是 C++ 工作区的一部分**：它不含任何 `CMakeLists.txt`，
> 不参与 `ecas` 的 CMake 构建，也不该被 CLion 索引（见文末「CLion」）。

```
web/
├── backend/        FastAPI + psycopg 3（只读数据库；换算口径的唯一实现）
│   ├── app/
│   │   ├── settings.py   配置（环境变量前缀 ECAS_）；tz 决定班次边界切在哪个时区
│   │   ├── convert.py    raw → mA → 浓度（+0.48 补偿），唯一实现
│   │   ├── db.py         连接池 + 查询助手
│   │   ├── shifts.py     班次切分（白班 08:00–20:00 / 夜班 20:00–次日 08:00），只管时间
│   │   ├── report.py     班次 Excel 报表（openpyxl 排版 + matplotlib 画曲线图）
│   │   ├── schemas.py    接口契约（Pydantic）
│   │   └── main.py       路由 / WebSocket / 静态托管
│   └── tests/      换算 14 例 + 接口冒烟 + 班次 54 例（共 131 条）
├── frontend/       Vite + React 19 + TS 看板（recharts）
└── scripts/
    └── sync-from-cloud.sh   从云端 pg_dump 刷新本机快照
```

## 一、起后端（默认 http://127.0.0.1:8080）

```bash
cd web/backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8080     # 或：uv run python -m app.main
curl -s http://127.0.0.1:8080/healthz                        # → ok
```

数据库是本地 Homebrew PostgreSQL 18（**端口 55432，trust，无口令**，库 `ecas_dev`）；
配置里不含任何凭据。建库与拉数据见 [`docs/本地开发环境.md`](../docs/本地开发环境.md)。

## 二、起前端（开发模式，默认 http://127.0.0.1:5173）

```bash
cd web/frontend
pnpm install
pnpm dev          # /api 与 /ws 自动代理到 127.0.0.1:8080
```

或者构建后由后端**同一个端口**托管（单 URL 部署形态）：

```bash
cd web/frontend && pnpm build      # 产出 web/frontend/dist
# 重启后端 → http://127.0.0.1:8080/ 直接就是看板
```

## 三、刷新数据快照

```bash
bash web/scripts/sync-from-cloud.sh     # 云端 ecas 库 → 本机 ecas_dev（幂等）
```

## 四、接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/healthz` | 存活探针（`ok`）——**不需要令牌** |
| GET | `/api/auth` | 值守门状态（`auth_required`）——**不需要令牌** |
| GET | `/api/params` | 现役口径：量程 / 补偿 / 阈值 / 公式 |
| GET | `/api/health` | 行数、最新时间、滞后秒数、是否断流 |
| GET | `/api/readings?hours&limit&offset` | 区间读数（含换算），时间倒序；hours 夹 1..720、limit 夹 1..5000 |
| GET | `/api/summary?hours` | 区间统计（均值/极值**只算有效区间** 4000~20500）+ 越限计数 |
| GET | `/api/export.csv?hours&limit` | 导出区间原始值 CSV（**中文表头** + 口径注释头）；limit 夹 1..20000 |
| GET | `/api/shifts?start&end&kinds` | 班次清单预览（只算时间、不查数据）；`start`/`end` 为 `YYYY-MM-DD`，`kinds` 取 `day,night` |
| GET | `/api/export/shifts?start&end&kinds` | **按班次导出 Excel**：单班 → `.xlsx`，多班 → `.zip`（内附 `说明.txt`）；上限 31 天 / 62 班 |
| GET | `/api/alarms?hours&limit` | 报警事件（进行中 + 已结束），带最近一次扫描结果；hours 夹 1..720、limit 夹 1..2000 |
| PUT | `/api/alarms/config?low&high` | 自定义报警上下限（**落库** `alarm_config`），0..100 且 low<high，否则 400 |
| DELETE | `/api/alarms/config` | 恢复出厂默认上下限（`ECAS_ALARM_LOW/HIGH`） |
| POST | `/api/alarms/scan` | 立刻扫一次（后端本来就每分钟自动扫） |
| WS | `/ws/readings` | 有新行就推一条（5 秒轮询数据库）；浏览器不能给 WS 带请求头，令牌只能走 `?token=` |
| — | `/internal/accounts*` | **账户接口**（建号/改密/导入/校验/启停）：前缀 `/internal/accounts`，用**专用** `ECAS_INTERNAL_TOKEN`，与看板令牌**分开**（安全审查 M-02） |

交互式文档 `/docs`、`/redoc`、`/openapi.json` **默认关闭**（安全审查 M-01）；临时要看得设 `ECAS_ENABLE_DOCS=1`。

口径只实现一次（后端），前端只展示：`浓度 = (raw − 4000)/160 + 0.48`，
断线判据 `raw < 3600`、超量程 `raw > 20500`。

## 四之二、报警阈值与导出（第 2 步）

- 报警上下限来自 `ECAS_ALARM_LOW` / `ECAS_ALARM_HIGH`（默认 **40.0 / 60.0，是占位值**，
  等工艺给定真实范围；`ECAS_ALARM_LOW=50 ECAS_ALARM_HIGH=52` 这样就能临时试越限效果）。
- 后端把浓度阈值**反解成 raw 判据**（`convert.conc_to_raw`），与正向换算共用一处：
  `low_raw = 10323`、`high_raw = 13523`（= `4000 + (conc − 0.48) × 160`，补偿 2026-09-26 现场标定为 +0.48）。
   `/api/params` 返回 `alarms{low,high,low_raw,high_raw,custom,default_low,default_high}`，
  前端画 `ReferenceLine`（**下限红、上限黄**，`ifOverflow="extendDomain"` 以免线落在数据域外被 recharts 丢掉）。
- 越限计数 `n_below_alarm` / `n_above_alarm` **只在有效区间内统计**，且
  断线/超量程行的浓度是数学外推值，**不参与越限判断**（按 `status` 列判断设备异常）。
- 导出给的是「能直接存档 / 给工艺看」的文件：UTF-8 **带 BOM**（Excel 不乱码）、
  `\r\n` 行尾、开头 10 行 `#` 注释写清口径与异常阈值，文件名形如
  `ecas_DPRO-66_720h_20260926-0957.csv`。

```bash
curl -s "http://127.0.0.1:8080/api/export.csv?hours=720" | head -14
curl -sOJ "http://127.0.0.1:8080/api/export.csv?hours=168"   # 带令牌时记得加 ?token=
```


## 四之三、报警事件（第 3 步）

报警**不是一个前端动画**，是后端的一条规则引擎：数据在哪儿（现在在云服务器、
将来在本地服务器），规则就得跟到哪儿，所以判定跑在服务端；看板只负责显示与改阈值。

- **上下限可自定义**：`PUT /api/alarms/config?low=45&high=55` 写进库表 `alarm_config`，
  不再是"改环境变量重启"。优先级 = **库里的覆盖 > `ECAS_ALARM_LOW/HIGH`（出厂默认）**；
  `DELETE` 恢复默认。落库的理由：看板画的线、`/api/summary` 数的越限行数、引擎记的事件、
  导出的注释头必须用**同一个数**，放浏览器 localStorage 会出现"页面写 45、事件按 40 记"。
- **五类事件**（`alarm_event` 表）：`break` 断线 ｜ `over` 超量程 ｜ `conc_high` 浓度高于上限 ｜
  `conc_low` 浓度低于下限 ｜ `no_data` 长时间无数据（超过 `ECAS_STALE_SECONDS`）。
- **判定用 raw 阈值**（`low_raw` / `high_raw`），不用浓度浮点比较 —— 与统计、图表逐位一致，
  边界行不会一处算进去一处没算。
- **幂等**：靠部分唯一索引 `alarm_event_open_uidx (model, kind) WHERE ended_ts IS NULL`
  保证"同一型号、同一类报警同时只有一条进行中"，同一批数据扫一百遍结果一样。
- **两个刻意的判断**（都能在 `app/alarms.py` 的注释里看到理由）：
  数据断流时**只维护 `no_data`，其它进行中的事件原地冻结**（数据停在两小时前，
  既不能判"已恢复"也不能判"仍故障"）；浓度只在**有效区间** 4000~20500 判定
  （断线行的浓度是数学外推值，可能是负数）。
- 配置：`ECAS_ALARM_SCAN=1`（默认开）、`ECAS_ALARM_SCAN_SECONDS=60`。

```bash
curl -s -X PUT "http://127.0.0.1:8080/api/alarms/config?low=45&high=55"   # 改成 45~55
curl -s "http://127.0.0.1:8080/api/alarms?hours=24"                       # 看事件
curl -s -X DELETE "http://127.0.0.1:8080/api/alarms/config"               # 恢复默认
```

## 四之四、按班次导出 Excel（第 4 步）

CSV 是「一整段原始数据」，工艺要看的是**班次**：白班 08:00–20:00、夜班 20:00–次日 08:00。
看板「最近读数」下方的「班次报表（Excel）」卡片选日期范围 + 班次类型即可导出。

- **每班一份文件**，一份里两张表：`班次报表`（元信息 / 班末读数 / 区间统计 / **曲线图** / 报警事件）
  ＋ `读数明细`（逐行带 型号·原始值·电流·未补偿浓度·浓度·状态·浓度越限）。
- **多班打一个 zip**（附 `说明.txt` 写清哪个文件对应哪个班次）；单班直接给 `.xlsx`。
- **曲线图由后端 matplotlib 渲染成 PNG 再嵌进 Excel**，不是 Excel 原生图表——
  原生图表在 WPS／手机／在线预览里经常画不出来，PNG 到哪儿都一样。中文字体按
  `Hiragino Sans GB → Noto Sans CJK → 文泉驿微米黑` 逐个探测，**探不到会退化成英文标签**。
- **班次边界按 `ECAS_TZ`（默认 `Asia/Shanghai`）切**。服务器多半跑 UTC，不显式指定的话
  「早八点」会整体错 8 小时；给非法时区名**启动即报错**（故意 fail-fast，静默回退更难发现）。
- **口径与看板逐位一致**：统计复用 `/api/summary` 那条 `SQL_SUMMARY`，读数由 `convert.py` 现算——
  报表和网页必须是同一个数（有测试逐项对账）。
- 上限 **31 天 / 62 班**（超了报 400，提示是中文人话）；单班明细另有行数上限，
  真被截断时报表里会写明「只导出了前 N 行」。

```bash
curl -s "http://127.0.0.1:8080/api/shifts?start=2026-09-25&end=2026-09-26"   # 先看有哪些班
curl -sOJ "http://127.0.0.1:8080/api/export/shifts?start=2026-09-25&end=2026-09-25&kinds=night"
curl -sOJ "http://127.0.0.1:8080/api/export/shifts?start=2026-09-25&end=2026-09-26"   # 4 班 → zip
```

> ⚠️ 服务器要**装中文字体**，否则图上标签是英文（代码有兜底、不会报错，只是不好看）。
> 部署前用 `fc-list :lang=zh` 确认一下。

## 四之五、电磁流量计（FMC400，2026-11-03 接入）

现场第二台 485 设备是电磁流量计（**FMC400，站号 8，FC03 读寄存器 0~13**）。采集器
`rtu_master` 每秒把它的 14 个寄存器写进 `/opt/ecas/rtu-master/logs/rtu-live.jsonl`，
但**不入库**；于是看板上加了一个独立区块：`flow_ingest.py` 把 JSONL 里的成功帧写进
`flow_reading`，后端解码 + 出接口，前端与浓度计同一时间窗展示。

**口径**（与浓度计同一原则：库里只存原始值，换算全在后端）：

| 表 / 字段 | 内容 |
|-----------|------|
| `flow_reading` | `model`（`FMC400`）/ `ts` / `regs integer[14]`，PK `(model, ts)`，**只存寄存器原值** |
| `/api/flow/latest` | 最新一帧解码结果 + `available`（没接入时是 `false`，不是 404） |
| `/api/flow/readings` | 同 `/api/readings` 的 `hours/start/end/limit/offset/sample` 语义 |
| `/api/flow/summary` | 区间统计（瞬时流量/流速/满量程百分比的均值与极值、空管/反流/报警次数、累积量增量） |
| `/api/flow/params` | 寄存器表、单位、公式、状态清单（前端脚注与 `params` 同一来源） |

解码在 `web/backend/app/flow.py` 一处实现（`s32(r2,r3)/100` 瞬时流量、`s32(r0,r1)/1000`
流速、`u32(r6,r7)` 正向累积、`r12` 报警位、`r13` 流向；空管 ≥50% 优先判「空管」，
流量 < -0.01 判「反流」，报警位非 0 判「报警」）。累积量是 32 位、约 **8.5 天回绕**，
所以看板只用它算**区间增量**，回绕/清零时显示提示而不是负数。

```bash
# 第一次上线：建表 + 把今天已经采到的帧回填（幂等，重复跑不会写重）
psql -f scripts/485/sql/004_flow_reading.sql          # 线上：docker exec -i ecas-db psql -U ecas -d ecas
python3 scripts/485/flow_ingest.py --once --from-start   # 本地加 --psql/--jsonl 覆盖路径

# 常驻（systemd 单元 ecas-flow-ingest.service，见 deploy/ubuntu/systemd/）
python3 scripts/485/flow_ingest.py --follow
```

> 采集侧一行代码都不用改：`rtu_master` 继续只写 JSONL。入库器只读文件、只写
> `flow_reading`，数据库不通时下一轮重试（不会拖累采集），断点存在 `state.json`。

**看板展示**（2026-11-04 加深）：

* 顶部浓度 KPI 的单位统一写 `%`；
* 「电磁流量计（FMC400）」区块 6 张即时卡：当前流量 / 流速 / 满量程占比 / 正向累积（仪表，
  附本区间增量）/ 流向 / 状态；下面一排区间瓦片（均值、最高、最低、流速均值、测点、空管、
  反流、报警、区间增量），再下面是趋势图；
* 页面上方「区间统计条」多一行「流量」，与浓度计的统计条同一张瓦片网格；
* 班次 Excel（`/api/export/shifts`）在「班次报表」里多一段「流量（电磁流量计 FMC400）」，
  有流量数据的班次另建一张「流量明细」表（时间/瞬时流量/流速/满量程/空管/累积/流向/状态/报警）。

**部署注意**：`deploy.sh push` 的 `rsync --delete` 会把 `/opt/ecas` 下**不在 stage 清单里**
的运行时目录删掉；这些目录只在目标机上存在，已显式 `--exclude`：`rtu-master`（采集代码 +
11 MB 日志）、`flow`（入库器与断点）、`mqtt`（mosquitto 证书与消息库，root 所有）。
新增运行时目录时记得同步加 exclude。

## 五、访问令牌（对外发布用）

后端**只读环境变量** `ECAS_AUTH_TOKEN`（见 `web/backend/.env.example`）：

```bash
# 开发模式（默认）：没设令牌 → 不校验，启动日志会明确写「未启用」
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080

# 启用令牌：/api/* 与 /ws/readings 都要令牌
ECAS_AUTH_TOKEN='一串够长的随机串' .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

客户端三种带法都认（WebSocket 用第 2 种，因为浏览器不能给 WS 带请求头）：

```bash
curl -H "Authorization: Bearer $T" http://127.0.0.1:8080/api/health   # ① 请求头
curl "http://127.0.0.1:8080/api/health?token=$T"                      # ② 查询串
curl --cookie "ecas_token=$T" http://127.0.0.1:8080/api/health        # ③ Cookie
```

浏览器打开看板时，前端先问 `/api/auth`：要令牌而且本机没有 → 弹「需要访问令牌」；
令牌存 `localStorage`（键 `ecas_token`），**不回传任何地方**；顶栏可「清除令牌」。

> ⚠️ 两条硬约束：`Authorization` 头与 Cookie 值**只能是 ASCII**（协议如此），
> 非 ASCII 令牌只能走 `?token=`（URL 会 percent-encode）；
> 令牌比较按 **UTF-8 字节**做常量时间比较，中文令牌也不会 500。

## 六、测试

```bash
cd web/backend && .venv/bin/pytest -q     # 135 passed（含真实库冒烟，库不在则自动跳过）
cd web/frontend && pnpm build             # tsc -b 类型检查 + 构建
```

> 本机沙箱里 `uv run` 不可用（写不了 `~/.cache/uv`），本地一律改用
> `web/backend/.venv/bin/python` 与 `.venv/bin/pytest`。

## 七、部署（现网 = ubuntu-super 的 Docker 栈）

后端 + 看板部署在 **`ubuntu-super:/opt/ecas`**（easytier 叠加网 `10.10.10.11`；
**2026-10-26 安全收口起后端只绑回环 `127.0.0.1`，只经本机 launchd SSH 隧道 `com.ecas.tunnel` 访问 `http://127.0.0.1:18080`，其它来源一律不可达**），
运行方式是 **Docker Compose 栈**（项目名 `ecas`，systemd 单元 `ecas-stack.service`）：

| 容器 | 说明 |
|------|------|
| `ecas-api` | FastAPI 后端 + 看板产物，绑 **`127.0.0.1:18080`** → 容器内 `8000`（仅本机 SSH 隧道可达） |
| `ecas-db` | `postgres:16-alpine`，数据在宿主机 **`/data/ecas/pgdata`**（数据库唯一副本） |
| `ecas-collector` | 采集链路（MQTT → 写库） |

```bash
web/deploy/ubuntu/deploy.sh preflight   # 只读体检：凭据卷、ssh、与线上凭据指纹比对
web/deploy/ubuntu/deploy.sh all         # push + creds + build + up + check（幂等）
web/deploy/ubuntu/deploy.sh push        # 只推代码（35 个文件，不含凭据与数据）
web/deploy/ubuntu/deploy.sh build       # 目标机构建镜像 ecas-api:local
web/deploy/ubuntu/deploy.sh up          # 起容器（无凭据卷时回落为直连 docker compose up）
web/deploy/ubuntu/deploy.sh check       # 端到端自检
web/deploy/ubuntu/deploy.sh logs        # 采集器日志
```

**凭据卷**：目标机的 root 口令只存在 `/Volumes/keys/ssh/ssh.toml`（外接卷，不是每台机器
都挂）。没挂时 `creds` / `backup` / `watchdog` 会在动手前直接报错拒绝，`up` 回落到用户
`ubuntu` 直连 `docker compose up`（该用户在 docker 组，等效于单元的 ExecStart，但**不动**
systemd 单元与定时器），`check` 跳过「带令牌应 200」那一项。所以「只改后端再上线」其实
`push` + `build`（+ 无卷时 `up`）就够了。完整说明见
[`deploy/ubuntu/README.md`](deploy/ubuntu/README.md)。

> ### ⚠️ `scripts/deploy-to-cloud.sh` 已退役（2026-09-29）
> 它指向腾讯云 `/root/ecas-web` + systemd 单元 `ecas-web.service`（`127.0.0.1:8080`）——
> **那已经不是现网**。腾讯云那台机器现在只做 MQTT 中继与边缘网关（docker 容器
> `ecas-edge` = nginx，占 80/443），其 `ecas-web.service` 仍是 `disabled; inactive (dead)`，
> 所以对着它跑**不会报错**，只会 rsync 一份没人用的代码、enable 一个没人用的单元 ——
> 也就是静默部署到错的地方。该脚本已加退役闸门、默认拒绝执行（考古需显式
> `ALLOW_RETIRED=1`）。旧架构的完整记录见
> [`../docs/云端部署.md`](../docs/云端部署.md)，部署件仍在 `deploy/cloud.env.example`。

## CLion（不影响 C++ 索引）

CLion 是 C++ 工作区，`node_modules` / `.venv` 无需索引。建议在项目视图里
右键 `web/frontend/node_modules`、`web/backend/.venv` → **Mark Directory as → Excluded**
（`.idea/` 已在 `.gitignore` 中，属本地设置，不会进仓库）。
