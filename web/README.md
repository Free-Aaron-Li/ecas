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
| GET | `/api/summary?hours` | 区间统计（均值/极值**只算有效区间** 4000~20500） |
| WS | `/ws/readings` | 有新行就推一条（5 秒轮询数据库） |

口径只实现一次（后端），前端只展示：`浓度 = (raw − 4000)/160 + 0.3`，
断线判据 `raw < 3600`、超量程 `raw > 20500`。

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

## 五、测试

```bash
cd web/backend && uv run pytest -q        # 29 passed（含真实库冒烟，库不在则自动跳过）
cd web/frontend && pnpm build             # tsc -b 类型检查 + 构建
```

## CLion（不影响 C++ 索引）

CLion 是 C++ 工作区，`node_modules` / `.venv` 无需索引。建议在项目视图里
右键 `web/frontend/node_modules`、`web/backend/.venv` → **Mark Directory as → Excluded**
（`.idea/` 已在 `.gitignore` 中，属本地设置，不会进仓库）。
