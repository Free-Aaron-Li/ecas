"""数据库访问层：psycopg 3 连接池 + 极简查询助手。

约定：
* `raw_reading` **只读**（写入由采集端负责）；后端唯一会写的是报警引擎的两张表
  `alarm_event` / `alarm_config`，见 `app/alarms.py`；
* `autocommit=True` + `dict_row`；
* SQL 一律参数化，不做字符串拼接。
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def make_pool(dsn: str) -> ConnectionPool:
    """建池但不打开；由 lifespan 显式 open()/close()，避免 import 期就连库。"""
    return ConnectionPool(
        dsn,
        min_size=1,
        max_size=4,
        open=False,
        name="ecas-pool",
        kwargs={"autocommit": True, "row_factory": dict_row},
    )


def fetch_all(pool: ConnectionPool, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            return list(cur.fetchall())


def fetch_one(pool: ConnectionPool, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            return cur.fetchone()


def execute(pool: ConnectionPool, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """跑一条写/DDL 语句（autocommit）。

    有结果集（`INSERT ... RETURNING`、`SELECT`）就把行取回来，否则返回空列表 ——
    对没有结果集的语句调 `fetchall()` 在 psycopg 3 里会抛 ProgrammingError。
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            return list(cur.fetchall()) if cur.description else []
