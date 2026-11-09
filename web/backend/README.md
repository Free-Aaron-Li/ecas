# ECAS 后端（FastAPI）

浓度计原始值 → 换算/补偿 → RESTful API；同时托管前端构建产物（同一个进程，不再引 nginx）。

## 跑起来

```bash
# 本机开发（需要 ≥3.13，依赖由 uv 管）
cd web/backend
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8080
# 等价：python app/main.py（读 settings.host / settings.port）
```

容器里跑（生产/测试）看 `web/deploy/ubuntu/`：那边的 compose 会用本目录的
`Dockerfile` 构建镜像，并**必须**把前端产物挂到 `/srv/web/frontend/dist`
（`app/main.py` 用 `Path(__file__).resolve().parents[2] / "frontend" / "dist"` 定位它，
所以容器内的层级要保持成 `<root>/web/backend` + `<root>/web/frontend/dist`）。

## 配置（前缀 `ECAS_`，可写 `.env`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ECAS_DSN` | `postgresql://ecas@/ecas_dev?host=/tmp&port=55432` | 库连接串；生产用 `postgresql://ecas:<口令>@<主机>:5432/<库>` |
| `ECAS_AUTH_TOKEN` | 空 | 看板令牌。⚠️ **空 = fail-closed**（`/api/*` 一律 503、容器入口直接拒绝启动），**不是「不校验」**；只有显式设了 `ECAS_ALLOW_NO_AUTH=1` 的本机开发才放行（安全审查 M-03） |
| `ECAS_INTERNAL_TOKEN` | 空 | 内部账户接口 `/internal/accounts*` 的**专用**令牌，与看板令牌分开（M-02）；为空则这些接口拒绝服务 |
| `ECAS_ALLOW_NO_AUTH` | `false` | 本机开发逃生门：显式置 `1` 才允许无令牌启动（**只该绑 127.0.0.1**） |
| `ECAS_ENABLE_DOCS` | `false` | 是否开放 `/docs` / `/redoc` / `/openapi.json`（M-01 默认全关） |
| `ECAS_HOST` / `ECAS_PORT` | `127.0.0.1` / `8080` | 监听地址与端口。⚠️ 容器里被 compose 覆盖为 `0.0.0.0:8000`，再由 compose 映射成 mesh 地址的 `18080`（`docker-compose.yml:93`） |
| `ECAS_TZ` | `Asia/Shanghai` | 班次切分与报表用的时区 —— **班次边界 08:00 / 20:00 按它算**，报表表头也印它 |
| `ECAS_MODEL_NAME` | `DPRO-66` | 型号（入库/查询都按它过滤） |
| `ECAS_I_LO` `ECAS_I_HI` `ECAS_C_LO` `ECAS_C_HI` | 4 / 20 / 0 / 100 | 量程 |
| `ECAS_OFFSET_MODE` | `add` | `add`=加性偏移，`mul`=乘性百分比 |
| `ECAS_OFFSET_CONC` | `0.48` | ⭐ 2026-09-26 现场标定值（加性），**不是 0.5** |
| `ECAS_OFFSET_PCT` | `0.3` | 乘性偏移量，**只有 `ECAS_OFFSET_MODE=mul` 时参与计算**（加性模式下不使用） |
| `ECAS_BREAK_LOW` / `ECAS_OVER_HIGH` | 3600 / 20500 | 断线（<3.6mA）/ 超量程（>20.5mA）判据 |
| `ECAS_ALARM_LOW` / `ECAS_ALARM_HIGH` | 40 / 60 | 出厂默认报警限，可在看板上改（落库 `alarm_config`） |
| `ECAS_ALARM_SCAN` / `ECAS_ALARM_SCAN_SECONDS` | `1` / `60` | 后台周期扫描开关与周期 |
| `ECAS_STALE_SECONDS` | 600 | 多久没数据算「长时间未更新」 |
| `ECAS_EXPORT_ROWS_MAX` | 50000 | CSV 导出行数上限（**超限报 400，不再静默截断**；720h×100s≈26k 行，50000 保证可选区间都导得全） |
| `ECAS_CORS_ORIGINS` | `["http://127.0.0.1:5173","http://localhost:5173"]` | 前端 dev server 跨域白名单（生产同源托管不需要） |
| `ECAS_AUTH_COOKIE` | `1` | 浏览器侧把令牌同时写 Cookie |

## 接口

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| GET | `/healthz` | 否 | 存活探针（纯文本 `ok`），部署脚本靠它自检 |
| GET | `/api/auth` | 否 | 是否需要令牌 |
| GET | `/api/params` | 是 | 量程/补偿/阈值/报警限（前端页脚口径也取这里） |
| GET | `/api/health` | 是 | 数据新鲜度、断线/超量程状态 |
| GET | `/api/readings` | 是 | 原始值分页（`raw` + 后端换算结果）。`hours`/`start`+`end`、`limit`(1..5000)、`offset`、`sample`：`none`（默认）＝最新 `limit` 条（分页/表格用）；`auto`＝窗口行数超过 `limit` 时对**整个窗口**均匀抽样（每桶取时间居中的真实行，不插值），响应带 `rows_total`/`sampled`/`bucket_seconds` |
| GET | `/api/summary` | 是 | 最新值/KPI 汇总 |
| GET | `/api/export.csv` | 是 | 导出 CSV |
| GET | `/api/shifts` | 是 | 班次清单预览（白班 08:00–20:00 / 夜班 20:00–次日 08:00；只算时间、不查数据） |
| GET | `/api/export/shifts` | 是 | 按班次导出 Excel（单班 `.xlsx`／多班 `.zip`，内嵌 matplotlib 浓度趋势图） |
| GET | `/api/alarms` | 是 | 报警事件分页 |
| PUT/DELETE | `/api/alarms/config` | 是 | 改报警上下限 / 恢复默认 |
| POST | `/api/alarms/scan` | 是 | 手动触发一次扫描 |
| WS | `/ws/readings` | 是 | 有新行就推一条（默认 5 秒轮询数据库）。浏览器不能给 WS 带请求头，令牌只能走 `?token=` |
| — | `/internal/accounts*` | 是（**内部令牌**） | 账户接口（建号/改密/导入/校验/启停），用 `ECAS_INTERNAL_TOKEN`，**与看板令牌分开**（安全审查 M-02） |
| GET | `/` | 否 | 前端静态产物（`dist/` 存在时挂载） |

交互式文档 `/docs`、`/redoc`、`/openapi.json` **默认关闭**（安全审查 M-01）；
临时要看得设 `ECAS_ENABLE_DOCS=1`。

## 数据

库里**只存型号/时间/原始值**（2026-09-26 定案）：换算、补偿、断线/超量程判断全在后端，
所以改补偿不用动历史数据。

```sql
-- scripts/485/sql/001_init.sql
raw_reading(model text, ts timestamptz, raw integer CHECK (raw BETWEEN 0 AND 65535),
            PRIMARY KEY (model, ts, raw))   -- 幂等，可重复执行
```

告警两张表（`alarm_config` / `alarm_event`）由后端启动时自建，见 `app/alarms.py`。

## 安全约定

* 令牌只从 `ECAS_AUTH_TOKEN` 注入，**不写进仓库任何文件**；容器里由 compose secret / 环境注入，
  取用见 `web/scripts/ecas-dashboard.sh`（读 PID 1 的 `/proc/1/environ` 后只放进程内存，不落盘）；
* 账户接口另用 `ECAS_INTERNAL_TOKEN`（M-02）；`/docs` 默认关闭（M-01）；
  **无令牌时是 fail-closed**（`/api/*` 503 + 容器拒不启动），不是裸奔（M-03）；
* 生产端数据库走 scram-sha-256 + `PGPASSFILE`，DSN/命令行里不放明文口令；
* 只监听内网/mesh 地址时也要设令牌 —— 现网只绑 `10.10.10.11:18080`（easytier mesh 内网），
  但令牌校验是开着的。
