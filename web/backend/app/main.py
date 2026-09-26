"""ECAS 本地开发后端（FastAPI）。

接口一览：
    GET  /healthz                        存活探针（纯文本 ok）
    GET  /api/params                     当前换算口径（量程/补偿/阈值/公式）
    GET  /api/health                     行数、最新时间、滞后秒数、是否断流
    GET  /api/readings?hours&limit&offset  区间读数（含换算结果），按时间倒序
    GET  /api/summary?hours              区间统计（均值/极值只算有效区间）
    WS   /ws/readings                    有新的行就推一条（默认 5 秒轮询数据库）

设计约束（与项目分层约定一致）：
* 换算口径只有一处实现（`app/convert.py`），接口只输出结果，前端不重算；
* 后端**只读**数据库；写入由采集端（DTU → mosquitto → monitor）负责；
* hours 夹在 1..720、limit 夹在 1..5000（越界夹住而不报错，沿用云端脚本行为）。

启动：`uv run uvicorn app.main:app --host 127.0.0.1 --port 8080`
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool

from . import convert, db, schemas
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
       min(raw) FILTER (WHERE raw BETWEEN %(lo)s AND %(hi)s)            AS raw_min,
       max(raw) FILTER (WHERE raw BETWEEN %(lo)s AND %(hi)s)            AS raw_max,
       avg(raw) FILTER (WHERE raw BETWEEN %(lo)s AND %(hi)s)            AS raw_avg
  FROM raw_reading
 WHERE ts >= now() - make_interval(hours => %(hours)s)
"""

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


@app.get("/api/params", response_model=schemas.Params)
def api_params(request: Request) -> schemas.Params:
    return schemas.Params(**convert.params(request.app.state.settings))


@app.get("/api/health", response_model=schemas.Health)
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


@app.get("/api/readings", response_model=schemas.ReadingsPage)
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


@app.get("/api/summary", response_model=schemas.Summary)
def api_summary(request: Request, hours: int = Query(24, description="回看小时数，夹在 1..720")) -> schemas.Summary:
    settings: Settings = request.app.state.settings
    pool: ConnectionPool = request.app.state.pool
    hours = clamp(hours, 1, 720)
    row = db.fetch_one(
        pool,
        SQL_SUMMARY,
        {"hours": hours, "lo": int(settings.i_lo * 1000), "hi": settings.over_high},
    ) or {}
    return schemas.Summary(
        hours=hours,
        n=int(row.get("n") or 0),
        n_valid=int(row.get("n_valid") or 0),
        broken=int(row.get("broken") or 0),
        low=int(row.get("low") or 0),
        over=int(row.get("over") or 0),
        conc_avg=convert.raw_to_conc(row["raw_avg"], settings) if row.get("raw_avg") is not None else None,
        conc_min=convert.raw_to_conc(row["raw_min"], settings) if row.get("raw_min") is not None else None,
        conc_max=convert.raw_to_conc(row["raw_max"], settings) if row.get("raw_max") is not None else None,
        raw_min=row.get("raw_min"),
        raw_max=row.get("raw_max"),
    )


@app.websocket("/ws/readings")
async def ws_readings(websocket: WebSocket) -> None:
    """有新行就推；用最新一行的 ts 去重，不重复推同一条。"""
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
