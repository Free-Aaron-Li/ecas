"""数据库访问层：psycopg 3 连接池 + 极简查询助手。

约定：
* 只读连接（后端只读现网数据；写入由采集端负责）；
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
