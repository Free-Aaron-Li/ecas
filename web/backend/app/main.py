"""ECAS 本地开发后端（FastAPI）。

接口一览：
    GET  /healthz                        存活探针（纯文本 ok，**不需要令牌**）
    GET  /api/auth                       是否启用了令牌校验（**不需要令牌**）
    GET  /api/params                     当前换算口径（量程/补偿/阈值/公式）
    GET  /api/health                     行数、最新时间、滞后秒数、是否断流
    GET  /api/readings?hours&limit&offset  区间读数（含换算结果），按时间倒序
    GET  /api/summary?hours              区间统计（均值/极值只算有效区间；含越限行数）
    GET  /api/export.csv?hours&limit     导出原始值 + 换算结果（CSV，带 BOM 与口径注释头）
    WS   /ws/readings                    有新的行就推一条（默认 5 秒轮询数据库）

鉴权（见 `app/auth.py`）：设了 `ECAS_AUTH_TOKEN` 就对 `/api/*` 与 `/ws/readings` 生效，
令牌可放 `Authorization: Bearer <t>`、`?token=<t>` 或 Cookie `ecas_token`；
**没设令牌 = 开发模式（放行）**，启动日志会写明，避免"以为开了其实没开"。

设计约束（与项目分层约定一致）：
* 换算口径只有一处实现（`app/convert.py`），接口只输出结果，前端不重算；
* 后端**只读**数据库；写入由采集端（DTU → mosquitto → monitor）负责；
* hours 夹在 1..720、limit 夹在 1..5000（越界夹住而不报错，沿用云端脚本行为）。

启动：`uv run uvicorn app.main:app --host 127.0.0.1 --port 8080`
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool

from . import auth, convert, db, schemas
from .settings import Settings, get_settings

log = logging.getLogger("ecas.backend")

SQL_HEALTH = "SELECT count(*)::int AS rows_total, max(ts) AS last_ts FROM raw_reading"
SQL_LAST = "SELECT ts, model, raw FROM raw_reading ORDER BY ts DESC LIMIT 1"
SQL_READINGS = """
SELECT ts, model, raw
  FROM raw_reading
 WHERE ts >= now() - make_interval(hours => %(hours)s)
 ORDER BY ts DESC
 LIMIT %(limit)s OFFSET %(offset)s
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
 WHERE ts >= now() - make_interval(hours => %(hours)s)
"""

#: 导出用：与 /api/readings 同源，但**按时间正序**，且换行/列的组装只在后端做一次
SQL_EXPORT = """
SELECT ts, model, raw
  FROM raw_reading
 WHERE ts >= now() - make_interval(hours => %(hours)s)
 ORDER BY ts ASC
 LIMIT %(limit)s
"""

CSV_COLUMNS = ("model", "ts", "raw", "ma", "conc_raw", "conc", "status")

#: 前端构建产物（`pnpm build` 之后）挂到根路径；没构建时根路径给出提示
DIST_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    pool = db.make_pool(settings.dsn)
    pool.open()
    app.state.pool = pool
    app.state.settings = settings
    log.info("ECAS 后端启动：http://%s:%s（DSN 不含口令）", settings.host, settings.port)
    if settings.auth_required:
        log.info("访问令牌校验：已启用（%d 位，来源 ECAS_AUTH_TOKEN）", len(settings.auth_token))
    else:
        log.warning(
            "访问令牌校验：**未启用**（ECAS_AUTH_TOKEN 为空）——仅适合只监听 127.0.0.1 的开发模式，发布前必须设置"
        )
    try:
        yield
    finally:
        pool.close()
        log.info("ECAS 后端退出，连接池已关闭")


app = FastAPI(title="ECAS 后端", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/api/auth", response_model=schemas.AuthInfo)
def api_auth(request: Request) -> schemas.AuthInfo:
    """值守门状态（本身不需要令牌）：前端据此决定要不要弹输入框。"""
    return schemas.AuthInfo(auth_required=bool(request.app.state.settings.auth_required))


@app.get("/api/params", response_model=schemas.Params, dependencies=[Depends(auth.require_auth)])
def api_params(request: Request) -> schemas.Params:
    return schemas.Params(**convert.params(request.app.state.settings))


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
    hours: int = Query(24, description="回看小时数，夹在 1..720"),
    limit: int = Query(200, description="条数上限，夹在 1..5000"),
    offset: int = Query(0, description="偏移，负数按 0"),
) -> schemas.ReadingsPage:
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    hours, limit, offset = clamp(hours, 1, 720), clamp(limit, 1, 5000), max(0, offset)
    rows = db.fetch_all(pool, SQL_READINGS, {"hours": hours, "limit": limit, "offset": offset})
    return schemas.ReadingsPage(
        hours=hours,
        limit=limit,
        offset=offset,
        count=len(rows),
        items=[schemas.to_reading(row, settings) for row in rows],
    )


@app.get("/api/summary", response_model=schemas.Summary, dependencies=[Depends(auth.require_auth)])
def api_summary(request: Request, hours: int = Query(24, description="回看小时数，夹在 1..720")) -> schemas.Summary:
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    hours = clamp(hours, 1, 720)
    row = db.fetch_one(
        pool,
        SQL_SUMMARY,
        {
            "hours": hours,
            "lo": int(settings.i_lo * 1000),
            "hi": settings.over_high,
            "alarm_lo": round(convert.conc_to_raw(settings.alarm_low, settings)),
            "alarm_hi": round(convert.conc_to_raw(settings.alarm_high, settings)),
        },
    ) or {}
    return schemas.Summary(
        hours=hours,
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


@app.get("/api/export.csv", dependencies=[Depends(auth.require_auth)])
def api_export_csv(
    request: Request,
    hours: int = Query(24, description="回看小时数，夹在 1..720"),
    limit: int = Query(20000, description="行数上限，夹在 1..ECAS_EXPORT_ROWS_MAX"),
) -> Response:
    """导出区间原始值 + 换算结果（CSV）。

    * 文件头带 `\\ufeff` BOM —— 否则 Excel 打开中文「断线/超量程」会是乱码；
    * 前若干行是 `#` 注释，写明**当时**的口径、补偿、报警阈值与行数，
      导出的文件自带口径说明，避免"这份 CSV 是哪个口径算的"说不清；
    * 断线/超量程行的浓度是数学外推值，注释里明确提醒按 status 列判断。
    """
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    hours, limit = clamp(hours, 1, 720), clamp(limit, 1, settings.export_rows_max)
    rows = db.fetch_all(pool, SQL_EXPORT, {"hours": hours, "limit": limit})
    params = convert.params(settings)
    alarms = params["alarms"]
    now = datetime.now().astimezone()

    def stamp(value: datetime | None) -> str:
        return value.astimezone().isoformat(timespec="seconds") if value else "无数据"

    lines = [
        "# ECAS 浓度计原始值导出",
        f"# 型号: {settings.model_name}",
        f"# 区间: 最近 {hours} 小时（数据实际覆盖 {stamp(rows[0]['ts']) if rows else '无数据'}"
        f" ~ {stamp(rows[-1]['ts']) if rows else '无数据'}）",
        f"# 生成时间: {now.isoformat(timespec='seconds')} ｜ 行数: {len(rows)}",
        f"# 口径: {params['formula']['ma']} ｜ conc_raw = {params['formula']['conc_raw']}"
        f" ｜ conc = {params['formula']['conc']}",
        f"# 补偿: offset_mode={settings.offset_mode} offset_conc={settings.offset_conc}"
        f" offset_pct={settings.offset_pct}",
        f"# 浓度报警: 下限 {alarms['low']}（raw < {alarms['low_raw']}）"
        f" ｜ 上限 {alarms['high']}（raw > {alarms['high_raw']}）",
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
    filename = f"ecas_{slug}_{hours}h_{now.strftime('%Y%m%d-%H%M')}.csv"
    return Response(
        content="\ufeff" + "\r\n".join(lines) + "\r\n",
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


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
