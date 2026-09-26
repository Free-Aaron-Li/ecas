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

    # —— 报警上下限（浓度，单位与浓度同）——
    #: ⚠️ 40 / 60 只是**出厂默认值**，真实的上下限可以在看板上直接改
    #: （落库 `alarm_config`，重启/换浏览器都还在；「恢复默认」就回到这里）。
    #: 判据由 `convert.conc_to_raw()` 反解成 raw，SQL、图表、报警引擎共用同一处换算。
    alarm_low: float = 40.0
    alarm_high: float = 60.0

    #: 报警引擎（`app/alarms.py`）：是否开启后台周期扫描、扫描周期（秒）
    #: 关掉后仍可用 `POST /api/alarms/scan` 手动扫，事件表也照常可读。
    alarm_scan: bool = True
    alarm_scan_seconds: int = 60

    #: 导出 CSV 的最大行数（越界夹住；比 /api/readings 的 5000 更宽松，
    #: 因为导出是"要整段数据"的用途）
    export_rows_max: int = 20000

    # —— 数据库：本地 unix socket + trust，无口令 ——
    dsn: str = "postgresql://ecas@/ecas_dev?host=/tmp&port=55432"

    # —— HTTP ——
    host: str = "127.0.0.1"
    port: int = 8080
    cors_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]

    # —— 访问令牌（对外发布用）——
    #: 空 = 开发模式（不校验，仅适合只监听 127.0.0.1）；发布前必须设置。
    #: 用 `ECAS_AUTH_TOKEN=...` 注入，**不要**写进仓库里的任何文件。
    auth_token: str = ""

    #: 浏览器侧把令牌同时写进 Cookie（同名），便于刷新后直接带上下文
    auth_cookie: bool = True

    @property
    def auth_required(self) -> bool:
        return bool(self.auth_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
