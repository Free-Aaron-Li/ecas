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
| `ECAS_AUTH_TOKEN` | 空 | **空 = 开发模式不校验**（只该绑 127.0.0.1）；对外必须设置 |
| `ECAS_HOST` / `ECAS_PORT` | `127.0.0.1` / `8080` | 监听地址与端口 |
| `ECAS_MODEL_NAME` | `DPRO-66` | 型号（入库/查询都按它过滤） |
| `ECAS_I_LO` `ECAS_I_HI` `ECAS_C_LO` `ECAS_C_HI` | 4 / 20 / 0 / 100 | 量程 |
| `ECAS_OFFSET_MODE` | `add` | `add`=加性偏移，`mul`=乘性百分比 |
| `ECAS_OFFSET_CONC` | `0.48` | ⭐ 2026-09-26 现场标定值（加性），**不是 0.5** |
| `ECAS_BREAK_LOW` / `ECAS_OVER_HIGH` | 3600 / 20500 | 断线（<3.6mA）/ 超量程（>20.5mA）判据 |
| `ECAS_ALARM_LOW` / `ECAS_ALARM_HIGH` | 40 / 60 | 出厂默认报警限，可在看板上改（落库 `alarm_config`） |
| `ECAS_ALARM_SCAN` / `ECAS_ALARM_SCAN_SECONDS` | `1` / `60` | 后台周期扫描开关与周期 |
| `ECAS_STALE_SECONDS` | 600 | 多久没数据算「长时间未更新」 |
| `ECAS_EXPORT_ROWS_MAX` | 20000 | CSV 导出行数上限 |
| `ECAS_CORS_ORIGINS` | `["http://127.0.0.1:5173","http://localhost:5173"]` | 前端 dev server 跨域白名单（生产同源托管不需要） |
| `ECAS_AUTH_COOKIE` | `1` | 浏览器侧把令牌同时写 Cookie |

## 接口

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| GET | `/healthz` | 否 | 存活探针（纯文本 `ok`），部署脚本靠它自检 |
| GET | `/api/auth` | 否 | 是否需要令牌 |
| GET | `/api/params` | 是 | 量程/补偿/阈值/报警限（前端页脚口径也取这里） |
| GET | `/api/health` | 是 | 数据新鲜度、断线/超量程状态 |
| GET | `/api/readings` | 是 | 原始值分页（`raw` + 后端换算结果） |
| GET | `/api/summary` | 是 | 最新值/KPI 汇总 |
| GET | `/api/export.csv` | 是 | 导出 CSV |
| GET | `/api/alarms` | 是 | 报警事件分页 |
| PUT/DELETE | `/api/alarms/config` | 是 | 改报警上下限 / 恢复默认 |
| POST | `/api/alarms/scan` | 是 | 手动触发一次扫描 |
| GET | `/` | 否 | 前端静态产物（`dist/` 存在时挂载） |

交互式文档：`/docs`（FastAPI 自带）。

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

* 令牌只从 `ECAS_AUTH_TOKEN` 注入，**不写进仓库任何文件**；
* 生产端数据库走 scram-sha-256 + `PGPASSFILE`，DSN/命令行里不放明文口令；
* 只监听内网/mesh 地址时也要设令牌（`auth_token` 为空即等于不校验）。
