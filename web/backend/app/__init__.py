"""ECAS 本地开发后端（FastAPI + PostgreSQL）。

目录结构：
    web/
      backend/           ← 本包：FastAPI 只读接口 + 换算口径的唯一实现
        app/
          settings.py    配置（环境变量前缀 ECAS_）
          convert.py     原始值 → mA → 浓度（含 +0.3 补偿，唯一实现）
          db.py          连接池与查询助手
          schemas.py     接口契约（Pydantic）
          main.py        路由 / WebSocket / 静态托管
      frontend/          ← Vite + React + TS 看板

本地开发库：Homebrew PostgreSQL 18（端口 55432，trust，无口令），库名 `ecas_dev`，
数据由云端 `pg_dump` 拉取，保证与现场同源。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
