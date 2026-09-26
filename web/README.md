# Web 前后端（本地开发）

ECAS 阶段 0 的**本地验证栈**：把现场 485/DTU 采集到的原始值，在开发机上跑通
「PostgreSQL → FastAPI → Web 看板」这条链路，验证没问题再谈对外发布。

> ⚠️ 这个目录**不是 C++ 工作区的一部分**：它不含任何 `CMakeLists.txt`，
> 不参与 `ecas` 的 CMake 构建，也不该被 CLion 索引（见文末「CLion」）。

```
web/
├── backend/        FastAPI + psycopg 3（只读数据库；换算口径的唯一实现）
│   ├── app/
│   │   ├── settings.py   配置（环境变量前缀 ECAS_）
│   │   ├── convert.py    raw → mA → 浓度（+0.3 补偿），唯一实现
│   │   ├── db.py         连接池 + 查询助手
│   │   ├── schemas.py    接口契约（Pydantic）
│   │   └── main.py       路由 / WebSocket / 静态托管
│   └── tests/      换算 14 例 + 接口冒烟（29 条）
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
| GET | `/api/alarms?hours&limit` | 报警事件（进行中 + 已结束），带最近一次扫描结果；hours 夹 1..720、limit 夹 1..2000 |
| PUT | `/api/alarms/config?low&high` | 自定义报警上下限（**落库** `alarm_config`），0..100 且 low<high，否则 400 |
| DELETE | `/api/alarms/config` | 恢复出厂默认上下限（`ECAS_ALARM_LOW/HIGH`） |
| POST | `/api/alarms/scan` | 立刻扫一次（后端本来就每分钟自动扫） |
| WS | `/ws/readings` | 有新行就推一条（5 秒轮询数据库） |

口径只实现一次（后端），前端只展示：`浓度 = (raw − 4000)/160 + 0.3`，
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
cd web/backend && uv run pytest -q        # 61 passed（含真实库冒烟，库不在则自动跳过）
cd web/frontend && pnpm build             # tsc -b 类型检查 + 构建
```

## 七、部署到云服务器（已上线）

后端 + 看板已经部署到采集链路所在的腾讯云服务器上（`systemd` unit `ecas-web.service`，
只监听 `127.0.0.1:8080`；数据与报警判定都在那边）：

```bash
web/scripts/deploy-to-cloud.sh                # 构建 → 同步 → uv sync → 重启 → 自检（幂等）
HOST_ALIAS=ecas web/scripts/deploy-to-cloud.sh --no-build   # 只改后端时
ssh -L 18080:127.0.0.1:8080 ecas              # 开隧道后浏览器打开 http://127.0.0.1:18080/
ssh ecas 'grep ECAS_AUTH_TOKEN /root/ecas-web/ecas-web.env' # 取看板令牌
```

完整说明（首次部署做了什么、令牌轮换、回滚到 `ecas-api`、安全待办）见
[`../docs/云端部署.md`](../docs/云端部署.md)。部署件就在本目录：`deploy/ecas-web.service`、
`deploy/cloud.env.example`、`scripts/deploy-to-cloud.sh`。

## CLion（不影响 C++ 索引）

CLion 是 C++ 工作区，`node_modules` / `.venv` 无需索引。建议在项目视图里
右键 `web/frontend/node_modules`、`web/backend/.venv` → **Mark Directory as → Excluded**
（`.idea/` 已在 `.gitignore` 中，属本地设置，不会进仓库）。
