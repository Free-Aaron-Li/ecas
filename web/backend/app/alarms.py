"""报警规则引擎：把"看板上一眼看到的异常"变成**落库的事件**。

为什么要有它
------------
看板只能告诉你"此刻"怎么样，人不可能一直盯着：断线发生 3 分钟又好了，
事后翻数据很难判断"到底断了多久、几点断的"。这里把每次异常记成一条事件
（开始时间、结束时间、持续多久、期间最极端的值），于是：

* 漏看不会丢 —— 事件表就是台账，可以按时间查"这个月断了几次"；
* 有开始也有结束 —— 只有一条 open 记录在"进行中"，恢复时原地补齐 `ended_ts`；
* 幂等 —— 反复扫描不会产生重复事件（靠 `(model, kind) WHERE ended_ts IS NULL`
  这个部分唯一索引兜底），所以掉电重跑、扫描周期抖动都无所谓。

规则（判据与看板完全同源，阈值来自 `alarm_config` 表或环境变量）
----------------------------------------------------------------
| 种类 | 触发条件 | 级别 |
|------|----------|------|
| `break` | `raw < 3600`（<3.6 mA，NAMUR NE43 断线） | 严重 |
| `over` | `raw > 20500`（>20.5 mA，超量程） | 严重 |
| `conc_low` | 有效区间内浓度 < 下限（默认 40.0） | 警告 |
| `conc_high` | 有效区间内浓度 > 上限（默认 60.0） | 警告 |
| `no_data` | 距最后一条数据超过 `stale_seconds`（默认 600 秒） | 严重 |

两个刻意的判断：
1. **数据断流时不评数据类规则**：最后一条数据停在两小时前，谁也不知道传感器现在是好是坏，
   此时把 `break` 判成"已恢复"或"仍断线"都是编。所以断流期间只维护 `no_data`，
   其它种类的 open 事件**原地冻结**，等数据回来再对账。
2. **只在有效区间里判浓度**：断线/超量程行的浓度是数学外推值（可能是负数），
   拿它比 40 会得出"浓度偏低"这种荒唐结论 —— 那其实是设备故障，属于 `break`/`over`。

写入边界（与"后端只读"约定的偏离，明确记录）
--------------------------------------------
后端**只写两张自己的表**：`alarm_event`（事件台账）与 `alarm_config`（阈值覆盖）。
`raw_reading` 永远只读 —— 采集端（DTU → mosquitto → monitor）是它唯一的写入者。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg_pool import ConnectionPool

from . import convert, db

log = logging.getLogger("ecas.alarms")

#: 报警种类（落库用英文键，展示用中文）
BREAK = "break"
OVER = "over"
CONC_HIGH = "conc_high"
CONC_LOW = "conc_low"
NO_DATA = "no_data"
KINDS = (BREAK, OVER, CONC_HIGH, CONC_LOW, NO_DATA)

LABELS = {
    BREAK: "断线",
    OVER: "超量程",
    CONC_HIGH: "浓度高于上限",
    CONC_LOW: "浓度低于下限",
    NO_DATA: "长时间无数据",
}
LEVELS = {
    BREAK: "严重",
    OVER: "严重",
    CONC_HIGH: "警告",
    CONC_LOW: "警告",
    NO_DATA: "严重",
}

#: 只在最近这段时间里找"当前这一轮异常"的起点；更早的历史不给它翻（避免全表扫）
LOOKBACK_HOURS = 6

#: 建表语句：幂等，启动时跑一遍（后端**唯一**会写库的地方，见模块 docstring）
DDL = (
    """
    CREATE TABLE IF NOT EXISTS alarm_event (
        id          bigserial   PRIMARY KEY,
        model       text        NOT NULL,
        kind        text        NOT NULL,
        level       text        NOT NULL,
        started_ts  timestamptz NOT NULL,
        ended_ts    timestamptz,
        peak_raw    integer,
        peak_ts     timestamptz,
        n_rows      integer     NOT NULL DEFAULT 0,
        note        text,
        created_at  timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT alarm_event_kind_chk CHECK (kind IN ('break','over','conc_high','conc_low','no_data'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS alarm_event_started_idx ON alarm_event (started_ts DESC)",
    # 同一种报警同时只允许有一条"进行中"——扫描重复执行也不会造出第二条
    "CREATE UNIQUE INDEX IF NOT EXISTS alarm_event_open_uidx ON alarm_event (model, kind) WHERE ended_ts IS NULL",
    """
    CREATE TABLE IF NOT EXISTS alarm_config (
        model      text        PRIMARY KEY,
        low        real        NOT NULL,
        high       real        NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
)

SQL_RECENT = """
SELECT ts, raw
  FROM raw_reading
 WHERE model = %(model)s
   AND ts >= now() - make_interval(hours => %(hours)s)
 ORDER BY ts ASC
"""
SQL_LAST_TS = "SELECT max(ts) AS last_ts FROM raw_reading WHERE model = %(model)s"
SQL_OPEN = """
SELECT id, kind, started_ts, peak_raw, peak_ts, n_rows, note
  FROM alarm_event
 WHERE model = %(model)s AND ended_ts IS NULL
"""
SQL_INSERT = """
INSERT INTO alarm_event (model, kind, level, started_ts, peak_raw, peak_ts, n_rows, note)
VALUES (%(model)s, %(kind)s, %(level)s, %(started_ts)s, %(peak_raw)s, %(peak_ts)s, %(n_rows)s, %(note)s)
RETURNING id
"""
SQL_UPDATE = """
UPDATE alarm_event
   SET ended_ts = %(ended_ts)s, peak_raw = %(peak_raw)s, peak_ts = %(peak_ts)s,
       n_rows = %(n_rows)s, note = %(note)s
 WHERE id = %(id)s
"""
SQL_TOUCH = """
UPDATE alarm_event
   SET peak_raw = %(peak_raw)s, peak_ts = %(peak_ts)s, n_rows = %(n_rows)s, note = %(note)s
 WHERE id = %(id)s
"""
SQL_EVENTS = """
SELECT id, model, kind, level, started_ts, ended_ts, peak_raw, peak_ts, n_rows, note
  FROM alarm_event
 WHERE model = %(model)s
   AND (ended_ts IS NULL OR (started_ts >= %(start)s AND started_ts < %(end)s))
 ORDER BY started_ts DESC
 LIMIT %(limit)s
"""
SQL_CONFIG_GET = "SELECT low, high, updated_at FROM alarm_config WHERE model = %(model)s"
SQL_CONFIG_SET = """
INSERT INTO alarm_config (model, low, high) VALUES (%(model)s, %(low)s, %(high)s)
ON CONFLICT (model) DO UPDATE SET low = EXCLUDED.low, high = EXCLUDED.high, updated_at = now()
"""
SQL_CONFIG_DEL = "DELETE FROM alarm_config WHERE model = %(model)s"


# ── 建表 ───────────────────────────────────────────────────────────────────
def ensure_schema(pool: ConnectionPool) -> None:
    """启动时建表/建索引（幂等）。失败只记日志，不让后端起不来。"""
    for statement in DDL:
        db.execute(pool, statement)


# ── 阈值：库里的覆盖 > 环境变量 ────────────────────────────────────────────
def effective_alarms(pool: ConnectionPool, settings: Any) -> dict[str, Any]:
    """当前生效的报警上下限（库里有覆盖就用覆盖，否则用环境变量默认）。"""
    row = db.fetch_one(pool, SQL_CONFIG_GET, {"model": settings.model_name})
    if row:
        return convert.resolve_alarms(settings, float(row["low"]), float(row["high"]))
    return convert.resolve_alarms(settings)


def set_alarms(pool: ConnectionPool, settings: Any, low: float, high: float) -> dict[str, Any]:
    """自定义报警上下限（持久化到库里，服务重启/换浏览器都还在）。"""
    db.execute(
        pool,
        SQL_CONFIG_SET,
        {"model": settings.model_name, "low": float(low), "high": float(high)},
    )
    log.info("报警上下限已改为 %s ~ %s（写库 alarm_config）", low, high)
    return effective_alarms(pool, settings)


def reset_alarms(pool: ConnectionPool, settings: Any) -> dict[str, Any]:
    """恢复成环境变量里的默认值。"""
    db.execute(pool, SQL_CONFIG_DEL, {"model": settings.model_name})
    log.info("报警上下限已恢复默认（删除 alarm_config 覆盖）")
    return effective_alarms(pool, settings)


def events(
    pool: ConnectionPool,
    settings: Any,
    hours: int = 24,
    limit: int = 200,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    """区间内的事件台账（进行中的那条**永远列出** —— 它可能是区间开始前就断的）。

    给了 `start`/`end` 就用绝对窗口（看板自定义时段），否则按「最近 hours 小时」滑动。
    """
    if start is None or end is None:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=max(1, int(hours)))
    rows = db.fetch_all(
        pool, SQL_EVENTS, {"model": settings.model_name, "start": start, "end": end, "limit": limit}
    )
    out = []
    for row in rows:
        started, ended = row["started_ts"], row.get("ended_ts")
        end = ended or datetime.now(timezone.utc)
        out.append(
            {
                "id": int(row["id"]),
                "kind": row["kind"],
                "kind_label": LABELS.get(row["kind"], row["kind"]),
                "level": row["level"],
                "started_ts": started,
                "ended_ts": ended,
                "duration_seconds": round((end - started).total_seconds(), 1),
                "active": ended is None,
                "peak_raw": row.get("peak_raw"),
                "peak_conc": (
                    convert.raw_to_conc(int(row["peak_raw"]), settings)
                    # 断线/超量程行的浓度是数学外推值（可能为负），对它们不给浓度，只给 raw
                    if row.get("peak_raw") is not None
                    and int(settings.i_lo * 1000) <= int(row["peak_raw"]) <= settings.over_high
                    else None
                ),
                "peak_ts": row.get("peak_ts"),
                "n_rows": int(row.get("n_rows") or 0),
                "note": row.get("note"),
            }
        )
    return out


# ── 规则判定 ───────────────────────────────────────────────────────────────
def _runs(flags: list[bool]) -> list[tuple[int, int]]:
    """把 True/False 序列折成连续 True 的段 [(起, 止), ...]（闭区间下标）。"""
    out: list[tuple[int, int]] = []
    start: int | None = None
    for i, flag in enumerate(flags):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def evaluate(pool: ConnectionPool, settings: Any, now: datetime | None = None) -> dict[str, Any]:
    """对账一次：把"当前处于异常状态"的事实同步成 alarm_event 的 open/close。

    幂等：同样的数据跑一百遍，结果与跑一遍相同（靠"同类只有一条进行中"的部分唯一索引兜底）。
    """
    now = now or datetime.now(timezone.utc)
    alarms = effective_alarms(pool, settings)
    rows = db.fetch_all(pool, SQL_RECENT, {"model": settings.model_name, "hours": LOOKBACK_HOURS})
    last_row = db.fetch_one(pool, SQL_LAST_TS, {"model": settings.model_name}) or {}
    last_ts: datetime | None = last_row.get("last_ts")

    open_rows = db.fetch_all(pool, SQL_OPEN, {"model": settings.model_name})
    open_by_kind = {row["kind"]: row for row in open_rows}
    opened: list[str] = []
    closed: list[str] = []
    touched: list[str] = []

    # ① 数据断流：只维护 no_data，其它种类冻结（见模块 docstring 的"刻意的判断"）
    stale = last_ts is None or (now - last_ts).total_seconds() > settings.stale_seconds
    if last_ts is None:
        started_ts = now
        note = "库里没有任何数据"
    else:
        started_ts = last_ts + timedelta(seconds=settings.stale_seconds)
        note = f"最后一条数据 {last_ts.astimezone().isoformat(timespec='seconds')}，" \
               f"已 {(now - last_ts).total_seconds():.0f} 秒无新数据（阈值 {settings.stale_seconds} 秒）"

    if stale:
        row = open_by_kind.get(NO_DATA)
        if row is None:
            if last_ts is None or started_ts <= now:
                db.execute(
                    pool,
                    SQL_INSERT,
                    {
                        "model": settings.model_name,
                        "kind": NO_DATA,
                        "level": LEVELS[NO_DATA],
                        "started_ts": started_ts,
                        "peak_raw": None,
                        "peak_ts": None,
                        "n_rows": 0,
                        "note": note,
                    },
                )
                opened.append(NO_DATA)
        # 只在 no_data 的"说法"变了时更新（开始时刻或剩余时长变了）：一行都没变就别写库
        elif (last_ts is not None and row["started_ts"] != started_ts) or row.get("note") != note:
            db.execute(
                pool,
                SQL_TOUCH,
                {"id": row["id"], "peak_raw": None, "peak_ts": None, "n_rows": 0, "note": note},
            )
            touched.append(NO_DATA)
        return {"opened": opened, "closed": closed, "touched": touched, "stale": True, "n_rows": len(rows)}

    # 数据回来了：先把 no_data 结掉
    row = open_by_kind.get(NO_DATA)
    if row is not None:
        db.execute(
            pool,
            SQL_UPDATE,
            {
                "id": row["id"],
                "ended_ts": max(row["started_ts"], last_ts) if last_ts else now,
                "peak_raw": row.get("peak_raw"),
                "peak_ts": row.get("peak_ts"),
                "n_rows": int(row.get("n_rows") or 0),
                "note": row.get("note"),
            },
        )
        closed.append(NO_DATA)

    # ② 数据类规则：看"最后一段连续满足"是否延续到最后一条
    lo_raw, hi_raw = int(settings.i_lo * 1000), settings.over_high
    flags: dict[str, list[bool]] = {BREAK: [], OVER: [], CONC_HIGH: [], CONC_LOW: []}
    for item in rows:
        raw = int(item["raw"])
        valid = lo_raw <= raw <= hi_raw
        flags[BREAK].append(raw < settings.break_low)
        flags[OVER].append(raw > settings.over_high)
        # 用 raw 判据（而不是再比一次浓度）：这样「扫描记的事件」「/api/summary 的越限行数」
        # 与「图表那条线」三处用的判据逐位相同，不会在边界行上互相差一条。
        flags[CONC_LOW].append(valid and raw < alarms["low_raw"])
        flags[CONC_HIGH].append(valid and raw > alarms["high_raw"])

    for kind in (BREAK, OVER, CONC_HIGH, CONC_LOW):
        segments = _runs(flags[kind])
        state = None
        if segments:
            start_i, end_i = segments[-1]
            active = end_i == len(rows) - 1
            peak_i = start_i
            for i in range(start_i, end_i + 1):
                if kind in (CONC_HIGH, OVER):
                    peak_i = i if int(rows[i]["raw"]) > int(rows[peak_i]["raw"]) else peak_i
                else:
                    peak_i = i if int(rows[i]["raw"]) < int(rows[peak_i]["raw"]) else peak_i
            state = {
                "started_ts": rows[start_i]["ts"],
                # 恢复时刻取"第一段之后不再满足"的那条数据的时间 —— 有据可查，不用 now()
                "ended_ts": None if active else rows[end_i + 1]["ts"],
                "active": active,
                "peak_raw": int(rows[peak_i]["raw"]),
                "peak_ts": rows[peak_i]["ts"],
                "n_rows": end_i - start_i + 1,
            }

        row = open_by_kind.get(kind)
        if state and state["active"]:
            if row is None:
                db.execute(
                    pool,
                    SQL_INSERT,
                    {
                        "model": settings.model_name,
                        "kind": kind,
                        "level": LEVELS[kind],
                        "started_ts": state["started_ts"],
                        "peak_raw": state["peak_raw"],
                        "peak_ts": state["peak_ts"],
                        "n_rows": state["n_rows"],
                        "note": _note(kind, state, alarms, settings),
                    },
                )
                opened.append(kind)
            elif (
                row["peak_raw"] != state["peak_raw"]
                or row["n_rows"] != state["n_rows"]
                or row["started_ts"] != state["started_ts"]
            ):
                db.execute(
                    pool,
                    SQL_TOUCH,
                    {
                        "id": row["id"],
                        "peak_raw": state["peak_raw"],
                        "peak_ts": state["peak_ts"],
                        "n_rows": state["n_rows"],
                        "note": _note(kind, state, alarms, settings),
                    },
                )
                touched.append(kind)
        elif row is not None:
            # 进行中的事件：数据说明它已经恢复（state 为 None 或已不再延续）
            ended_ts = state["ended_ts"] if state else (rows[-1]["ts"] if rows else now)
            db.execute(
                pool,
                SQL_UPDATE,
                {
                    "id": row["id"],
                    "ended_ts": ended_ts,
                    "peak_raw": state["peak_raw"] if state else row.get("peak_raw"),
                    "peak_ts": state["peak_ts"] if state else row.get("peak_ts"),
                    "n_rows": state["n_rows"] if state else int(row.get("n_rows") or 0),
                    "note": _note(kind, state, alarms, settings) if state else row.get("note"),
                },
            )
            closed.append(kind)

    if opened or closed:
        log.info("报警扫描：新开 %s ｜ 结束 %s（现有 %d 条进行中）", opened or "无", closed or "无", len(open_by_kind))
    return {
        "opened": opened,
        "closed": closed,
        "touched": touched,
        "stale": False,
        "n_rows": len(rows),
        "alarms": alarms,
    }


def _note(kind: str, state: dict[str, Any], alarms: dict[str, Any], settings: Any) -> str:
    peak = state["peak_raw"]
    if kind == CONC_HIGH:
        return f"浓度 {convert.raw_to_conc(peak, settings)} 高于上限 {alarms['high']}（raw {peak}）"
    if kind == CONC_LOW:
        return f"浓度 {convert.raw_to_conc(peak, settings)} 低于下限 {alarms['low']}（raw {peak}）"
    if kind == BREAK:
        return f"raw {peak} 低于断线判据 {settings.break_low}（电流 {convert.raw_to_ma(peak)} mA）"
    if kind == OVER:
        return f"raw {peak} 高于超量程判据 {settings.over_high}（电流 {convert.raw_to_ma(peak)} mA）"
    return ""
