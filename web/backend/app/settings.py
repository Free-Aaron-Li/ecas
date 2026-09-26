"""ECAS 后端配置。

补偿口径与云端 `scripts/485/app/config.py` 保持一致：**浓度 = 未补偿 + 加性 0.3 个点**
（2026-09-26 先生定案；不是 ×1.003）。

安全约定：本地开发用 Homebrew PostgreSQL 的 trust 认证（local / 127.0.0.1 / ::1），
DSN 里**不含任何凭据**；生产端走 scram-sha-256 + `/root/.pgpass`，
详见 `docs/本地开发环境.md`。工作区内不留任何口令。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ECAS_", env_file=".env", extra="ignore")

    # —— 设备与量程 ——
    model_name: str = "DPRO-66"
    i_lo: float = 4.0
    i_hi: float = 20.0
    c_lo: float = 0.0
    c_hi: float = 100.0

    # —— 补偿（add=加性偏移，mul=乘性百分比）——
    offset_mode: str = "add"
    offset_conc: float = 0.3
    offset_pct: float = 0.3

    # —— 异常阈值（参照 NAMUR NE43）——
    break_low: int = 3600
    over_high: int = 20500
    stale_seconds: int = 600

    # —— 数据库：本地 unix socket + trust，无口令 ——
    dsn: str = "postgresql://ecas@/ecas_dev?host=/tmp&port=55432"

    # —— HTTP ——
    host: str = "127.0.0.1"
    port: int = 8080
    cors_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
