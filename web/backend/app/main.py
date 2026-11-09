"""ECAS 本地开发后端（FastAPI）。

接口一览：
    GET  /healthz                        存活探针（纯文本 ok，**不需要令牌**）
    GET  /api/auth                       是否启用了令牌校验（**不需要令牌**）
    GET  /api/params                     当前换算口径（量程/补偿/阈值/公式/生效的报警上下限）
    GET  /api/health                     行数、最新时间、滞后秒数、是否断流
    GET  /api/readings?hours&limit&offset  区间读数（含换算结果），按时间倒序
    GET  /api/summary?hours              区间统计（均值/极值只算有效区间；含越限行数）
    GET  /api/flow/params                流量计口径（FMC400 寄存器表/单位/状态判据）
    GET  /api/flow/latest                流量计最新一条（含新鲜度；库里没行时 available=false）
    GET  /api/flow/readings?hours&...    流量计区间读数（14 个寄存器原样 + 解码结果）
    GET  /api/flow/summary?hours         流量计区间统计（流量均值/极值、累积量增量）
    GET  /api/export.csv?hours&limit     导出原始值 + 换算结果（CSV，带 BOM 与中文口径注释头）
    GET  /api/export.xlsx?hours&limit    导出浓度结果（Excel，**不含**仪表原始计数）
    GET  /api/export/flow.csv?hours&limit  导出流量工程量（CSV）
    GET  /api/export/flow.xlsx?hours&limit 导出流量工程量（Excel）
    GET  /api/shifts?start&end&kinds     列出区间内的班次（导出前预览，不查数据）
    GET  /api/export/shifts?start&end&kinds
                                         按班次导出 Excel（单班 xlsx／多班 zip，内嵌趋势图）
    GET  /api/export/window?start&end&detail
                                         按自定义时段导出 Excel（起止精确到分钟，可跨班次）
    GET  /api/alarms?hours&limit         报警事件台账（含进行中的）

    PUT  /api/alarms/config?low&high     自定义报警上下限（落库，事件/统计/画线一起生效）
    DELETE /api/alarms/config            恢复默认上下限
    POST /api/alarms/scan                立即扫描一次（不等后台周期）
    WS   /ws/readings                    有新的行就推一条（默认 5 秒轮询数据库）

**时间窗**（`/api/readings`、`/api/summary`、`/api/export.csv`、`/api/alarms` 四者同源）：
默认按「最近 `hours` 小时」滑动；也可以给 `start`/`end`（ISO 8601，可带偏移或 `Z`）看**指定时段**
——两端都给就是绝对窗口，不会再随当前时间漂走；自定义区间最长 720 小时。

鉴权（见 `app/auth.py`）：设了 `ECAS_AUTH_TOKEN` 就对 `/api/*` 与 `/ws/readings` 生效，
令牌可放 `Authorization: Bearer <t>`、`?token=<t>` 或 Cookie `ecas_token`；
**没设令牌 = fail-closed**（`/api/*` 一律 503、容器入口拒绝启动），只有显式
`ECAS_ALLOW_NO_AUTH=1` 的本机开发才放行（2026-09-28 安全审查 M-03）。
账户内部接口 `/internal/accounts*` 另有专用令牌 `ECAS_INTERNAL_TOKEN`（M-02）；
`/docs`·`/redoc`·`/openapi.json` 默认**关闭**（M-01），临时要看设 `ECAS_ENABLE_DOCS=1`。

设计约束（与项目分层约定一致）：
* 换算口径只有一处实现（`app/convert.py`），接口只输出结果，前端不重算；
* `raw_reading` 后端**只读**，写入由采集端（DTU → mosquitto → monitor）负责；
  后端唯一的写库动作是报警引擎自己的两张表（`alarm_event` / `alarm_config`，见 `app/alarms.py`）；
* hours 夹在 1..720、limit 夹在 1..5000（越界夹住而不报错，沿用云端脚本行为）。

启动：`uv run uvicorn app.main:app --host 127.0.0.1 --port 8080`
"""

from __future__ import annotations

import asyncio
import logging
import re
import zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool

from . import accounts, alarms, auth, convert, db, export_flow, export_xlsx, flow, report, schemas, shifts
from .settings import Settings, get_settings

log = logging.getLogger("ecas.backend")

#: 时间窗上限：预设 30 天；自定义起止也夹在同一个上限（超过报 400 而不是悄悄改窗口）
MAX_WINDOW_HOURS = 720

SQL_HEALTH = "SELECT count(*)::int AS rows_total, max(ts) AS last_ts FROM raw_reading"
SQL_LAST = "SELECT ts, model, raw FROM raw_reading ORDER BY ts DESC LIMIT 1"
SQL_READINGS = """
SELECT ts, model, raw
  FROM raw_reading
 WHERE ts >= %(start)s AND ts < %(end)s
 ORDER BY ts DESC
 LIMIT %(limit)s OFFSET %(offset)s
"""
#: 窗口内总行数：`sample=auto` 先数一次，行数不超过 limit 就直接给完整数据（不抽样）
SQL_READINGS_COUNT = """
SELECT count(*)::int AS n
  FROM raw_reading
 WHERE ts >= %(start)s AND ts < %(end)s
"""
#: **整窗均匀抽样**（`sample=auto`）：把窗口等分成 limit 个时间桶，每桶取**时间居中的
#: 那条真实读数**（不插值、不平均 ⇒ 返回的每一点都对应库里一行，`raw` 可直接判定
#: 断线/超量程）。`width_bucket` 对 [lo, hi) 给出 1..n 的桶号，只用于分组与取中。
SQL_READINGS_SAMPLED = """
WITH win AS (
    SELECT ts, model, raw,
           width_bucket(extract(epoch FROM ts), %(start_epoch)s, %(end_epoch)s, %(limit)s) AS bucket
      FROM raw_reading
     WHERE ts >= %(start)s AND ts < %(end)s
), ranked AS (
    SELECT ts, model, raw,
           row_number() OVER (PARTITION BY bucket ORDER BY ts) AS rn,
           count(*)     OVER (PARTITION BY bucket)            AS n
      FROM win
)
SELECT ts, model, raw
  FROM ranked
 WHERE rn = (n + 1) / 2
 ORDER BY ts DESC
"""
SQL_SUMMARY = """
SELECT count(*)::int                                                   AS n,
       count(*) FILTER (WHERE raw BETWEEN %(lo)s AND %(hi)s)::int       AS n_valid,
       count(*) FILTER (WHERE raw < 3600)::int                          AS broken,
       count(*) FILTER (WHERE raw >= 3600 AND raw < 4000)::int          AS low,
       count(*) FILTER (WHERE raw > %(hi)s)::int                        AS over,
       count(*) FILTER (WHERE raw BETWEEN %(lo)s AND %(hi)s
                          AND raw < %(alarm_lo)s)::int                  AS n_below_alarm,
       count(*) FILTER (WHERE raw BETWEEN %(lo)s AND %(hi)s
                          AND raw > %(alarm_hi)s)::int                  AS n_above_alarm,
       min(raw) FILTER (WHERE raw BETWEEN %(lo)s AND %(hi)s)            AS raw_min,
       max(raw) FILTER (WHERE raw BETWEEN %(lo)s AND %(hi)s)            AS raw_max,
       avg(raw) FILTER (WHERE raw BETWEEN %(lo)s AND %(hi)s)            AS raw_avg
  FROM raw_reading
 WHERE ts >= %(start)s AND ts < %(end)s
"""

# ---------------------------------------------------------------- 流量计（FMC400）
#
# 流量计一次应答 14 个寄存器（0~13），`flow_reading.regs` 原样存；解码在 `app/flow.py`。
# 下面的聚合 SQL 里要按寄存器位拼 32 位（PostgreSQL 数组是 **1 基**：regs[1] 就是地址 0），
# 所以这类表达式只在这里出现一次 —— 换成后端算会把整窗 8 万多行拉进内存。

SQL_FLOW_TOTAL = "SELECT count(*)::int AS n FROM flow_reading WHERE model = %(model)s"
SQL_FLOW_LATEST = """
SELECT ts, model, regs
  FROM flow_reading
 WHERE model = %(model)s
 ORDER BY ts DESC
 LIMIT 1
"""
SQL_FLOW_COUNT = """
SELECT count(*)::int AS n
  FROM flow_reading
 WHERE model = %(model)s AND ts >= %(start)s AND ts < %(end)s
"""
SQL_FLOW_READINGS = """
SELECT ts, model, regs
  FROM flow_reading
 WHERE model = %(model)s AND ts >= %(start)s AND ts < %(end)s
 ORDER BY ts DESC
 LIMIT %(limit)s OFFSET %(offset)s
"""
#: 与 `SQL_READINGS_SAMPLED` 同一套「整窗均匀 + 只取真实行」的做法，只是换了表
SQL_FLOW_SAMPLED = """
WITH win AS (
    SELECT ts, model, regs,
           width_bucket(extract(epoch FROM ts), %(start_epoch)s, %(end_epoch)s, %(limit)s) AS bucket
      FROM flow_reading
     WHERE model = %(model)s AND ts >= %(start)s AND ts < %(end)s
), ranked AS (
    SELECT ts, model, regs,
           row_number() OVER (PARTITION BY bucket ORDER BY ts) AS rn,
           count(*)     OVER (PARTITION BY bucket)            AS n
      FROM win
)
SELECT ts, model, regs
  FROM ranked
 WHERE rn = (n + 1) / 2
 ORDER BY ts DESC
"""
#: 流量计聚合：`flow_raw`/`vel_raw` 按有符号 32 位拼（与 `flow._s32` 一字不差）；
#: `total_*` 取窗口首/末的累积量，回绕/清零由接口层判断（差值为负就不给增量）。
SQL_FLOW_SUMMARY = """
WITH f AS (
    SELECT ts,
           (CASE WHEN regs[3] >= 32768 THEN regs[3] - 65536 ELSE regs[3] END)::bigint * 65536
           + regs[4]                                     AS flow_raw,
           (CASE WHEN regs[1] >= 32768 THEN regs[1] - 65536 ELSE regs[1] END)::bigint * 65536
           + regs[2]                                     AS vel_raw,
           regs[5]                                       AS pct,   -- 1-based 第 5 个 = r4 百分比
           regs[6]                                       AS empty_pct,
           regs[13]                                      AS alarm,
           regs[7]::bigint * 65536 + regs[8]             AS total_raw
      FROM flow_reading
     WHERE model = %(model)s AND ts >= %(start)s AND ts < %(end)s
)
SELECT count(*)::int                                             AS n,
       count(*) FILTER (WHERE empty_pct >= 5000)::int            AS n_empty_pipe,
       count(*) FILTER (WHERE flow_raw < 0)::int                 AS n_reverse,
       count(*) FILTER (WHERE alarm <> 0)::int                   AS n_alarm,
       avg(flow_raw) / 100.0                                     AS flow_avg,
       min(flow_raw) / 100.0                                     AS flow_min,
       max(flow_raw) / 100.0                                     AS flow_max,
       avg(vel_raw) / 1000.0                                     AS velocity_avg,
       max(vel_raw) / 1000.0                                     AS velocity_max,
       avg(empty_pct) / 100.0                                    AS empty_pipe_avg,
       avg(pct) / 100.0                                          AS percent_avg,
       (array_agg(total_raw ORDER BY ts))[1]                     AS total_first,
       (array_agg(total_raw ORDER BY ts DESC))[1]                AS total_last
  FROM f
"""

#: 流量明细导出（班次报表的「流量明细」表用）：按时间正序，只取所需列
SQL_FLOW_EXPORT = """
SELECT ts, model, regs
  FROM flow_reading
 WHERE model = %(model)s AND ts >= %(start)s AND ts < %(end)s
 ORDER BY ts ASC
 LIMIT %(limit)s
"""

#: 导出用：与 /api/readings 同源，但**按时间正序**，且换行/列的组装只在后端做一次
SQL_EXPORT = """
SELECT ts, model, raw
  FROM raw_reading
 WHERE ts >= %(start)s AND ts < %(end)s
 ORDER BY ts ASC
 LIMIT %(limit)s
"""

CSV_COLUMNS = ("型号", "时间", "原始值", "电流(mA)", "未补偿浓度", "浓度", "状态")

#: 前端构建产物（`pnpm build` 之后）挂到根路径；没构建时根路径给出提示
DIST_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def parse_ts(value: str) -> datetime:
    """ISO 8601 → 带时区的 datetime；不带时区的按**服务器本地时间**解释。

    前端 `datetime-local` 输入框给的是本地时间，浏览器侧会先转成带偏移的 ISO 再发过来，
    所以只有手工调接口时才会走到"不带时区"这条兜底路径。

    容错：查询串里没转义的 `+08:00` 会被解成空格（`+` → ` `），
    `2026-09-27T08:00 08:00` 这种也照样认（`curl` 手敲常见）。
    """
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.fromisoformat(text.replace(" ", "+"))
        except ValueError as exc:
            raise ValueError(f"{value!r} 不是 ISO 8601 时间") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def resolve_window(hours: int, start: str | None = None, end: str | None = None) -> tuple[datetime, datetime, int]:
    """把「最近 N 小时」或「自定义起止」统一成**左闭右开**的时间窗。

    * 只给 `hours`（看板预设）⇒ 终点＝现在，窗口随时间滑动；
    * 给了 `start`/`end`（看板自定义时段）⇒ 用绝对值 —— 「看昨天 8 点到 12 点」不能在刷新后漂走；
    * 返回 `(start_ts, end_ts, hours)`，`hours` 供界面显示用（自定义时是实际跨度，取整）。
    """
    if start or end:
        now = datetime.now().astimezone()
        try:
            end_ts = parse_ts(end) if end else now
            start_ts = parse_ts(start) if start else end_ts - timedelta(hours=clamp(hours, 1, MAX_WINDOW_HOURS))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"时间参数无法解析（{exc}）") from exc
        if start_ts >= end_ts:
            raise HTTPException(status_code=400, detail="起点必须早于终点")
        span = end_ts - start_ts
        if span > timedelta(hours=MAX_WINDOW_HOURS):
            raise HTTPException(
                status_code=400,
                detail=f"自定义区间最长 {MAX_WINDOW_HOURS} 小时（30 天），当前 {span.total_seconds() / 3600:.1f} 小时",
            )
        return start_ts, end_ts, max(1, round(span.total_seconds() / 3600))
    hours = clamp(hours, 1, MAX_WINDOW_HOURS)
    now = datetime.now().astimezone()
    return now - timedelta(hours=hours), now, hours


async def alarm_loop(app: FastAPI, stop: asyncio.Event) -> None:
    """后台周期性扫描：把异常对账成 alarm_event（唯一的写库动作，见 app/alarms.py）。"""
    settings: Settings = app.state.settings
    pool: ConnectionPool = app.state.pool
    while not stop.is_set():
        try:
            result = await asyncio.to_thread(alarms.evaluate, pool, settings)
            app.state.last_scan = result
        except Exception as exc:  # 扫描失败不能影响看板（数据库重启、表被锁…）
            log.warning("报警扫描失败：%s", exc)
            app.state.last_scan = {"error": str(exc)}
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.alarm_scan_seconds)
        except asyncio.TimeoutError:
            continue


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    pool = db.make_pool(settings.dsn)
    pool.open()
    app.state.pool = pool
    app.state.settings = settings
    app.state.last_scan = None
    log.info("ECAS 后端启动：http://%s:%s（DSN 不含口令）", settings.host, settings.port)
    if settings.auth_required:
        log.info("访问令牌校验：已启用（%d 位，来源 ECAS_AUTH_TOKEN）", len(settings.auth_token))
    elif settings.allow_no_auth:
        log.warning(
            "访问令牌校验：**未启用**，但 ECAS_ALLOW_NO_AUTH=1 ⇒ 放行 —— 只允许本机开发，发布前必须撤掉"
        )
    else:
        log.error(
            "访问令牌校验：**未启用**（ECAS_AUTH_TOKEN 为空）⇒ 按 fail-closed，所有 /api/* 一律 503。"
            "本机开发请显式设 ECAS_ALLOW_NO_AUTH=1"
        )
    if settings.internal_required:
        log.info(
            "内部账户接口令牌：%s",
            "用 ECAS_INTERNAL_TOKEN（与对外令牌分离）" if settings.internal_token else "回退 ECAS_AUTH_TOKEN（过渡期，建议尽快分离）",
        )
    else:
        log.warning("内部账户接口：未配置任何令牌 ⇒ /internal/accounts* 一律 503")
    if not settings.enable_docs:
        log.info("接口文档：已关闭（/docs、/redoc、/openapi.json 均 404）")
    try:
        await asyncio.to_thread(alarms.ensure_schema, pool)
    except Exception as exc:
        log.warning("报警表建立失败（不影响看板读取）：%s", exc)
    try:
        await asyncio.to_thread(accounts.ensure_schema, pool)
    except Exception as exc:
        log.warning("账户表建立失败（网页登录将不可用）：%s", exc)
    stop = asyncio.Event()
    task: asyncio.Task | None = None
    if settings.alarm_scan:
        log.info("报警引擎：已启用，每 %d 秒扫描一次（ECAS_ALARM_SCAN=0 可关）", settings.alarm_scan_seconds)
        task = asyncio.create_task(alarm_loop(app, stop))
    else:
        log.info("报警引擎：后台扫描已关闭，只能 POST /api/alarms/scan 手动扫")
    try:
        yield
    finally:
        stop.set()
        if task is not None:
            task.cancel()
        pool.close()
        log.info("ECAS 后端退出，连接池已关闭")


def _docs_kwargs(settings: Settings) -> dict[str, str | None]:
    """安全审查 M-01：默认**不**暴露 `/docs`、`/redoc`、`/openapi.json`。

    接口清单是攻击者最省事的地图（`/openapi.json` 连 `/internal/*` 的参数都写清楚）。
    真有需要（本机调试）再 `ECAS_ENABLE_DOCS=1` 打开。
    """
    if settings.enable_docs:
        return {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


app = FastAPI(
    title="ECAS 后端",
    version="0.1.0",
    lifespan=lifespan,
    **_docs_kwargs(get_settings()),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

#: 账户内部 API（`/internal/accounts*`，只有网关 Worker 凭 `ECAS_INTERNAL_TOKEN` 能调，
#: 没配就回退 `ECAS_AUTH_TOKEN`）。
#: ⚠️ 必须在下面 `app.mount("/", StaticFiles...)` **之前**注册，否则会被静态站挂载吞掉。
app.include_router(accounts.router)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/api/auth", response_model=schemas.AuthInfo)
def api_auth(request: Request) -> schemas.AuthInfo:
    """值守门状态（本身不需要令牌）：前端据此决定要不要弹输入框。"""
    return schemas.AuthInfo(auth_required=bool(request.app.state.settings.auth_required))


@app.get("/api/params", response_model=schemas.Params, dependencies=[Depends(auth.require_auth)])
def api_params(request: Request) -> schemas.Params:
    """当前口径 + **生效的**报警上下限（库里的自定义值优先于环境变量默认值）。"""
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    alarms_now = alarms.effective_alarms(pool, settings)
    return schemas.Params(**convert.params(settings, alarms_now["low"], alarms_now["high"]))


@app.get("/api/health", response_model=schemas.Health, dependencies=[Depends(auth.require_auth)])
def api_health(request: Request) -> schemas.Health:
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    head = db.fetch_one(pool, SQL_HEALTH) or {"rows_total": 0, "last_ts": None}
    last = db.fetch_one(pool, SQL_LAST)
    last_ts: datetime | None = head.get("last_ts")
    age = (datetime.now(timezone.utc) - last_ts).total_seconds() if last_ts else None
    return schemas.Health(
        rows_total=int(head.get("rows_total") or 0),
        last_ts=last_ts,
        age_seconds=round(age, 1) if age is not None else None,
        stale=age is None or age > settings.stale_seconds,
        last=schemas.to_reading(last, settings) if last else None,
    )


@app.get("/api/readings", response_model=schemas.ReadingsPage, dependencies=[Depends(auth.require_auth)])
def api_readings(
    request: Request,
    hours: int = Query(24, description="回看小时数，夹在 1..720（给了 start/end 时只当兜底）"),
    limit: int = Query(200, description="条数上限，夹在 1..5000"),
    offset: int = Query(0, description="偏移，负数按 0；触发抽样时忽略（抽样已把整窗压成 limit 条）"),
    sample: str = Query(
        "none",
        description="none=原样返回最新 limit 条（分页用）；auto=窗口行数超过 limit 时对整个窗口均匀抽样",
    ),
    start: str | None = Query(None, description="自定义起点，ISO 8601；与 end 一起用时为绝对窗口"),
    end: str | None = Query(None, description="自定义终点，ISO 8601；缺省=现在"),
) -> schemas.ReadingsPage:
    """区间原始值 + 后端换算结果。

    ⚠️ 默认（`sample=none`）语义是「**取最新 limit 条**」：窗口比 limit 长时只返回尾部。
    图表若直接拿它当整窗数据，就会出现「选 30 天只画 3 天」（2026-10-09 修的就是这个）。
    要「整段窗口都得看一眼」的调用方（趋势图）传 `sample=auto`：先数窗口行数，
    超过 limit 就把窗口等分成 limit 个桶、每桶取时间居中那条**真实**读数 ⇒ 返回的行
    **均匀覆盖整个区间**，代价是长窗口分辨率下降（响应里 `rows_total` / `sampled` /
    `bucket_seconds` 说明总共多少行、有没有抽样、一个点代表多长时间）。
    """
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    limit, offset = clamp(limit, 1, 5000), max(0, offset)
    start_ts, end_ts, hours = resolve_window(hours, start, end)
    rows_total = int(
        (db.fetch_one(pool, SQL_READINGS_COUNT, {"start": start_ts, "end": end_ts}) or {}).get("n") or 0
    )
    sampled = sample == "auto" and rows_total > limit
    if sampled:
        rows = db.fetch_all(
            pool,
            SQL_READINGS_SAMPLED,
            {
                "start": start_ts,
                "end": end_ts,
                "limit": limit,
                "start_epoch": start_ts.timestamp(),
                "end_epoch": end_ts.timestamp(),
            },
        )
        offset = 0
    else:
        rows = db.fetch_all(
            pool, SQL_READINGS, {"start": start_ts, "end": end_ts, "limit": limit, "offset": offset}
        )
    return schemas.ReadingsPage(
        hours=hours,
        start=start_ts,
        end=end_ts,
        limit=limit,
        offset=offset,
        count=len(rows),
        rows_total=rows_total,
        sampled=sampled,
        bucket_seconds=((end_ts - start_ts).total_seconds() / limit) if sampled else None,
        items=[schemas.to_reading(row, settings) for row in rows],
    )


@app.get("/api/summary", response_model=schemas.Summary, dependencies=[Depends(auth.require_auth)])
def api_summary(
    request: Request,
    hours: int = Query(24, description="回看小时数，夹在 1..720（给了 start/end 时只当兜底）"),
    start: str | None = Query(None, description="自定义起点，ISO 8601"),
    end: str | None = Query(None, description="自定义终点，ISO 8601；缺省=现在"),
) -> schemas.Summary:
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    start_ts, end_ts, hours = resolve_window(hours, start, end)
    alarms_now = alarms.effective_alarms(pool, settings)
    row = db.fetch_one(
        pool,
        SQL_SUMMARY,
        {
            "start": start_ts,
            "end": end_ts,
            "lo": int(settings.i_lo * 1000),
            "hi": settings.over_high,
            "alarm_lo": alarms_now["low_raw"],
            "alarm_hi": alarms_now["high_raw"],
        },
    ) or {}
    return schemas.Summary(
        hours=hours,
        start=start_ts,
        end=end_ts,
        n=int(row.get("n") or 0),
        n_valid=int(row.get("n_valid") or 0),
        broken=int(row.get("broken") or 0),
        low=int(row.get("low") or 0),
        over=int(row.get("over") or 0),
        n_below_alarm=int(row.get("n_below_alarm") or 0),
        n_above_alarm=int(row.get("n_above_alarm") or 0),
        conc_avg=convert.raw_to_conc(row["raw_avg"], settings) if row.get("raw_avg") is not None else None,
        conc_min=convert.raw_to_conc(row["raw_min"], settings) if row.get("raw_min") is not None else None,
        conc_max=convert.raw_to_conc(row["raw_max"], settings) if row.get("raw_max") is not None else None,
        raw_min=row.get("raw_min"),
        raw_max=row.get("raw_max"),
    )


# ---------------------------------------------------------------- 流量计（FMC400）


@app.get("/api/flow/params", response_model=schemas.FlowParams, dependencies=[Depends(auth.require_auth)])
def api_flow_params(request: Request) -> schemas.FlowParams:
    """流量计口径：型号、单位、寄存器表、状态判据。

    和浓度计的 `/api/params` 一个用途：单位与公式改了，前端不用动（脚注与单位都读这里）。
    """
    return schemas.FlowParams(**flow.params(request.app.state.settings))


@app.get("/api/flow/latest", response_model=schemas.FlowLatest, dependencies=[Depends(auth.require_auth)])
def api_flow_latest(request: Request) -> schemas.FlowLatest:
    """最新一条流量读数 + 新鲜度。

    ⚠️ 库里没有流量行时返回 `available=false`，**不是** 404：「还没接入/设备离线」是
    正常状态，看板要能照常打开并显示空态；404 会让前端分不清「没数据」和「后端坏了」。
    """
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    row = db.fetch_one(pool, SQL_FLOW_LATEST, {"model": flow.MODEL})
    total = int((db.fetch_one(pool, SQL_FLOW_TOTAL, {"model": flow.MODEL}) or {}).get("n") or 0)
    if not row:
        return schemas.FlowLatest(model=flow.MODEL, available=False, rows_total=total, stale=True)
    last_ts: datetime = row["ts"]
    age = (datetime.now(timezone.utc) - last_ts).total_seconds()
    return schemas.FlowLatest(
        model=flow.MODEL,
        available=True,
        rows_total=total,
        last_ts=last_ts,
        age_seconds=round(age, 1),
        stale=age > settings.stale_seconds,
        reading=schemas.to_flow_reading(row),
    )


@app.get("/api/flow/readings", response_model=schemas.FlowPage, dependencies=[Depends(auth.require_auth)])
def api_flow_readings(
    request: Request,
    hours: int = Query(24, description="回看小时数，夹在 1..720（给了 start/end 时只当兜底）"),
    limit: int = Query(3000, description="条数上限，夹在 1..5000"),
    offset: int = Query(0, description="偏移；触发抽样时忽略（抽样已把整窗压成 limit 条）"),
    sample: str = Query(
        "none",
        description="none=最新 limit 条（分页用）；auto=窗口行数超过 limit 时对整个窗口均匀抽样",
    ),
    start: str | None = Query(None, description="自定义起点，ISO 8601"),
    end: str | None = Query(None, description="自定义终点，ISO 8601；缺省=现在"),
) -> schemas.FlowPage:
    """流量计区间读数（14 个寄存器原样 + 解码结果），语义与 `/api/readings` 完全一致。"""
    pool: ConnectionPool = request.app.state.pool
    limit, offset = clamp(limit, 1, 5000), max(0, offset)
    start_ts, end_ts, hours = resolve_window(hours, start, end)
    rows_total = int(
        (db.fetch_one(pool, SQL_FLOW_COUNT, {"model": flow.MODEL, "start": start_ts, "end": end_ts}) or {}).get("n")
        or 0
    )
    sampled = sample == "auto" and rows_total > limit
    if sampled:
        rows = db.fetch_all(
            pool,
            SQL_FLOW_SAMPLED,
            {
                "model": flow.MODEL,
                "start": start_ts,
                "end": end_ts,
                "limit": limit,
                "start_epoch": start_ts.timestamp(),
                "end_epoch": end_ts.timestamp(),
            },
        )
        offset = 0
    else:
        rows = db.fetch_all(
            pool,
            SQL_FLOW_READINGS,
            {"model": flow.MODEL, "start": start_ts, "end": end_ts, "limit": limit, "offset": offset},
        )
    return schemas.FlowPage(
        hours=hours,
        start=start_ts,
        end=end_ts,
        limit=limit,
        offset=offset,
        count=len(rows),
        rows_total=rows_total,
        sampled=sampled,
        bucket_seconds=((end_ts - start_ts).total_seconds() / limit) if sampled else None,
        items=[schemas.to_flow_reading(row) for row in rows],
    )


@app.get("/api/flow/summary", response_model=schemas.FlowSummary, dependencies=[Depends(auth.require_auth)])
def api_flow_summary(
    request: Request,
    hours: int = Query(24, description="回看小时数，夹在 1..720（给了 start/end 时只当兜底）"),
    start: str | None = Query(None, description="自定义起点，ISO 8601"),
    end: str | None = Query(None, description="自定义终点，ISO 8601；缺省=现在"),
) -> schemas.FlowSummary:
    """流量计区间统计。

    累积量增量只在「末值 ≥ 首值」时给出：32 位计数会回绕、也可能被人工清零，
    差值为负并不代表倒流，宁可不给也不能报个负数当用水量。
    """
    pool: ConnectionPool = request.app.state.pool
    start_ts, end_ts, hours = resolve_window(hours, start, end)
    row = db.fetch_one(
        pool, SQL_FLOW_SUMMARY, {"model": flow.MODEL, "start": start_ts, "end": end_ts}
    ) or {}

    def num(key: str) -> float | None:
        value = row.get(key)
        return round(float(value), 2) if value is not None else None

    first, last = row.get("total_first"), row.get("total_last")
    delta = (int(last) - int(first)) if first is not None and last is not None and last >= first else None
    return schemas.FlowSummary(
        hours=hours,
        start=start_ts,
        end=end_ts,
        n=int(row.get("n") or 0),
        n_empty_pipe=int(row.get("n_empty_pipe") or 0),
        n_reverse=int(row.get("n_reverse") or 0),
        n_alarm=int(row.get("n_alarm") or 0),
        flow_avg=num("flow_avg"),
        flow_min=num("flow_min"),
        flow_max=num("flow_max"),
        velocity_avg=num("velocity_avg"),
        velocity_max=num("velocity_max"),
        empty_pipe_avg=num("empty_pipe_avg"),
        percent_avg=num("percent_avg"),
        total_first=int(first) if first is not None else None,
        total_last=int(last) if last is not None else None,
        total_delta=delta,
    )


@app.get("/api/export.csv", dependencies=[Depends(auth.require_auth)])
def api_export_csv(
    request: Request,
    hours: int = Query(24, description="回看小时数，夹在 1..720（给了 start/end 时只当兜底）"),
    limit: int = Query(50000, description="行数上限，夹在 1..ECAS_EXPORT_ROWS_MAX"),
    start: str | None = Query(None, description="自定义起点，ISO 8601"),
    end: str | None = Query(None, description="自定义终点，ISO 8601；缺省=现在"),
) -> Response:
    """导出区间原始值 + 换算结果（CSV）。

    * 文件头带 `\\ufeff` BOM —— 否则 Excel 打开中文「断线/超量程」会是乱码；
    * 前若干行是 `#` 注释，写明**当时**的口径、补偿、报警阈值与行数，
      导出的文件自带口径说明，避免"这份 CSV 是哪个口径算的"说不清；
    * 断线/超量程行的浓度是数学外推值，注释里明确提醒按 status 列判断。
    """
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    start_ts, end_ts, hours = resolve_window(hours, start, end)
    limit = clamp(limit, 1, settings.export_rows_max)
    # 旧行为是 `ORDER BY ts ASC LIMIT`：超上限时**静默丢掉最新的行**（导出的文件看着正常，
    # 其实少了尾巴 —— 2026-10-09 与「趋势图只画 4 天」一起查出）。导出宁可报错也不静默丢数据。
    rows_total = int(
        (db.fetch_one(pool, SQL_READINGS_COUNT, {"start": start_ts, "end": end_ts}) or {}).get("n") or 0
    )
    if rows_total > limit:
        raise HTTPException(
            status_code=400,
            detail=(
                f"该区间共 {rows_total} 行，超过导出上限 {limit} 行："
                "请缩短区间后重试（导出不会静默截断）"
            ),
        )
    rows = db.fetch_all(pool, SQL_EXPORT, {"start": start_ts, "end": end_ts, "limit": limit})
    # 注释头与表头都用**生效的**阈值（和看板、报警引擎同一处来源）
    alarms_now = alarms.effective_alarms(pool, settings)
    params = convert.params(settings, alarms_now["low"], alarms_now["high"])
    now = datetime.now().astimezone()

    def stamp(value: datetime | None) -> str:
        return value.astimezone().isoformat(timespec="seconds") if value else "无数据"

    lines = [
        "# ECAS 浓度计原始值导出",
        f"# 型号: {settings.model_name}",
        f"# 区间: {stamp(start_ts)} ~ {stamp(end_ts)}（{hours} 小时；数据实际覆盖 "
        f"{stamp(rows[0]['ts']) if rows else '无数据'} ~ {stamp(rows[-1]['ts']) if rows else '无数据'}）",
        f"# 生成时间: {now.isoformat(timespec='seconds')} ｜ 行数: {len(rows)}",
        f"# 口径: {params['formula']['ma']} ｜ conc_raw = {params['formula']['conc_raw']}"
        f" ｜ conc = {params['formula']['conc']}",
        f"# 补偿: offset_mode={settings.offset_mode} offset_conc={settings.offset_conc}"
        f" offset_pct={settings.offset_pct}",
        f"# 浓度报警: 下限 {alarms_now['low']}（raw < {alarms_now['low_raw']}）"
        f" ｜ 上限 {alarms_now['high']}（raw > {alarms_now['high_raw']}）"
        + ("（自定义值）" if alarms_now["custom"] else "（出厂默认值）"),
        f"# 异常阈值: 断线 raw < {settings.break_low} ｜ 偏低 {settings.break_low}~{int(settings.i_lo * 1000)}"
        f" ｜ 正常 {int(settings.i_lo * 1000)}~{settings.over_high} ｜ 超量程 raw > {settings.over_high}",
        "# 注意: 断线/超量程行的 conc 是数学外推值，无物理意义 —— 请按 status 列判断",
        ",".join(CSV_COLUMNS),
    ]
    for row in rows:
        readings = convert.convert(int(row["raw"]), settings)
        lines.append(
            ",".join(
                [
                    str(row["model"]),
                    stamp(row["ts"]),
                    str(row["raw"]),
                    f"{readings['ma']:.3f}",
                    f"{readings['conc_raw']:.3f}",
                    f"{readings['conc']:.3f}",
                    readings["status"],
                ]
            )
        )

    slug = re.sub(r"[^A-Za-z0-9._-]", "_", settings.model_name) or "ecas"
    # 预设区间用「最近 Nh」（一眼知道是滑动窗口）；自定义区间把起止写进文件名（免得下个月忘了导出的是哪一段）
    span = (
        f"{start_ts:%m%d-%H%M}~{end_ts:%m%d-%H%M}"
        if (start or end)
        else f"{hours}h"
    )
    filename = f"ecas_{slug}_{span}_{now.strftime('%Y%m%d-%H%M')}.csv"
    return Response(
        content="\ufeff" + "\r\n".join(lines) + "\r\n",
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/export.xlsx", dependencies=[Depends(auth.require_auth)])
def api_export_xlsx(
    request: Request,
    hours: int = Query(24, description="回看小时数，夹在 1..720（给了 start/end 时只当兜底）"),
    limit: int = Query(50000, description="行数上限，夹在 1..ECAS_EXPORT_ROWS_MAX"),
    start: str | None = Query(None, description="自定义起点，ISO 8601"),
    end: str | None = Query(None, description="自定义终点，ISO 8601；缺省=现在"),
) -> Response:
    """导出区间浓度结果（Excel）：**不含仪表原始计数 raw**。

    与 `/api/export.csv` 共用同一套时间窗/上限/口径，只是列不同：
    时间 / 电流(mA) / 浓度(%) / 状态 / 浓度越限。给「看数据的人」用，
    所以不写站号、采集链路、寄存器这类排查细节；要 raw 请导 CSV。
    行数超上限同样报 400（不静默截断）。
    """
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    start_ts, end_ts, hours = resolve_window(hours, start, end)
    limit = clamp(limit, 1, settings.export_rows_max)
    rows_total = int(
        (db.fetch_one(pool, SQL_READINGS_COUNT, {"start": start_ts, "end": end_ts}) or {}).get("n") or 0
    )
    if rows_total > limit:
        raise HTTPException(
            status_code=400,
            detail=(
                f"该区间共 {rows_total} 行，超过导出上限 {limit} 行："
                "请缩短区间后重试（导出不会静默截断）"
            ),
        )
    rows = db.fetch_all(pool, SQL_EXPORT, {"start": start_ts, "end": end_ts, "limit": limit})
    alarms_now = alarms.effective_alarms(pool, settings)
    now = datetime.now().astimezone()
    payload = export_xlsx.build_readings_workbook(
        settings=settings,
        start=start_ts,
        end=end_ts,
        hours=hours,
        rows=rows,
        alarms_now=alarms_now,
        generated_at=now,
    )
    span = f"{start_ts:%m%d-%H%M}~{end_ts:%m%d-%H%M}" if (start or end) else f"{hours}h"
    filename = f"ecas_conc_{span}_{now.strftime('%Y%m%d-%H%M')}.xlsx"
    return Response(
        content=payload,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


#: xlsx 的 MIME（zip 的 MIME 是 application/zip）
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: 流量导出的列（与浓度导出同一个思路：只给工程量，不给寄存器/站号这类采集细节）
FLOW_CSV_COLUMNS = export_flow.HEADERS


def _flow_export_window(
    request: Request, hours: int, limit: int, start: str | None, end: str | None
) -> tuple[float, datetime, datetime, list[dict[str, Any]]]:
    """流量导出的公共前置：解析窗口 → 行数上限校验 → 取行（超限 400，不静默截断）。"""
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    start_ts, end_ts, hours = resolve_window(hours, start, end)
    limit = clamp(limit, 1, settings.export_rows_max)
    rows_total = int(
        (db.fetch_one(pool, SQL_FLOW_COUNT, {"model": flow.MODEL, "start": start_ts, "end": end_ts}) or {}).get("n")
        or 0
    )
    if rows_total > limit:
        raise HTTPException(
            status_code=400,
            detail=(
                f"该区间共 {rows_total} 行，超过导出上限 {limit} 行："
                "请缩短区间后重试（导出不会静默截断）"
            ),
        )
    rows = db.fetch_all(pool, SQL_FLOW_EXPORT, {"model": flow.MODEL, "start": start_ts, "end": end_ts, "limit": limit})
    return hours, start_ts, end_ts, rows


def _flow_span(start_ts: datetime, end_ts: datetime, hours: float, start: str | None, end: str | None) -> str:
    return f"{start_ts:%m%d-%H%M}~{end_ts:%m%d-%H%M}" if (start or end) else f"{hours:g}h"


@app.get("/api/export/flow.csv", dependencies=[Depends(auth.require_auth)])
def api_export_flow_csv(
    request: Request,
    hours: int = Query(24, description="回看小时数，夹在 1..720（给了 start/end 时只当兜底）"),
    limit: int = Query(50000, description="行数上限，夹在 1..ECAS_EXPORT_ROWS_MAX"),
    start: str | None = Query(None, description="自定义起点，ISO 8601"),
    end: str | None = Query(None, description="自定义终点，ISO 8601；缺省=现在"),
) -> Response:
    """导出区间流量工程量（CSV）：时间 / 流量 / 流速 / 正向累积 / 流向 / 状态。

    与浓度计导出一致：BOM + 注释头（区间、单位、判据），行数超上限报 400 而不是静默截断。
    """
    settings: Settings = request.app.state.settings
    hours, start_ts, end_ts, raw_rows = _flow_export_window(request, hours, limit, start, end)
    items = export_flow.decode_rows(raw_rows)
    now = datetime.now().astimezone()

    def stamp(value: datetime) -> str:
        return value.astimezone().isoformat(timespec="seconds")

    lines = [
        f"# ECAS 流量计导出（{flow.MODEL}）",
        f"# 区间: {stamp(start_ts)} ~ {stamp(end_ts)}（{hours:g} 小时）",
        f"# 生成时间: {now.isoformat(timespec='seconds')} ｜ 行数: {len(items)}",
        "# 单位: 流量 m³/h ｜ 流速 m/s ｜ 正向累积 m³（32 位计数，会回绕）",
        "# 说明: 正向累积是仪表累计值；区间净增量请用 /api/flow/summary（回绕/清零时不给）",
        ",".join(FLOW_CSV_COLUMNS),
    ]
    for item in items:
        lines.append(
            ",".join(
                [
                    stamp(item["ts"]),
                    f"{item['flow_m3h']:.2f}",
                    f"{item['velocity_mps']:.3f}",
                    str(item["total_m3"]),
                    item["direction_label"],
                    item["status"],
                ]
            )
        )

    span = _flow_span(start_ts, end_ts, hours, start, end)
    filename = f"ecas_flow_{span}_{now.strftime('%Y%m%d-%H%M')}.csv"
    return Response(
        content="\ufeff" + "\r\n".join(lines) + "\r\n",
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/export/flow.xlsx", dependencies=[Depends(auth.require_auth)])
def api_export_flow_xlsx(
    request: Request,
    hours: int = Query(24, description="回看小时数，夹在 1..720（给了 start/end 时只当兜底）"),
    limit: int = Query(50000, description="行数上限，夹在 1..ECAS_EXPORT_ROWS_MAX"),
    start: str | None = Query(None, description="自定义起点，ISO 8601"),
    end: str | None = Query(None, description="自定义终点，ISO 8601；缺省=现在"),
) -> Response:
    """导出区间流量工程量（Excel）：时间 / 流量(m³/h) / 流速(m/s) / 正向累积(m³) / 流向 / 状态。"""
    settings: Settings = request.app.state.settings
    hours, start_ts, end_ts, raw_rows = _flow_export_window(request, hours, limit, start, end)
    now = datetime.now().astimezone()
    payload = export_flow.build_flow_workbook(
        start=start_ts,
        end=end_ts,
        hours=hours,
        rows=raw_rows,
        tz=settings.display_tz,
        generated_at=now,
    )
    span = _flow_span(start_ts, end_ts, hours, start, end)
    filename = f"ecas_flow_{span}_{now.strftime('%Y%m%d-%H%M')}.xlsx"
    return Response(
        content=payload,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )




def parse_day(value: str, field: str) -> date:
    """`YYYY-MM-DD` → date；顺带容忍完整的 ISO 时间串（只取日期部分）。

    班次是按「天」切的，所以这里只认日期。用户把 `2026-09-25T08:00` 粘进来也照样能用。
    """
    text = (value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field} 需要 YYYY-MM-DD 格式，收到 {value!r}") from exc


def resolve_shifts(settings: Settings, start: str, end: str, kinds: str) -> list[shifts.Shift]:
    """接口参数 → 班次清单；日期非法或超出上限一律 400。"""
    start_day = parse_day(start, "start")
    end_day = parse_day(end, "end")
    try:
        return shifts.validate_range(start_day, end_day, shifts.parse_kinds(kinds))
    except shifts.ShiftRangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/shifts", dependencies=[Depends(auth.require_auth)])
def api_shifts(
    request: Request,
    start: str = Query(..., description="起始日期 YYYY-MM-DD（= 班次开始的那天）"),
    end: str = Query(..., description="结束日期 YYYY-MM-DD（含当天）"),
    kinds: str = Query("day,night", description="班次种类：day,night（也可写 白班/夜班）"),
) -> dict[str, Any]:
    """列出区间内的班次清单（导出前预览：要导几个、分别是哪 12 小时），**不查数据**。

    单独一个接口是为了让界面能先说清「这次要点出去几份报表」——
    一键下去等半分钟才发现选了一个月，那体验很差。
    """
    settings: Settings = request.app.state.settings
    tz = settings.display_tz
    items = resolve_shifts(settings, start, end, kinds)
    now = datetime.now(tz)
    return {
        "count": len(items),
        "tz": settings.tz,
        "kinds": sorted({item.kind for item in items}),
        "max_shifts": shifts.MAX_SHIFTS,
        # `filename` 由接口层补：真实文件名带型号前缀（`ecas_DPRO-66_20260925-night.xlsx`），
        # 而这个词只有 settings 知道 —— shifts.py 只管时间，不该认识型号。
        # 预览报的名字必须与下载/打包出来的**逐字一致**，否则 zip 里对不上账。
        "shifts": [
            {
                **shifts.describe(item, tz),
                "filename": report.shift_filename(settings, item),
                "complete": item.is_complete(now, tz),
            }
            for item in items
        ],
    }


@app.get("/api/export/shifts", dependencies=[Depends(auth.require_auth)])
def api_export_shifts(
    request: Request,
    start: str = Query(..., description="起始日期 YYYY-MM-DD（= 班次开始的那天）"),
    end: str = Query(..., description="结束日期 YYYY-MM-DD（含当天）"),
    kinds: str = Query("day,night", description="班次种类：day,night（也可写 白班/夜班）"),
    detail: bool = Query(True, description="是否附带「读数明细」工作表（false = 只出统计/趋势/报警）"),
    brief: bool = Query(False, description="概述版：标题/时间/班次/四项指标/三张趋势图（汇报用）；勾选明细时仍附三张明细表"),
) -> Response:
    """按班次导出 Excel：一班一个工作簿，含看板上的全部信息与浓度趋势图。

    * 内容对齐看板：班末读数（≈KPI 卡）、区间统计（≈统计条）、**浓度趋势图（PNG 内嵌）**、
      报警事件表、以及逐条读数明细；
    * 单班 → 直接给 `.xlsx`；多班 → 打成一个 zip（另附一份「说明.txt」对账文件与班次）；
    * 班次边界按 `ECAS_TZ`（默认 Asia/Shanghai）切，**与服务器系统时区无关** ——
      服务器跑在 UTC 也不会把「早八点」切成 UTC 08:00；
    * 统计口径直接复用 `/api/summary` 的同一段 SQL，报表数字与网页逐一对得上。
    """
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    tz = settings.display_tz
    plan = resolve_shifts(settings, start, end, kinds)
    now = datetime.now(tz)

    alarms_now = alarms.effective_alarms(pool, settings)
    health = db.fetch_one(pool, SQL_HEALTH) or {}
    rows_total = int(health.get("rows_total") or 0)
    lo, hi = int(settings.i_lo * 1000), settings.over_high

    books: list[tuple[str, bytes]] = []
    for shift in plan:
        shift_start, shift_end = shift.bounds(tz)
        log.info("生成班次报表：%s", shift.label_long(tz))
        rows = db.fetch_all(
            pool, SQL_EXPORT, {"start": shift_start, "end": shift_end, "limit": settings.export_rows_max}
        )
        stats = db.fetch_one(
            pool,
            SQL_SUMMARY,
            {
                "start": shift_start,
                "end": shift_end,
                "lo": lo,
                "hi": hi,
                "alarm_lo": alarms_now["low_raw"],
                "alarm_hi": alarms_now["high_raw"],
            },
        ) or {}
        events = alarms.events(pool, settings, start=shift_start, end=shift_end, limit=200)
        # 流量计同期数据：班次报表里既要汇总（均值/累积增量/异常次数），也要逐行明细。
        # 表可能还没建（老库未跑 004）⇒ 捕获异常并降级成「本班无流量数据」，不影响浓度报表。
        try:
            flow_rows = db.fetch_all(
                pool,
                SQL_FLOW_EXPORT,
                {
                    "model": flow.MODEL,
                    "start": shift_start,
                    "end": shift_end,
                    "limit": settings.export_rows_max,
                },
            )
            flow_stats = db.fetch_one(
                pool, SQL_FLOW_SUMMARY, {"model": flow.MODEL, "start": shift_start, "end": shift_end}
            ) or {}
        except Exception as exc:  # noqa: BLE001 —— 报表优先，流量缺失不阻断导出
            log.warning("班次报表取流量数据失败（按无流量数据出报表）：%s", exc)
            flow_rows, flow_stats = [], {}
        books.append(
            (
                report.shift_filename(settings, shift),
                report.build_shift_workbook(
                    settings=settings,
                    shift=shift,
                    tz=tz,
                    rows=rows,
                    summary=stats,
                    events=events,
                    alarms_now=alarms_now,
                    rows_total=rows_total,
                    generated_at=now,
                    complete=shift.is_complete(now, tz),
                    flow_rows=flow_rows,
                    flow_summary=flow_stats,
                    with_detail=detail,
                    brief=brief,
                ),
            )
        )

    headers = {"Cache-Control": "no-store"}
    if len(books) == 1:
        name, payload = books[0]
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
        return Response(content=payload, media_type=XLSX_MEDIA_TYPE, headers=headers)

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, payload in books:
            bundle.writestr(name, payload)
        bundle.writestr("说明.txt", shift_bundle_readme(settings, plan, tz, now, brief=brief))
    headers["Content-Disposition"] = f'attachment; filename="{report.bundle_filename(settings, plan)}"'
    return Response(content=buf.getvalue(), media_type="application/zip", headers=headers)


@app.get("/api/export/window", dependencies=[Depends(auth.require_auth)])
def api_export_window(
    request: Request,
    start: str = Query(..., description="起始时刻（ISO 8601，精确到分钟）"),
    end: str = Query(..., description="结束时刻（ISO 8601，左闭右开）"),
    detail: bool = Query(True, description="是否附带「读数明细」工作表（false = 只出统计/趋势/报警）"),
    brief: bool = Query(False, description="概述版：标题/时间/班次/四项指标/三张趋势图（汇报用）；勾选明细时仍附三张明细表"),
) -> Response:
    """按自定义时段导出 Excel：起止精确到分钟，可跨班次、任意长短（≤30 天）。

    * 与班次报表同结构、同口径：区间统计 / 流量统计 / 综合干矿量 / 三张趋势图 / 报警事件；
    * 这就是先生 2026-11-06 定的「导出指定时间点」形态 —— 起止都精确到分钟的自定义区间；
    * 时段还没结束（end 在未来）时按导出时刻统计，并在报表里标「未结束」。
    """
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    tz = settings.display_tz
    # 复用看板的时间窗校验：左闭右开、起点早于终点、跨度 ≤ 720 小时，非法直接 400
    start_ts, end_ts, _ = resolve_window(1, start, end)
    now = datetime.now(tz)

    alarms_now = alarms.effective_alarms(pool, settings)
    health = db.fetch_one(pool, SQL_HEALTH) or {}
    rows_total = int(health.get("rows_total") or 0)
    lo, hi = int(settings.i_lo * 1000), settings.over_high

    log.info("生成自定义时段报表：%s ~ %s", start_ts, end_ts)
    rows = db.fetch_all(
        pool, SQL_EXPORT, {"start": start_ts, "end": end_ts, "limit": settings.export_rows_max}
    )
    stats = db.fetch_one(
        pool,
        SQL_SUMMARY,
        {
            "start": start_ts,
            "end": end_ts,
            "lo": lo,
            "hi": hi,
            "alarm_lo": alarms_now["low_raw"],
            "alarm_hi": alarms_now["high_raw"],
        },
    ) or {}
    events = alarms.events(pool, settings, start=start_ts, end=end_ts, limit=200)
    # 流量计同期数据：与班次导出同样处理 —— 表/采集缺失时降级，不影响浓度报表
    try:
        flow_rows = db.fetch_all(
            pool,
            SQL_FLOW_EXPORT,
            {"model": flow.MODEL, "start": start_ts, "end": end_ts, "limit": settings.export_rows_max},
        )
        flow_stats = db.fetch_one(
            pool, SQL_FLOW_SUMMARY, {"model": flow.MODEL, "start": start_ts, "end": end_ts}
        ) or {}
    except Exception as exc:  # noqa: BLE001 —— 报表优先，流量缺失不阻断导出
        log.warning("时段报表取流量数据失败（按无流量数据出报表）：%s", exc)
        flow_rows, flow_stats = [], {}

    payload = report.build_window_workbook(
        settings=settings,
        start=start_ts.astimezone(tz),
        end=end_ts.astimezone(tz),
        tz=tz,
        rows=rows,
        summary=stats,
        events=events,
        alarms_now=alarms_now,
        rows_total=rows_total,
        generated_at=now,
        complete=end_ts <= now,
        flow_rows=flow_rows,
        flow_summary=flow_stats,
        with_detail=detail,
        brief=brief,
    )
    name = report.window_filename(settings, start_ts.astimezone(tz), end_ts.astimezone(tz))
    headers = {
        "Cache-Control": "no-store",
        "Content-Disposition": f'attachment; filename="{name}"',
    }
    return Response(content=payload, media_type=XLSX_MEDIA_TYPE, headers=headers)


def shift_bundle_readme(
    settings: Settings, plan: list[shifts.Shift], tz: Any, now: datetime, *, brief: bool = False
) -> str:
    """zip 里附一份说明：哪个文件对应哪个班次、按什么口径算的。"""
    lines = [
        "ECAS 浓度计班次报表",
        f"型号：{settings.model_name}",
        f"班次数：{len(plan)}",
        f"时区：{settings.tz}（班次边界：白班 08:00–20:00，夜班 20:00–次日 08:00）",
        f"生成时间：{now:%Y-%m-%d %H:%M:%S}",
        "",
        "文件 ←→ 班次：",
    ]
    lines += [f"  {report.shift_filename(settings, shift)}  ←  {shift.label_long(tz)}" for shift in plan]
    lines += [
        "",
        "每个 xlsx 的工作表：" if not brief else "每个 xlsx 都是「概述」汇报版，主表只有一张，可按需附三张明细表：",
        *(
            [
                "  「班次报表」= 口径与阈值 + 综合汇总 + 具体干矿量 + 干矿量趋势图",
                "               + 具体浓度 + 浓度趋势图 + 具体流量 + 流量趋势图",
                "               + 区间统计 + 报警事件",
                "  「读数明细」= 本班次逐条 原始值 / 电流 / 未补偿浓度 / 浓度 / 状态（可选）",
                "  「流量明细」= 有流量数据时附：瞬时流量 / 流速 / 空管 / 累积量 / 报警（可选）",
                "  「干矿量明细」= 有浓度-流量配对时附：浓度时刻 / 配对流量时刻 / 延时 / 瞬时干矿量（可选）",
                "  （三张明细表都只在勾选「包含数据明细」时生成）",
            ]
            if not brief
            else [
                "  「概述」= 报表标题（不含型号）+ 时间/班次 + 综合干矿量 + 本班次累计",
                "           + 平均浓度 + 平均流量 + 干矿量/浓度/流量三张趋势图",
                "  「读数明细」/「流量明细」/「干矿量明细」= 勾选「包含数据明细」时附（可选）",
            ]
        ),
        "",
        "综合干矿量口径：Q = V / (1/ρp + (1−C)/C)，ρp = 4.3 t/m³；浓度只取「正常」点，",
        "与流量按 ±90 秒容差逐点配对，流量计下游 2.5 m 按实测流速补偿延时；",
        "本班累计 = 平均干矿量 × 班次时长（不用梯形积分）。",
        "",
        "注意：断线、超量程行的浓度是数学外推值，无物理意义 —— 请按状态列判断。",
        "",
    ]
    return "\r\n".join(lines)


@app.get("/api/alarms", response_model=schemas.AlarmsPage, dependencies=[Depends(auth.require_auth)])
def api_alarms(
    request: Request,
    hours: int = Query(24, description="回看小时数，夹在 1..720（给了 start/end 时只当兜底）"),
    limit: int = Query(200, description="条数上限，夹在 1..2000"),
    start: str | None = Query(None, description="自定义起点，ISO 8601"),
    end: str | None = Query(None, description="自定义终点，ISO 8601；缺省=现在"),
) -> schemas.AlarmsPage:
    """报警事件台账（含进行中的），按时间倒序。"""
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    limit = clamp(limit, 1, 2000)
    start_ts, end_ts, hours = resolve_window(hours, start, end)
    items = alarms.events(pool, settings, start=start_ts, end=end_ts, limit=limit)
    return schemas.AlarmsPage(
        hours=hours,
        start=start_ts,
        end=end_ts,
        count=len(items),
        active=sum(1 for item in items if item["active"]),
        items=[schemas.AlarmEvent(**item) for item in items],
        scan=request.app.state.last_scan,
    )


@app.put("/api/alarms/config", response_model=schemas.Alarms, dependencies=[Depends(auth.require_auth)])
def api_set_alarms(
    request: Request,
    low: float = Query(..., ge=0.0, le=100.0, description="浓度下限"),
    high: float = Query(..., ge=0.0, le=100.0, description="浓度上限"),
) -> schemas.Alarms:
    """自定义报警上下限（落库；事件判定、统计越限、图表阈值线一起生效）。"""
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    if not low < high:
        raise HTTPException(status_code=400, detail=f"下限必须小于上限（收到 {low} / {high}）")
    alarms.set_alarms(pool, settings, low, high)
    return schemas.Alarms(**alarms.effective_alarms(pool, settings))


@app.delete("/api/alarms/config", response_model=schemas.Alarms, dependencies=[Depends(auth.require_auth)])
def api_reset_alarms(request: Request) -> schemas.Alarms:
    """恢复出厂默认上下限（删掉库里的覆盖行）。"""
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    alarms.reset_alarms(pool, settings)
    return schemas.Alarms(**alarms.effective_alarms(pool, settings))


@app.post("/api/alarms/scan", dependencies=[Depends(auth.require_auth)])
def api_scan_alarms(request: Request) -> dict[str, Any]:
    """立即扫描一次（后台周期扫描之外的"现在就看看"）。"""
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    result = alarms.evaluate(pool, settings)
    request.app.state.last_scan = result
    return result


@app.websocket("/ws/readings")
async def ws_readings(websocket: WebSocket) -> None:
    """有新行就推；用最新一行的 ts 去重，不重复推同一条。握手前先验令牌。"""
    if not auth.ws_authorized(websocket):
        await websocket.close(code=1008)  # policy violation：浏览器侧表现为握手被拒
        return
    settings: Settings = websocket.app.state.settings
    pool: ConnectionPool = websocket.app.state.pool
    await websocket.accept()
    last_ts: datetime | None = None
    try:
        while True:
            row = await asyncio.to_thread(db.fetch_one, pool, SQL_LAST)
            if row and row["ts"] != last_ts:
                last_ts = row["ts"]
                await websocket.send_json(schemas.to_reading(row, settings).model_dump(mode="json"))
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        return
    except Exception as exc:  # 客户端半途消失等，别把进程带崩
        log.warning("WebSocket 结束：%s", exc)


if DIST_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(DIST_DIR), html=True), name="web")
else:

    @app.get("/")
    def index() -> dict[str, str]:
        return {
            "service": "ecas-backend",
            "hint": "前端尚未构建（web/frontend/dist 不存在）；接口文档见 /docs",
        }


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
